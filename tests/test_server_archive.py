"""Tests for web-archive-mcp server tools: web_fetch, archive_read (incl. jq), and the redaction helpers.

web_archive_store.storage is covered by the web-archive-store package's own
test suite; here we test the server-level behaviour built on top of it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from web_archive_store.storage import read_entries  # noqa: E402
from web_archive_mcp import server  # noqa: E402


# ---------------------------------------------------------------------------
# web_fetch — preview control + raw_html archiving
# ---------------------------------------------------------------------------


class FakeResponse:
    headers = {"content-type": "text/html; charset=utf-8"}
    text = (
        "<html><head><title>My Page</title></head>"
        "<body><h1>Hello</h1><p>Some longer content here.</p></body></html>"
    )

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return FakeResponse()

    async def request(self, method, url, headers=None, content=None):
        # Delegate to get() so subclasses that override only get() keep working
        # even though web_fetch now issues requests via client.request().
        return await self.get(url, headers=headers)


async def _run_web_fetch(preview=True):
    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=FakeClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/page", preview=preview)

    return result, captured


@pytest.mark.asyncio
async def test_web_fetch_archives_raw_html():
    _, captured = await _run_web_fetch()
    assert captured["raw_html"] == FakeResponse.text
    assert captured["content"]  # markdown produced


# ---------------------------------------------------------------------------
# _redact_html — BeautifulSoup DOM-based redaction
# ---------------------------------------------------------------------------


def test_redact_html_strips_scripts_and_styles():
    out = server._redact_html("<script>alert(1)</script><style>body{}</style><p>hi</p>")
    assert "alert" not in out
    assert "body{}" not in out
    assert "hi" in out


def test_redact_html_strips_hidden_inputs():
    out = server._redact_html(
        '<input type="hidden" name="csrf" value="token123">'
        '<input type="text" value="keepme">'
    )
    assert "token123" not in out
    assert "keepme" in out


def test_redact_html_strips_on_handlers():
    out = server._redact_html('<div onclick="evil()" onload="bad()">hi</div>')
    assert "onclick" not in out
    assert "onload" not in out
    assert "evil" not in out
    assert "hi" in out


def test_redact_html_preserves_legitimate_attrs():
    out = server._redact_html('<div conjunction="and" data-context="info">hi</div>')
    assert 'conjunction="and"' in out
    assert 'data-context="info"' in out
    assert "hi" in out


def test_redact_html_strips_javascript_uris():
    out = server._redact_html('<a href="javascript:alert(1)">link</a>')
    assert "javascript:" not in out
    assert "href" not in out.lower()


@pytest.mark.asyncio
async def test_redact_html_no_redact():
    html = "<html><body><script>var x = 1;</script><!-- c --></body></html>"

    class NoRedactResponse(FakeResponse):
        text = html

    class NoRedactClient(FakeClient):
        async def get(self, url, headers=None):
            return NoRedactResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=NoRedactClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/page", redact_html=False)

    assert captured["raw_html"] == html
    assert "<script>" in captured["raw_html"]
    assert "<!-- c -->" in captured["raw_html"]


# ---------------------------------------------------------------------------
# _redact_content — markdown/text secret sweeping
# ---------------------------------------------------------------------------


def test_redact_content_strips_bearer_tokens():
    out = server._redact_content("Token: Bearer abc123def456")
    assert "Bearer [REDACTED]" in out
    assert "abc123def456" not in out


def test_redact_content_strips_api_keys():
    out = server._redact_content("key sk-proj-1234567890abcdef1234567890abcdef here")
    assert "sk-[REDACTED]" in out
    assert "1234567890abcdef1234567890abcdef" not in out


@pytest.mark.asyncio
async def test_redact_content_in_web_fetch():
    json_body = '{"secret": "Bearer abc123def456"}'

    class JsonResponse(FakeResponse):
        headers = {"content-type": "application/json"}
        text = json_body

    class JsonClient(FakeClient):
        async def get(self, url, headers=None):
            return JsonResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=JsonClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/data")

    assert "Bearer [REDACTED]" in captured["content"]
    assert "abc123def456" not in captured["content"]


@pytest.mark.asyncio
async def test_web_fetch_preview_truncates():
    # Payload whose markdown exceeds 5000 chars so preview truncation is real.
    big_text = "<html><head><title>Big</title></head><body>" + ("<p>word </p>" * 3000) + "</body></html>"

    class BigResponse(FakeResponse):
        text = big_text

    class BigClient(FakeClient):
        async def get(self, url, headers=None):
            return BigResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=BigClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/big", preview=True)

    assert "... (truncated," in result
    assert len(captured["content"]) > 5000
    # The full content must not be included when previewing.
    assert captured["content"] not in result


@pytest.mark.asyncio
async def test_web_fetch_full_content_returns_all():
    # Build a fetch whose markdown exceeds 5000 chars to prove no truncation.
    big_text = "<html><head><title>Big</title></head><body>" + ("<p>word </p>" * 3000) + "</body></html>"

    class BigResponse(FakeResponse):
        text = big_text

    class BigClient(FakeClient):
        async def get(self, url, headers=None):
            return BigResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=BigClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/big", preview=False)

    assert "Archived to: entry.jsonl" in result
    assert "truncated" not in result
    assert captured["content"] in result
    assert len(captured["content"]) > 5000


@pytest.mark.asyncio
async def test_web_fetch_full_content_respects_cap():
    # Markdown produced from this body exceeds 1M chars (still under 10MB cap).
    big_text = "<html><head><title>Huge</title></head><body>" + ("<p>word </p>" * 200_000) + "</body></html>"

    class HugeResponse(FakeResponse):
        text = big_text

    class HugeClient(FakeClient):
        async def get(self, url, headers=None):
            return HugeResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=HugeClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/huge", preview=False)

    assert len(captured["content"]) > 1_000_000
    assert "truncated at 1M chars" in result
    assert captured["content"] not in result


@pytest.mark.asyncio
async def test_web_fetch_non_html_content_type():
    json_body = '{"key": "value"}'

    class JsonResponse(FakeResponse):
        headers = {"content-type": "application/json"}
        text = json_body

    class JsonClient(FakeClient):
        async def get(self, url, headers=None):
            return JsonResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=JsonClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/data")

    assert captured["raw_html"] is None
    # The JSON body is parsed and pretty-printed with 2-space indent.
    assert captured["content"] == '{\n  "key": "value"\n}'
    assert json.loads(captured["content"]) == json.loads(json_body)


@pytest.mark.asyncio
async def test_web_fetch_plus_json_content_type_pretty_prints():
    json_body = '{"@context": "https://schema.org", "name": "Example"}'

    class LdJsonResponse(FakeResponse):
        headers = {"content-type": "application/ld+json; charset=utf-8"}
        text = json_body

    class LdJsonClient(FakeClient):
        async def get(self, url, headers=None):
            return LdJsonResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=LdJsonClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/ld")

    assert captured["raw_html"] is None
    # application/ld+json (a +json media type) is parsed and pretty-printed.
    assert captured["content"] != json_body
    assert "\n  " in captured["content"]  # 2-space indent present
    assert json.loads(captured["content"]) == json.loads(json_body)


@pytest.mark.asyncio
async def test_web_fetch_invalid_json_falls_back_to_raw():
    invalid_body = "this is not json"

    class BadJsonResponse(FakeResponse):
        headers = {"content-type": "application/json"}
        text = invalid_body

    class BadJsonClient(FakeClient):
        async def get(self, url, headers=None):
            return BadJsonResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=BadJsonClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/badjson")

    assert captured["raw_html"] is None
    # Invalid JSON must be returned unchanged — no crash, no pretty-print attempt.
    assert captured["content"] == invalid_body


@pytest.mark.asyncio
async def test_web_fetch_preview_false_short_content():
    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=FakeClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/page", preview=False)

    assert "truncated" not in result
    assert captured["content"] in result


@pytest.mark.asyncio
async def test_web_fetch_dedup_keeps_raw_html(tmp_path):
    from web_archive_store import storage as storage_mod

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=FakeClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(storage_mod, "_default_dir", return_value=tmp_path),
    ):
        await server.web_fetch("https://example.com/page")
        await server.web_fetch("https://example.com/page")

    # A single archived entry persists with raw_html intact across the dedup.
    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    lines = [ln for ln in jsonl_files[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["raw_html"] == FakeResponse.text


@pytest.mark.asyncio
async def test_web_fetch_post_sends_body_and_content_type():
    class RecordingResponse(FakeResponse):
        headers = {"content-type": "application/json"}
        text = '{"ok": true}'

    class RecordingClient(FakeClient):
        async def request(self, method, url, headers=None, content=None):
            self.recorded = (method, url, content, headers)
            return RecordingResponse()

    client = RecordingClient()
    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["method"] = method
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=lambda *a, **k: client)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch(
            "https://example.com/api",
            method="POST",
            body='{"a":1}',
            content_type="application/json",
        )

    recorded_method, recorded_url, recorded_content, recorded_headers = client.recorded
    assert recorded_method == "POST"
    assert recorded_url == "https://example.com/api"
    assert recorded_content == '{"a":1}'
    assert recorded_headers["Content-Type"] == "application/json"
    assert recorded_headers.get("Authorization") is None
    assert captured["method"] == "POST"
    assert "Method: POST" in result


@pytest.mark.asyncio
async def test_web_fetch_unsupported_method():
    result = await server.web_fetch("https://example.com/page", method="PATCH-THING")
    assert "Unsupported HTTP method" in result
    assert "PATCH-THING" in result


@pytest.mark.asyncio
async def test_web_fetch_get_default_still_works():
    _, captured = await _run_web_fetch()
    assert captured["content"]  # default GET path via FakeClient.request still returns content


# ---------------------------------------------------------------------------
# archive_read — full_content option
# ---------------------------------------------------------------------------


def _write_entry(tmp_path, raw_html=None):
    fp = tmp_path / "2026-07-30-fetch-example.jsonl"
    entry = {
        "type": "fetch",
        "source": "https://example.com/x",
        "title": "Example",
        "content": "# Hello\n" + "x" * 1000,
        "timestamp": "2026-07-30T00:00:00+00:00",
        "content_hash": "abc",
    }
    if raw_html:
        entry["raw_html"] = raw_html
    fp.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return fp


def _run_archive_read(tmp_path, full_content=False):
    with patch.object(server, "read_entries", side_effect=lambda fid, max_entries=50: (
        read_entries(fid, base_dir=tmp_path, max_entries=max_entries)
    )):
        return server.archive_read("2026-07-30-fetch-example.jsonl", full_content=full_content)


def test_archive_read_preview_by_default(tmp_path):
    _write_entry(tmp_path, raw_html="<html>raw</html>")
    result = _run_archive_read(tmp_path)
    assert "Content preview:" in result
    assert "Raw HTML: available" in result
    assert "<html>raw</html>" not in result


def test_archive_read_full_content_includes_raw_html(tmp_path):
    _write_entry(tmp_path, raw_html="<html>raw</html>")
    result = _run_archive_read(tmp_path, full_content=True)
    assert "Content:" in result
    assert "<html>raw</html>" in result
    assert "Content preview:" not in result


def test_archive_read_full_content_no_raw_html(tmp_path):
    _write_entry(tmp_path)
    result = _run_archive_read(tmp_path, full_content=True)
    assert "Raw HTML: available" not in result
    assert "Content:" in result


def test_archive_read_full_content_caps_each_entry(tmp_path):
    fp = tmp_path / "2026-07-30-fetch-example.jsonl"
    entry = {
        "type": "fetch",
        "source": "https://example.com/x",
        "title": "Example",
        "content": "y" * 600_000,
        "timestamp": "2026-07-30T00:00:00+00:00",
        "content_hash": "abc",
        "raw_html": "<html>" + "z" * 600_000 + "</html>",
    }
    fp.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    result = _run_archive_read(tmp_path, full_content=True)

    assert "content truncated at 500k chars" in result
    assert "raw_html truncated at 500k chars" in result
    assert ("y" * 500_000) in result


def test_archive_read_full_content_default_max_entries(tmp_path):
    fp = tmp_path / "2026-07-30-fetch-example.jsonl"
    with open(fp, "a", encoding="utf-8") as f:
        for i in range(12):
            entry = {
                "type": "fetch",
                "source": f"https://example.com/{i}",
                "title": f"Title {i}",
                "content": f"# c{i}",
                "timestamp": "2026-07-30T00:00:00+00:00",
                "content_hash": str(i),
            }
            f.write(json.dumps(entry) + "\n")

    # Default with full_content -> 10 shown, not 50.
    result = _run_archive_read(tmp_path, full_content=True)
    assert "Entries: 10 shown" in result

    # An explicit max_entries value is honored.
    with patch.object(server, "read_entries", side_effect=lambda fid, max_entries=50: (
        read_entries(fid, base_dir=tmp_path, max_entries=max_entries)
    )):
        result2 = server.archive_read(
            "2026-07-30-fetch-example.jsonl", max_entries=3, full_content=True
        )
    assert "Entries: 3 shown" in result2


# ---------------------------------------------------------------------------
# archive_read — jq filter option
# ---------------------------------------------------------------------------


def _write_entry_kwargs(tmp_path, filename, **fields):
    """Write a single JSONL entry to `filename` in tmp_path.

    Accepts keyword args that override the default entry fields so tests can
    control type, title, source, etc. Returns the file path.
    """
    fp = tmp_path / filename
    entry = {
        "type": "fetch",
        "source": "https://example.com/x",
        "title": "Example",
        "content": "# Hello\n",
        "timestamp": "2026-07-30T00:00:00+00:00",
        "content_hash": "abc",
    }
    entry.update(fields)
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return fp


def _run_archive_read_jq(tmp_path, file_id, jq, **kwargs):
    with patch.object(server, "read_entries", side_effect=lambda fid, max_entries=50: (
        read_entries(fid, base_dir=tmp_path, max_entries=max_entries)
    )):
        return server.archive_read(file_id, jq=jq, **kwargs)


def test_archive_read_jq_select_field(tmp_path):
    _write_entry_kwargs(tmp_path, "2026-07-30-jq-select.jsonl", title="Test Title")
    result = _run_archive_read_jq(tmp_path, "2026-07-30-jq-select.jsonl", jq=".title")
    assert "Test Title" in result
    assert "matched" in result


def test_archive_read_jq_filter_by_type(tmp_path):
    file_id = "2026-07-30-jq-type.jsonl"
    _write_entry_kwargs(tmp_path, file_id, type="fetch", title="Fetch One")
    _write_entry_kwargs(tmp_path, file_id, type="search", title="Search One")
    result = _run_archive_read_jq(tmp_path, file_id, jq='select(.type == "fetch") | .title')
    assert "Fetch One" in result
    assert "Search One" not in result
    assert "1 of 2 entries matched" in result


def test_archive_read_jq_filter_out_all(tmp_path):
    file_id = "2026-07-30-jq-none.jsonl"
    _write_entry_kwargs(tmp_path, file_id, type="fetch")
    result = _run_archive_read_jq(tmp_path, file_id, jq='select(.type == "nonexistent")')
    assert "No entries matched" in result


def test_archive_read_jq_error(tmp_path):
    file_id = "2026-07-30-jq-error.jsonl"
    _write_entry_kwargs(tmp_path, file_id, title="Error Entry")
    result = _run_archive_read_jq(tmp_path, file_id, jq=".nonexistent | invalid_syntax[[[")
    assert "jq error" in result
    assert "Error Entry" in result


def test_archive_read_jq_null_output_skipped(tmp_path):
    file_id = "2026-07-30-jq-null.jsonl"
    _write_entry_kwargs(tmp_path, file_id, title="No Raw")
    result = _run_archive_read_jq(tmp_path, file_id, jq=".raw_html")
    assert "No entries matched" in result or "0 of 1 entries matched" in result
    assert "No Raw" not in result


def test_jq_env_is_empty(tmp_path):
    """jq must run with an empty env so it can't leak serve environment vars.

    With env={} the `env` builtin yields an empty object, so no real
    environment data should appear anywhere in the output.
    """
    file_id = "2026-07-30-jq-env.jsonl"
    _write_entry_kwargs(tmp_path, file_id, title="Env Entry")
    result = _run_archive_read_jq(tmp_path, file_id, jq="env")

    # jq `env` with empty env produces {} (not null/false/empty), so the entry
    # matches and the empty object is echoed.
    assert "Env Entry" in result
    assert "  {}\n" in result
    # No real environment variable NAME from the calling process may leak into
    # the output (names are distinct identifiers, unlike values which can
    # collide with the entry count).
    for name in os.environ:
        if name.isupper() and len(name) > 1:
            assert name not in result


def test_jq_timeout(tmp_path):
    """An infinitely-running jq filter must be killed and reported as timed out."""
    file_id = "2026-07-30-jq-timeout.jsonl"
    _write_entry_kwargs(tmp_path, file_id, title="Timeout Entry")

    real_run = subprocess.run

    # Force the jq subprocess timeout down to 1s so the test completes quickly,
    # while still exercising the real TimeoutExpired handling.
    def fast_timeout(*args, **kwargs):
        kwargs["timeout"] = 1
        return real_run(*args, **kwargs)

    with (
        patch.object(server, "read_entries", side_effect=lambda fid, max_entries=50: (
            read_entries(fid, base_dir=tmp_path, max_entries=max_entries)
        )),
        patch.object(server.subprocess, "run", side_effect=fast_timeout),
    ):
        result = server.archive_read(file_id, jq="range(infinite)")

    assert "timed out after 10s" in result


@pytest.mark.asyncio
async def test_raw_html_is_content_redacted():
    """raw_html passed to store must be secret-swept even when structurally redacted."""
    html = (
        "<html><head><title>Secrets</title></head>"
        "<body><p>Authorization: Bearer abc123def456</p></body></html>"
    )

    class SecretResponse(FakeResponse):
        text = html

    class SecretClient(FakeClient):
        async def get(self, url, headers=None):
            return SecretResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["raw_html"] = raw_html
        captured["content"] = content
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=SecretClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        await server.web_fetch("https://example.com/secrets")

    assert captured["raw_html"] is not None
    assert "Bearer [REDACTED]" in captured["raw_html"]
    assert "abc123def456" not in captured["raw_html"]


@pytest.mark.asyncio
async def test_title_is_redacted():
    """A secret embedded in the <title> must not leak into the stored title."""
    html = "<html><head><title>key sk-proj-1234567890abcdef</title></head><body><p>hi</p></body></html>"

    class TitleResponse(FakeResponse):
        text = html

    class TitleClient(FakeClient):
        async def get(self, url, headers=None):
            return TitleResponse()

    captured = {}

    def fake_store(entry_type, source, title, content, base_dir=None, raw_html=None, method=None):
        captured["title"] = title
        return Path("entry.jsonl"), True

    with (
        patch.object(server, "httpx", MagicMock(AsyncClient=TitleClient)),
        patch.object(server, "validate_url", return_value=None),
        patch.object(server, "store", side_effect=fake_store),
    ):
        result = await server.web_fetch("https://example.com/title")

    assert "[REDACTED]" in captured["title"]
    assert "sk-proj-1234567890abcdef" not in captured["title"]
    assert "sk-[REDACTED]" in captured["title"]
    # The response header/title line shown to the user is redacted too.
    assert "sk-proj-1234567890abcdef" not in result
