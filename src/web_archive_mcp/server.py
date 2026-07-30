"""web-archive-mcp MCP server.

Tools:
  web_fetch      — fetch a URL, persist, return markdown
  web_search     — search the web, persist results, return formatted
  archive_list   — list archived entries with metadata
  archive_read   — read entries from an archive file
  rebuild        — rebuild FST index for the web-archive domain
"""

import ipaddress
import re
import socket
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from .storage import store, list_files, read_entries, _default_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTENT_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; web-archive-mcp)"

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "web-archive",
    instructions="Fetch and search the web — every result is persisted forever for later search and indexing",
)

# ---------------------------------------------------------------------------
# URL validation (SSRF prevention)
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> Optional[str]:
    """Validate a URL is safe to fetch. Returns error string or None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return f"Invalid URL: {url}"

    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme}. Only http and https are allowed."

    hostname = parsed.hostname
    if not hostname:
        return f"URL has no hostname: {url}"

    # Resolve hostname and check for private/internal addresses
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"

    for _, _, _, _, sockaddr in addrs:
        ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback:
            return f"URL resolves to loopback address: {ip}"
        if addr.is_private:
            return f"URL resolves to private address: {ip}"
        if addr.is_link_local:
            return f"URL resolves to link-local address: {ip}"
        if addr.is_multicast:
            return f"URL resolves to multicast address: {ip}"

    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown, falling back gracefully."""
    try:
        from markdownify import markdownify as md
        return md(html, heading_style="ATX", strip=["script", "style"])
    except ImportError:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _extract_title(markdown: str, url: str) -> str:
    """Extract a title from markdown or HTML content."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", markdown, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:120]
    return url.split("/")[-1] or url


def _decode_ddg_url(href: str) -> str:
    """Decode a DuckDuckGo redirect URL to the real destination."""
    if "duckduckgo.com/l/?" in href:
        try:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            uddg = qs.get("uddg", [None])[0]
            if uddg:
                return unquote(uddg)
        except Exception:
            pass
    return href


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_fetch(
    url: str,
    timeout: int = 30,
) -> str:
    """Fetch a URL and archive the result.

    Fetches the URL, converts HTML to markdown, and persists the result
    as a timestamped JSONL entry in the web-archive. Only http and https
    URLs are allowed; private/internal IPs are blocked.

    Args:
        url:     The URL to fetch (http/https only)
        timeout: Request timeout in seconds (default 30, max 120)
    """
    timeout = min(timeout, 120)

    err = _validate_url(url)
    if err:
        return err

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/xhtml+xml, text/plain, */*",
    }

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except httpx.TimeoutException:
        return f"Timeout fetching {url} (>{timeout}s)"
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code} fetching {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"

    content_type = resp.headers.get("content-type", "").lower()
    raw_body = resp.text

    if len(raw_body) > MAX_CONTENT_SIZE:
        return f"Response too large ({len(raw_body)} bytes, max {MAX_CONTENT_SIZE}). Not archived."

    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        content = _html_to_markdown(raw_body)
    else:
        content = raw_body

    title = _extract_title(raw_body if "text/html" in content_type else content, url)

    filepath, is_new = store("fetch", url, title, content)

    status = "archived" if is_new else "duplicate (skipped)"
    output = [
        f"# {title}",
        f"  URL: {url}",
        f"  Status: {status}",
        f"  Archived to: {filepath.name}",
        "",
        content[:5000],
    ]
    if len(content) > 5000:
        output.append(f"\n... (truncated, {len(content)} chars total)")

    return "\n".join(output)


@mcp.tool()
async def web_search(
    query: str,
) -> str:
    """Search the web and archive the results.

    Performs a web search via DuckDuckGo, persists the results, and returns
    them formatted. The results are archived for later search.

    Args:
        query: The search query (max 500 chars)
    """
    query = query[:500]

    search_url = f"https://html.duckduckgo.com/html/?q={quote(query)}"

    headers = {"User-Agent": USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, max_redirects=MAX_REDIRECTS) as client:
            resp = await client.get(search_url, headers=headers)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        return f"Error searching for '{query}': {e}"

    # Check for CAPTCHA / block page
    if "g-recaptcha" in html or "ddg-anomaly" in html.lower():
        return "DuckDuckGo returned a CAPTCHA or block page. Try again later."

    # Parse results using CSS-class-aware regex
    result_blocks = re.findall(
        r'<a[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    snippet_blocks = re.findall(
        r'<a[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    if not result_blocks:
        return f"No results found for '{query}'."

    import html as html_mod

    results = []
    for i, (href, title_raw) in enumerate(result_blocks[:10]):
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        title = html_mod.unescape(title)
        snippet = ""
        if i < len(snippet_blocks):
            snippet = re.sub(r"<[^>]+>", "", snippet_blocks[i]).strip()
            snippet = html_mod.unescape(snippet)

        real_url = _decode_ddg_url(href)

        results.append({
            "url": real_url,
            "title": title,
            "snippet": snippet,
        })

    if not results:
        return f"No results found for '{query}'."

    # Build content
    content_lines = [f"# Web search: {query}", ""]
    for i, r in enumerate(results, 1):
        content_lines.append(f"## {i}. {r['title']}")
        content_lines.append(f"URL: {r['url']}")
        if r["snippet"]:
            content_lines.append(f"> {r['snippet']}")
        content_lines.append("")

    content = "\n".join(content_lines)
    title = f"Search: {query}"

    filepath, is_new = store("search", query, title, content)

    status = "archived" if is_new else "duplicate (skipped)"
    output = [
        f"# {title}",
        f"  Status: {status}",
        f"  Archived to: {filepath.name}",
        f"  Results: {len(results)}",
        "",
    ]
    for i, r in enumerate(results, 1):
        output.append(f"{i}. **{r['title']}**")
        output.append(f"   {r['url']}")
        if r["snippet"]:
            output.append(f"   > {r['snippet']}")
        output.append("")

    return "\n".join(output)


@mcp.tool()
def archive_list(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    max_results: int = 50,
) -> str:
    """List archived web fetch/search entries with metadata.

    Args:
        date_from:   Optional start of date range (YYYY-MM-DD)
        date_to:     Optional end of date range (YYYY-MM-DD, inclusive)
        max_results: Maximum entries to show (default 50)
    """
    files = list_files(date_from=date_from, date_to=date_to)

    if not files:
        return "No archived entries found."

    label = f"filtered: {date_from} → {date_to}" if date_from or date_to else "all"
    output = [f"Web Archive Entries ({label}):"]

    count = 0
    for f in files:
        if count >= max_results:
            output.append(f"\n... (truncated at {max_results})")
            break

        parts = [
            f"  {f['name']}",
            f"    Date: {f['date']} | Entries: {f['entries']} | Size: {f['size_kb']} KB",
        ]
        if f["summary"]:
            parts.append(f"    Summary: {f['summary']}")
        output.append("\n".join(parts) + "\n")
        count += 1

    if count == 0:
        return "No archived entries match the filter."
    return "\n".join(output)


@mcp.tool()
def archive_read(
    file_id: str,
    max_entries: int = 50,
) -> str:
    """Read entries from an archive file.

    Args:
        file_id:     Archive file name (e.g., '2026-07-30-fetch-example.jsonl')
        max_entries: Maximum entries to return, newest first (default 50)
    """
    entries, target = read_entries(file_id, max_entries=max_entries)

    if not entries:
        return f"Archive file not found or empty: {file_id}"

    output = [
        f"Archive file: {file_id}",
        f"  Entries: {len(entries)} shown (newest first)",
        "",
    ]

    for i, entry in enumerate(entries):
        etype = entry.get("type", "?")
        title = entry.get("title", "?")
        ts = entry.get("timestamp", "?")
        source = entry.get("source", "?")
        content = entry.get("content", "")

        output.append(f"--- Entry {i + 1} ---")
        output.append(f"  Type: {etype}")
        output.append(f"  Title: {title}")
        output.append(f"  Time: {ts}")
        output.append(f"  {'URL' if etype == 'fetch' else 'Query'}: {source}")
        output.append(f"  Content preview: {content[:300]}...")
        output.append("")

    return "\n".join(output)


@mcp.tool()
def rebuild() -> str:
    """Rebuild the FST index for the web-archive domain.

    Calls fst-indexer to rebuild the full-text index so archived
    content is searchable via unified-history-mcp.
    """
    base = _default_dir()
    if not base.is_dir():
        return "No archive directory found. Run web_fetch or web_search first."

    jsonl_files = list(base.glob("*.jsonl"))
    if not jsonl_files:
        return "No archive files to index."

    try:
        cmd = [
            "fst-indexer",
            "build",
            "--dir", str(base.resolve()),
            "--pattern", "*.jsonl",
            "--extractor", "jsonl",
            "--output", str(base.resolve()),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            stdout_info = ""
            for line in result.stderr.splitlines():
                if "Done" in line or "files" in line.lower():
                    stdout_info = line
            return f"Index rebuilt successfully ({len(jsonl_files)} files). {stdout_info}"
        else:
            return f"Index build failed: {result.stderr.strip()}"
    except FileNotFoundError:
        return "fst-indexer binary not found. Install it from https://github.com/palimpsest-labs/fst-indexer"
    except subprocess.TimeoutExpired:
        return "Index build timed out (too many large files?)"
    except OSError as e:
        return f"Index build error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
