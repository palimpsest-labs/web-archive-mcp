"""web-archive-mcp MCP server.

Tools:
  web_fetch      — fetch a URL, persist, return markdown
  web_search     — search the web, persist results, return formatted
  archive_list   — list archived entries with metadata
  archive_read   — read entries from an archive file
  rebuild        — rebuild FST index for the web-archive domain

Browser-driven traffic capture lives in the separate playwright-archive-mcp
server; both persist to the same shared web-archive-store.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from bs4 import BeautifulSoup, Comment
from mcp.server.fastmcp import FastMCP

from web_archive_store.storage import store, list_files, read_entries, _default_dir
from web_archive_store.url import validate_url

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTENT_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_REDIRECTS = 5
MAX_OUTPUT_CHARS = 2_000_000  # 2 MB budget for full_content responses
USER_AGENT = "Mozilla/5.0 (compatible; web-archive-mcp)"
ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "web-archive",
    instructions="Fetch and search the web — every result is persisted forever for later search and indexing",
)

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


def _redact_html(html: str) -> str:
    """Strip scripts, styles, comments, hidden inputs, event handlers, and
    dangerous URIs using a DOM parser so we don't mangle legitimate markup.

    Removes: <script>, <style>, <!-- comments -->, <input type="hidden">,
    on* attributes, javascript:/vbscript: URIs, and data-*-token/data-*-key attrs.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove entire elements
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    for tag in soup.find_all("input", type="hidden"):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Remove on* event handler attributes from ALL elements
    for tag in soup.find_all():
        attrs_to_drop = [
            attr for attr in tag.attrs
            if attr.lower().startswith("on")
            or (attr.lower().startswith("data-") and
                (attr.lower().endswith("-token") or attr.lower().endswith("-key")))
        ]
        for attr in attrs_to_drop:
            del tag[attr]

    # Strip javascript: and vbscript: from URI-bearing attributes
    _uri_attrs = {"href", "src", "action", "formaction"}
    for tag in soup.find_all():
        for attr in _uri_attrs:
            val = tag.get(attr)
            if val and isinstance(val, str):
                stripped = val.strip()
                if stripped.lower().startswith(("javascript:", "vbscript:")):
                    del tag[attr]

    return str(soup)


_SECRET_PATTERNS = [
    # Bearer / OAuth tokens
    (r'(?:Bearer|bearer)\s+([A-Za-z0-9\-._~+/]+=*)', r'Bearer [REDACTED]'),
    # API key prefixes (sk-, ghp_, gho_, ghu_, ghs_, github_pat_, AKIA, etc.)
    (r'\b(sk-[A-Za-z0-9_\-]{16,})\b', r'sk-[REDACTED]'),
    (r'\b(gh[pous]_[A-Za-z0-9]{36,})\b', r'gh[x]_[REDACTED]'),
    (r'\b(github_pat_[A-Za-z0-9_]{22,})\b', r'github_pat_[REDACTED]'),
    (r'\b(AKIA[A-Z0-9]{16})\b', r'AKIA[REDACTED]'),
    # Generic high-entropy-looking base64/hex tokens (40+ chars of [A-Za-z0-9+/=])
    (r'\b([A-Za-z0-9+/=]{40,})\b', None),  # matched but kept as-is unless flag says drop
]


def _redact_content(text: str) -> str:
    """Sweep common secret patterns from markdown/text content.

    Replaces API keys, bearer tokens, and other credential-like strings
    with placeholders so they are not persisted forever in the archive.
    Does NOT touch the raw HTML — that is handled by _redact_html().
    """
    for pattern, replacement in _SECRET_PATTERNS:
        if replacement is None:
            continue  # skip the generic catch-all for now
        text = re.sub(pattern, replacement, text)
    return text


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
    token: Optional[str] = None,
    preview: bool = True,
    redact_html: bool = True,
    method: str = "GET",
    body: Optional[str] = None,
    content_type: Optional[str] = None,
) -> str:
    """Fetch a URL and archive the result.

    Fetches the URL, converts HTML to markdown, and persists the result
    as a timestamped JSONL entry in the web-archive. Only http and https
    URLs are allowed; private/internal IPs are blocked.

    Args:
        url:     The URL to fetch (http/https only)
        timeout: Request timeout in seconds (default 30, max 120)
        token:   Optional Bearer token for authenticated requests
        preview: When True (default), return only the first 5000 chars of
            content. When False, return the content up to 1,000,000 chars
            (truncated with notice if larger).
            The full content and raw HTML are always archived regardless.
        redact_html: When True (default), strips `<script>`, `<style>`,
            comments, hidden inputs, and event handlers from the archived
            raw HTML
        method:  HTTP method to use (case-insensitive; uppercased internally).
            One of GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS. Default GET.
        body:    Optional request body (for POST/PUT/PATCH/DELETE/etc.).
        content_type: Optional Content-Type header for the request.
    """
    timeout = min(timeout, 120)

    method = (method or "GET").upper()
    if method not in ALLOWED_METHODS:
        return f"Unsupported HTTP method: {method} (allowed: {', '.join(sorted(ALLOWED_METHODS))})"

    err = validate_url(url)
    if err:
        return err

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/xhtml+xml, text/plain, */*",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            resp = await client.request(method, url, headers=headers, content=body)
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

    is_html = "text/html" in content_type or "application/xhtml+xml" in content_type
    if is_html:
        content = _html_to_markdown(raw_body)
        raw_html = raw_body if not redact_html else _redact_html(raw_body)
    else:
        content = raw_body
        raw_html = None

    # Sweep secrets from the content body before it is persisted.
    content = _redact_content(content)

    # Sweep secrets from the raw HTML too — it is persisted and searchable,
    # so it must not retain secret material even when redact_html is off.
    if raw_html is not None:
        raw_html = _redact_content(raw_html)

    title = _extract_title(raw_body if is_html else content, url)
    # The title is derived from unredacted raw_body — sweep it so page titles
    # don't leak secrets into the archive or logs.
    title = _redact_content(title)

    filepath, is_new = store("fetch", url, title, content, raw_html=raw_html, method=method)

    status = "archived" if is_new else "duplicate (skipped)"
    output = [
        f"# {title}",
        f"  URL: {url}",
        f"  Method: {method}",
        f"  Status: {status}",
        f"  Archived to: {filepath.name}",
        "",
    ]
    if preview:
        output.append(content[:5000])
        if len(content) > 5000:
            output.append(f"\n... (truncated, {len(content)} chars total)")
    elif len(content) > 1_000_000:
        output.append(content[:1_000_000])
        output.append(f"\n... (truncated at 1M chars, {len(content)} chars total)")
    else:
        output.append(content)

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
    max_entries: Optional[int] = None,
    full_content: bool = False,
    jq: Optional[str] = None,
) -> str:
    """Read entries from an archive file.

    Args:
        file_id:      Archive file name (e.g., '2026-07-30-fetch-example.jsonl')
        max_entries:  Maximum entries to return, newest first (default 50;
            default 10 when full_content=True). An explicit value is honored.
        full_content: When True, include the full content and raw HTML (if
            present) for each entry instead of the 300-char preview. Responses
            are capped at a 2 MB total output budget.
        jq:           Optional jq filter string. When provided, each entry's
            JSON is piped through `jq <filter>`. Entries where jq produces
            nothing, "null", or "false" are skipped. When jq is set,
            `full_content` is ignored — jq controls what is shown.
    """
    if max_entries is None:
        if jq is not None:
            # full_content is ignored in jq mode — jq controls the output.
            max_entries = 50
        else:
            max_entries = 10 if full_content else 50
    elif max_entries < 1:
        max_entries = 1

    entries, target = read_entries(file_id, max_entries=max_entries)

    if not entries:
        return f"Archive file not found or empty: {file_id}"

    # jq filter mode: ignore full_content and the 2 MB budget — jq controls output.
    if jq:
        return _archive_read_jq(file_id, entries, jq)

    output = [
        f"Archive file: {file_id}",
        f"  Entries: {len(entries)} shown (newest first)",
        "",
    ]

    total_chars = 0
    entries_shown = 0
    for i, entry in enumerate(entries):
        etype = entry.get("type", "?")
        title = entry.get("title", "?")
        ts = entry.get("timestamp", "?")
        source = entry.get("source", "?")
        content = entry.get("content", "")
        raw_html = entry.get("raw_html")

        entry_output = [
            f"--- Entry {i + 1} ---",
            f"  Type: {etype}",
            f"  Title: {title}",
            f"  Time: {ts}",
            f"  {'URL' if etype == 'fetch' else 'Query'}: {source}",
        ]
        if raw_html:
            entry_output.append("  Raw HTML: available")
        if full_content:
            if len(content) > 500_000:
                entry_output.append(f"  Content:\n{content[:500_000]}")
                entry_output.append(f"\n... (content truncated at 500k chars, {len(content)} chars total)")
            else:
                entry_output.append(f"  Content:\n{content}")
            if raw_html:
                if len(raw_html) > 500_000:
                    entry_output.append(f"\n  Raw HTML:\n{raw_html[:500_000]}")
                    entry_output.append(f"\n... (raw_html truncated at 500k chars, {len(raw_html)} chars total)")
                else:
                    entry_output.append(f"\n  Raw HTML:\n{raw_html}")
        else:
            entry_output.append(f"  Content preview: {content[:300]}...")
        entry_output.append("")

        entry_block = "\n".join(entry_output)
        if full_content and total_chars + len(entry_block) > MAX_OUTPUT_CHARS and entries_shown > 0:
            output.append(f"\n... (size budget reached at {entries_shown} of {len(entries)} entries)")
            break
        output.append(entry_block)
        total_chars += len(entry_block) + 1  # +1 for the blank line
        entries_shown += 1

    return "\n".join(output)


def _archive_read_jq(file_id: str, entries: list, jq: str) -> str:
    """Render entries through a jq filter.

    Each entry's JSON is piped through `jq <filter>`. Entries where jq
    produces nothing, "null", or "false" are skipped. On a jq error the
    entry is still shown with the error attached.
    """
    JQ_MAX_OUTPUT = 100_000  # per-entry clamp to prevent huge single-field dumps

    matched = 0
    output = [
        f"Archive file: {file_id}",
        f"  Running jq '{jq}' over {len(entries)} entries",
        "",
    ]

    for i, entry in enumerate(entries):
        entry_json = json.dumps(entry)
        try:
            result = subprocess.run(
                ["jq", jq],
                input=entry_json,
                capture_output=True,
                text=True,
                # Empty env so jq can't read/expose serve environment variables.
                env={},
                # Guard against infinite-recursion filters that hang forever.
                timeout=10,
            )
        except FileNotFoundError:
            return (
                f"jq binary not found. Install jq (e.g. `apt install jq`) "
                f"to use the jq parameter."
            )
        except subprocess.TimeoutExpired:
            return f"jq filter timed out after 10s: {jq}"
        except OSError as e:
            return f"jq error running filter: {e}"

        etype = entry.get("type", "?")
        title = entry.get("title", "?")
        ts = entry.get("timestamp", "?")
        source = entry.get("source", "?")

        header = "\n".join([
            f"--- Entry {i + 1} (jq match) ---",
            f"  Type: {etype}",
            f"  Title: {title}",
            f"  Time: {ts}",
            f"  {'URL' if etype == 'fetch' else 'Query'}: {source}",
        ])

        if result.returncode != 0:
            # Show the entry with the jq error attached so the agent can
            # see what failed; don't silently drop it.
            err = result.stderr.strip()
            output.append(header)
            output.append(f"  jq error: {err}")
            output.append("")
            continue

        stdout = result.stdout.strip()
        if stdout in ("", "null", "false"):
            # jq produced no output / null / false -> skip this entry.
            continue

        matched += 1
        if len(stdout) > JQ_MAX_OUTPUT:
            stdout = stdout[:JQ_MAX_OUTPUT] + "\n... (jq output truncated at 100k chars)"

        indented = "\n".join(f"    {ln}" for ln in stdout.splitlines())
        output.append(header)
        output.append("  jq result:")
        output.append(indented)
        output.append("")

    if matched == 0:
        output.append(f"No entries matched jq '{jq}' in {file_id}")
    else:
        output.append(f"{matched} of {len(entries)} entries matched jq '{jq}'")

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
