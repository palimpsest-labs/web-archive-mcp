"""Unit tests for the Playwright MCP tool validation/error paths (no browser, no network).

SSRF tests rely on _validate_url resolving "localhost" / link-local addresses
to loopback/private IPs, which needs no external network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import web_archive_mcp.server as server  # noqa: E402


# --- playwright_record: input bounds + SSRF ---------------------------------

@pytest.mark.asyncio
async def test_record_empty_urls():
    assert "No URLs provided" in await server.playwright_record([])


@pytest.mark.asyncio
async def test_record_too_many_urls():
    many = [f"https://e.com/{i}" for i in range(11)]
    assert "Too many URLs" in await server.playwright_record(many)


@pytest.mark.asyncio
async def test_record_invalid_timeout_and_entries():
    assert "timeout" in await server.playwright_record(["https://e.com"], timeout=0)
    assert "wait" in await server.playwright_record(["https://e.com"], wait=-1)
    assert "max_entries" in await server.playwright_record(["https://e.com"], max_entries=0)


@pytest.mark.asyncio
async def test_record_rejects_private_loopback():
    assert "loopback" in await server.playwright_record(["http://localhost:8080"])


@pytest.mark.asyncio
async def test_record_rejects_link_local_metadata():
    assert "private" in await server.playwright_record(["http://169.254.169.254/latest/meta-data/"])


@pytest.mark.asyncio
async def test_record_rejects_bad_scheme():
    assert "Unsupported URL scheme" in await server.playwright_record(["file:///etc/passwd"])


# --- playwright_navigate: SSRF + session guards -----------------------------

def test_navigate_no_session():
    # Ensure no session exists
    server._SESSION = None
    out = server.playwright_navigate("https://example.com")
    # tool is async; run it
    import asyncio
    assert "No active session" in asyncio.run(out)


@pytest.mark.asyncio
async def test_navigate_rejects_private_without_session_guard_ordering():
    # Even with no active session, URL validation happens first only when active.
    server._SESSION = None
    # With no session, returns no-session message (guard is before validation).
    assert "No active session" in await server.playwright_navigate("http://localhost")


@pytest.mark.asyncio
async def test_navigate_rejects_bad_scheme(monkeypatch):
    class FakeSession:
        active = True
    server._SESSION = FakeSession()
    try:
        assert "Unsupported URL scheme" in await server.playwright_navigate("ftp://e.com")
    finally:
        server._SESSION = None


# --- playwright_stats / close with no session -------------------------------

@pytest.mark.asyncio
async def test_stats_no_session():
    server._SESSION = None
    assert "No session" in await server.playwright_stats()


@pytest.mark.asyncio
async def test_close_no_session():
    server._SESSION = None
    assert "No session" in await server.playwright_close()


# --- playwright_screenshot filename sanitization ------------------------------

@pytest.mark.asyncio
async def test_screenshot_sanitizes_name(monkeypatch, tmp_path):
    captured = {}

    class FakeSession:
        active = True

        async def screenshot(self, path, full_page=False):
            captured["path"] = path
            return str(path)

    server._SESSION = FakeSession()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    try:
        out = await server.playwright_screenshot("../../evil")
    finally:
        server._SESSION = None
    # Directory components stripped; file is inside tmp_path/Downloads.
    assert str(tmp_path / "Downloads" / "evil.png") == str(captured["path"])
    assert ".." not in str(captured["path"])


@pytest.mark.asyncio
async def test_screenshot_requires_active_session():
    server._SESSION = None
    assert "No active session" in await server.playwright_screenshot("x.png")
