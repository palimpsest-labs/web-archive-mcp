"""Unit tests for PlaywrightSession recording logic (no real browser, no network)."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_archive_mcp import playwright_session  # noqa: E402
from web_archive_mcp.playwright_session import PlaywrightSession  # noqa: E402


class FakeReq:
    def __init__(self, method="GET", url="https://e.com/", headers=None, post_data=None):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.post_data = post_data


class FakeResp:
    def __init__(self, method="GET", url="https://e.com/", status=200,
                 req_headers=None, resp_headers=None, body=b"ok", post_data=None):
        self.request = FakeReq(method, url, req_headers, post_data)
        self.status = status
        self.headers = resp_headers if resp_headers is not None else {"content-type": "text/html"}
        self._body = body

    async def body(self):
        return self._body


def test_init_state():
    s = PlaywrightSession(archive_dir="/tmp/x")
    assert s.active is False
    assert s.recorded_new == 0
    assert s.recorded_dup == 0
    assert s.limit_hit is False


@pytest.mark.asyncio
async def test_record_response_stores_entry(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path)
    calls = []

    def fake_store(entry_type, source, title, content, base_dir):
        calls.append((entry_type, source, title, base_dir))
        return None, True

    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(FakeResp())

    assert len(calls) == 1
    et, src, title, base = calls[0]
    assert et == "request"
    assert src == "https://e.com/"
    assert "GET https://e.com/" in title
    assert base == tmp_path
    assert s.recorded_new == 1


@pytest.mark.asyncio
async def test_record_response_skips_binary_body(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path)
    seen = []

    def fake_store(*args, **kwargs):
        seen.append(args[3])  # content
        return None, True

    resp = FakeResp(resp_headers={"content-type": "image/png"}, body=b"BINARYBLOB")
    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(resp)
    assert "BINARYBLOB" not in seen[0]
    assert "(non-text or empty)" in seen[0]


@pytest.mark.asyncio
async def test_record_response_skips_body_over_max(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path, max_body=100)
    seen = []

    def fake_store(*args, **kwargs):
        seen.append(args[3])
        return None, True

    resp = FakeResp(resp_headers={"content-type": "text/html", "content-length": "5000"},
                    body=b"x" * 5000)
    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(resp)
    assert "(non-text or empty)" in seen[0]


@pytest.mark.asyncio
async def test_record_response_redacts_auth_headers(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path)
    seen = []

    def fake_store(*args, **kwargs):
        seen.append(args[3])
        return None, True

    resp = FakeResp(req_headers={"Authorization": "Bearer secret"},
                    resp_headers={"content-type": "text/html"})
    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(resp)
    assert "[REDACTED]" in seen[0]
    assert "Bearer secret" not in seen[0]


@pytest.mark.asyncio
async def test_record_response_dedup(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path)
    calls = []

    def fake_store(*args, **kwargs):
        calls.append(1)
        return None, True

    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(FakeResp())
        await s._record_response(FakeResp())  # identical -> dedup
    assert len(calls) == 1
    assert s.recorded_new == 1
    assert s.recorded_dup == 1


@pytest.mark.asyncio
async def test_record_response_max_entries(tmp_path):
    s = PlaywrightSession(archive_dir=tmp_path, max_entries=1)
    calls = []

    def fake_store(*args, **kwargs):
        calls.append(1)
        return None, True

    with patch.object(playwright_session.storage, "store", side_effect=fake_store):
        await s._record_response(FakeResp(url="https://e.com/a"))
        await s._record_response(FakeResp(url="https://e.com/b"))  # hits the cap
    assert len(calls) == 1
    assert s.limit_hit is True


@pytest.mark.asyncio
async def test_close_idempotent_on_fresh_session():
    s = PlaywrightSession(archive_dir="/tmp/x")
    await s.close()  # all attrs None -> must not raise
    assert s.active is False
