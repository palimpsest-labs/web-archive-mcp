"""Persistent interactive Playwright session with always-on traffic recording.

A single long-lived browser context lives inside the server. Every response
observed on it is archived to the web-archive store (type "request"), so as an
agent drives the page (navigate/click/fill), the network traffic is recorded
automatically. Uses Playwright's async API so it shares the server's event loop.
"""

from pathlib import Path
from typing import Optional

from . import storage
from . import playwright_recorder


class PlaywrightSession:
    """An interactive browser session whose network traffic is always archived."""

    def __init__(self, archive_dir: Optional[Path] = None):
        self.archive_dir = Path(archive_dir) if archive_dir else playwright_recorder.default_archive_dir()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.recorded = 0
        self.nav_errors: list[str] = []

    # -- lifecycle -----------------------------------------------------------

    @property
    def active(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        exe = playwright_recorder._resolve_chromium(self._pw, None)
        if not exe:
            await self._pw.stop()
            raise RuntimeError("No chromium binary found. Run `playwright install chromium`.")
        self._browser = await self._pw.chromium.launch(headless=True, executable_path=exe)
        self._context = await self._browser.new_context()
        self._context.on("response", self._record_response)
        self._page = await self._context.new_page()
        self.recorded = 0
        self.nav_errors = []

    async def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._browser = self._page = self._pw = None

    # -- driving -------------------------------------------------------------

    async def navigate(self, url: str) -> str:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"{await self._page.title()} | {url}"

    async def click(self, selector: str) -> None:
        await self._page.click(selector, timeout=10000)

    async def fill(self, selector: str, value: str) -> None:
        await self._page.fill(selector, value)

    async def text(self) -> str:
        return await self._page.inner_text("body")

    async def html(self) -> str:
        return await self._page.content()

    async def screenshot(self, path: Path) -> str:
        await self._page.screenshot(path=str(path), full_page=False)
        return str(path)

    async def back(self) -> None:
        await self._page.go_back()

    async def forward(self) -> None:
        await self._page.go_forward()

    async def current_url(self) -> str:
        return self._page.url

    def stats(self) -> dict:
        return {"active": self.active, "recorded": self.recorded,
                "url": self._page.url if self.active else None,
                "nav_errors": self.nav_errors}

    # -- recording -----------------------------------------------------------

    async def _record_response(self, resp) -> None:
        """Archive a response (and its request) to the store, real time."""
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
                ct = resp_headers.get("content-type", "")
                # Skip the body before fetching it (avoid streams / large binaries).
                if ct and any(t in ct.lower() for t in playwright_recorder.SKIP_BODY_TYPES):
                    resp_body = ""
                else:
                    raw = await resp.body()
                    resp_body = playwright_recorder._body_text(raw, ct) or ""
            except Exception:
                resp_body = ""

            # Redact auth headers by default (consistent with the batch recorder).
            req_headers = playwright_recorder._redact_headers(req_headers)
            resp_headers = playwright_recorder._redact_headers(resp_headers)

            content = playwright_recorder._build_content(
                req.method, req.url, req_headers, req_body, status, resp_headers, resp_body
            )
            storage.store(
                "request", req.url, f"{req.method} {req.url} -> {status}",
                content, base_dir=self.archive_dir,
            )
            self.recorded += 1
        except Exception:
            pass  # never let a recording failure break the session
