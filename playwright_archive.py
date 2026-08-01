#!/usr/bin/env python3
"""Record every HTTP request/response from a Playwright session into the web-archive store.

Each completed request/response pair is written in real time as a JSONL entry
into ~/.local/share/web-archive/YYYY-MM-DD-request-{host}.jsonl, using the same
schema and dedup as web_fetch/web_search. After a run, rebuild the FST index
(web_archive_rebuild) to make the entries searchable.

By default, authentication-related headers (Authorization, Cookie, Set-Cookie,
Proxy-Authorization, X-API-Key, X-Auth-Token) are redacted before storage. Pass
--no-redact-auth to store them verbatim. Bodies are never redacted.

Usage:
    playwright_archive.py [options] URL [URL ...]

Options:
    --wait SECONDS      Extra wait after page load for async requests (default 2)
    --timeout SECONDS   Navigation timeout (default 30)
    --max-body N        Max request/response body chars to record (default 2_000_000)
    --max-entries N     Stop recording after N entries total (default 10000)
    --executable P      Chromium binary path (auto-detected if omitted)
    --headful           Run a visible browser (default: headless)
    --redact-auth       Redact auth headers (default: on); use --no-redact-auth to disable
    --archive DIR       Archive directory (default: ~/.local/share/web-archive)
"""

import argparse
import glob
import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Make web-archive-mcp's storage module importable regardless of CWD.
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))

from web_archive_mcp import storage  # noqa: E402

# Content types whose bodies we skip (binary / streaming / media).
SKIP_BODY_TYPES = (
    "image/", "audio/", "video/", "font/",
    "application/octet-stream", "application/zip", "application/gzip",
    "application/pdf", "application/x-msdownload", "application/wasm",
    "text/event-stream",
)

# Headers whose values are secrets and should be redacted by default.
AUTH_HEADERS = (
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "x-auth-token",
)


def normalize_url(url: str) -> str:
    """Normalize a URL: prepend https:// if missing, enforce http/https only.

    Raises ValueError for unsupported schemes (file:, data:, ftp:, etc.).
    """
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}' "
                         f"(only http/https allowed): {url}")
    return url


def find_chromium() -> str | None:
    """Return the newest installed Playwright chromium binary, if any."""
    best = None
    best_rev = -1
    pattern = os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")
    for chrome in glob.glob(pattern):
        m = re.search(r"chromium-(\d+)", chrome)
        rev = int(m.group(1)) if m else 0
        if rev > best_rev:
            best_rev = rev
            best = chrome
    return best


def _resolve_chromium(playwright, executable: str | None) -> str | None:
    """Prefer Playwright's canonical browser path, falling back to a glob."""
    if executable:
        return executable
    try:
        path = playwright.chromium.executable_path
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return find_chromium()


def _redact_headers(headers: dict) -> dict:
    """Return a copy with auth-related header values replaced by [REDACTED]."""
    return {k: "[REDACTED]" if k.lower() in AUTH_HEADERS else v for k, v in headers.items()}


def _body_text(body, content_type: str) -> str | None:
    """Return a textual body, or None if it's binary/skippable/empty."""
    if not body:
        return None
    if content_type and any(t in content_type.lower() for t in SKIP_BODY_TYPES):
        return None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    return str(body)


def _build_content(method: str, url: str, req_headers: dict, req_body: str,
                   status: int, resp_headers: dict, resp_body: str) -> str:
    """Build the plain-text entry content for a request/response pair."""
    lines = [f"{method} {url}", f"STATUS {status}", ""]
    if req_headers:
        lines += ["REQUEST HEADERS",
                  "\n".join(f"    {k}: {v}" for k, v in req_headers.items()), ""]
    if req_body:
        lines += ["REQUEST BODY", req_body, ""]
    if resp_headers:
        lines += ["RESPONSE HEADERS",
                  "\n".join(f"    {k}: {v}" for k, v in resp_headers.items()), ""]
    lines += ["RESPONSE BODY", resp_body if resp_body else "(non-text or empty)"]
    return "\n".join(lines)


def _record(storage_mod, archive_dir: Path, method: str, url: str,
            req_headers: dict, req_body: str, status: int,
            resp_headers: dict, resp_body: str, max_body: int) -> tuple[Path, bool]:
    """Build and store one request/response entry. Returns (path, is_new)."""
    req_body = (req_body or "")[:max_body]
    resp_body = (resp_body or "")[:max_body]
    content = _build_content(method, url, req_headers, req_body, status, resp_headers, resp_body)
    title = f"{method} {url} -> {status}"
    return storage_mod.store("request", url, title, content, base_dir=archive_dir)


def record_session(urls, wait: float, timeout: int, max_body: int, max_entries: int,
                   executable: str | None, headful: bool, redact_auth: bool,
                   archive_dir: Path):
    """Drive a headless browser, capturing all network activity into the store."""
    from playwright.sync_api import sync_playwright

    count = {"n": 0, "new": 0, "dup": 0, "limit": False}
    seen: set[tuple[str, str]] = set()  # (url, content_hash) seen this run

    def on_response(resp):
        if count["limit"]:
            return
        try:
            req = resp.request
            req_headers = {}
            try:
                req_headers = dict(req.headers)
            except Exception:
                req_headers = {}
            req_body = ""
            try:
                req_body = req.post_data or ""
            except Exception:
                req_body = ""

            status = resp.status
            resp_headers = {}
            resp_body = ""
            try:
                resp_headers = dict(resp.headers)
                content_type = resp_headers.get("content-type", "")
                # Skip the body BEFORE fetching it (avoids blocking on streams
                # and pulling large binaries into memory).
                if content_type and any(t in content_type.lower() for t in SKIP_BODY_TYPES):
                    resp_body = ""
                else:
                    resp_body = _body_text(resp.body(), content_type) or ""
            except Exception:
                resp_body = ""

            if redact_auth:
                req_headers = _redact_headers(req_headers)
                resp_headers = _redact_headers(resp_headers)

            req_body = req_body[:max_body]
            resp_body = resp_body[:max_body]
            content = _build_content(req.method, req.url, req_headers, req_body,
                                     status, resp_headers, resp_body)
            content_hash = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
            key = (req.url, content_hash)
            if key in seen:
                count["n"] += 1
                count["dup"] += 1
                return
            seen.add(key)

            if count["n"] >= max_entries:
                count["limit"] = True
                return
            path, is_new = storage.store(
                "request", req.url, f"{req.method} {req.url} -> {status}",
                content, base_dir=archive_dir,
            )
            count["n"] += 1
            if is_new:
                count["new"] += 1
            print(f"  [{status}] {req.method} {req.url} "
                  f"{'new' if is_new else 'dup'} -> {path.name}", flush=True)
        except Exception as e:
            print(f"  !! failed to record response: {e}", flush=True)

    with sync_playwright() as p:
        exe = _resolve_chromium(p, executable)
        if not exe:
            sys.exit("No chromium binary found. Run `playwright install chromium`.")
        browser = p.chromium.launch(headless=not headful, executable_path=exe)
        try:
            for url in urls:
                if count["limit"]:
                    break
                try:
                    norm = normalize_url(url)
                except ValueError as e:
                    print(f"  !! {e}")
                    continue
                # A fresh context per URL keeps cookies/localStorage from
                # leaking between the sites we visit.
                context = browser.new_context()
                try:
                    context.on("response", on_response)
                    page = context.new_page()
                    try:
                        print(f"== {norm}")
                        page.goto(norm, wait_until="domcontentloaded", timeout=timeout * 1000)
                        if wait:
                            page.wait_for_timeout(int(wait * 1000))
                    except Exception as e:
                        print(f"  !! navigation error: {e}")
                    finally:
                        page.close()
                finally:
                    context.close()
        finally:
            browser.close()

    if count["limit"]:
        print(f"\nStopped early: hit max_entries ({max_entries}).")
    print(f"\nDone. {count['n']} request/response recorded ({count['new']} new, "
          f"{count['dup']} in-run dupes skipped).")
    print("Run web_archive_rebuild to make them searchable.")


def main():
    ap = argparse.ArgumentParser(description="Record Playwright HTTP traffic into the web-archive store.")
    ap.add_argument("urls", nargs="+", help="URL(s) to visit")
    ap.add_argument("--wait", type=float, default=2.0, help="wait seconds after load (default 2)")
    ap.add_argument("--timeout", type=float, default=30.0, help="navigation timeout in seconds (default 30)")
    ap.add_argument("--max-body", type=int, default=2_000_000, help="max body chars per entry")
    ap.add_argument("--max-entries", type=int, default=10_000, help="max entries per run")
    ap.add_argument("--executable", default=None, help="chromium binary path")
    ap.add_argument("--headful", action="store_true", help="visible browser")
    ap.add_argument("--redact-auth", dest="redact_auth", action=argparse.BooleanOptionalAction, default=True,
                    help="redact auth headers (default on); use --no-redact-auth to disable")
    ap.add_argument("--archive", default=str(storage._default_dir()), help="archive directory")
    args = ap.parse_args()

    if args.max_entries < 1:
        ap.error("--max-entries must be >= 1")
    if args.max_body < 1:
        ap.error("--max-body must be >= 1")

    archive_dir = Path(args.archive)
    record_session(args.urls, args.wait, args.timeout, args.max_body, args.max_entries,
                   args.executable, args.headful, args.redact_auth, archive_dir)


if __name__ == "__main__":
    main()
