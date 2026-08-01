"""Playwright HTTP request/response recorder for the web-archive store.

Captures every HTTP request/response from a Playwright session and writes each
pair as a JSONL entry (type "request") into the web-archive store, reusing the
same schema and content-hash dedup as web_fetch/web_search.
"""

import glob
import hashlib
import os
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import storage

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
    "x-api-key", "x-auth-token", "x-access-token", "x-csrf-token",
    "x-srf-token", "x-session-id", "api-key", "referer", "origin",
)


def default_archive_dir() -> Path:
    return storage._default_dir()


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


def find_chromium() -> Optional[str]:
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


def _resolve_chromium(playwright, executable: Optional[str]) -> Optional[str]:
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


def _body_text(body, content_type: str) -> Optional[str]:
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


def record_session(urls, wait: float = 2.0, timeout: float = 30.0,
                   max_body: int = 2_000_000, max_entries: int = 10000,
                   executable: Optional[str] = None, headful: bool = False,
                   redact_auth: bool = True, archive_dir: Optional[Path] = None,
                   progress: Optional[Callable[[str], None]] = None) -> dict:
    """Drive a headless browser, capturing network activity into the store.

    Returns a summary dict with keys: count, new, dup, limit, errors.
    Raises RuntimeError if no Chromium binary can be located.
    """
    from playwright.sync_api import sync_playwright

    archive_dir = Path(archive_dir) if archive_dir else default_archive_dir()
    count = {"n": 0, "new": 0, "dup": 0, "limit": False}
    seen: set[tuple[str, str]] = set()  # (url, content_hash) seen this run
    errors: list[str] = []

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
            if progress:
                progress(f"  [{status}] {req.method} {req.url} "
                         f"{'new' if is_new else 'dup'} -> {path.name}")
        except Exception as e:
            if progress:
                progress(f"  !! failed to record response: {e}")

    with sync_playwright() as p:
        exe = _resolve_chromium(p, executable)
        if not exe:
            raise RuntimeError("No chromium binary found. Run `playwright install chromium`.")
        browser = p.chromium.launch(headless=not headful, executable_path=exe)
        try:
            for url in urls:
                if count["limit"]:
                    break
                try:
                    norm = normalize_url(url)
                except ValueError as e:
                    errors.append(str(e))
                    continue
                # A fresh context per URL keeps cookies/localStorage from
                # leaking between the sites we visit.
                context = browser.new_context()
                try:
                    context.on("response", on_response)
                    page = context.new_page()
                    try:
                        if progress:
                            progress(f"== {norm}")
                        page.goto(norm, wait_until="domcontentloaded", timeout=timeout * 1000)
                        if wait:
                            page.wait_for_timeout(int(wait * 1000))
                    except Exception as e:
                        errors.append(f"{norm}: {e}")
                    finally:
                        page.close()
                finally:
                    context.close()
        finally:
            browser.close()

    return {
        "count": count["n"],
        "new": count["new"],
        "dup": count["dup"],
        "limit": count["limit"],
        "errors": errors,
    }
