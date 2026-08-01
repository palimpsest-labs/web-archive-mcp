"""Unit tests for playwright_archive.py pure logic (no browser, no disk store)."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from web_archive_mcp import playwright_recorder as pa  # noqa: E402


class FakeStore:
    """Minimal stand-in for web_archive_mcp.storage.store()."""

    def __init__(self):
        self.calls = []

    def store(self, entry_type, source, title, content, base_dir):
        self.calls.append({
            "entry_type": entry_type,
            "source": source,
            "title": title,
            "content": content,
            "base_dir": Path(base_dir),
        })
        return Path(base_dir) / "entry.jsonl", True


# --- _body_text -------------------------------------------------------------

@pytest.mark.parametrize("body,content_type,expected", [
    (b"hello", "text/plain", "hello"),
    (b"<html>", "text/html; charset=utf-8", "<html>"),
    (b"\xff\xfe binary", "image/png", None),                 # binary image
    (b"\xff\xfe binary", "application/octet-stream", None),  # octet-stream
    (b"stream", "text/event-stream", None),                  # streaming
    (b"a", "font/woff2", None),                              # font
    ("already str", "application/json", "already str"),
    (None, "text/html", None),
    (b"", "text/html", None),
    (b"\xc3\xa9", "text/plain", "\u00e9"),                   # valid utf-8
    (b"\xff\xff", "text/plain", None),                       # invalid utf-8
])
def test_body_text(body, content_type, expected):
    assert pa._body_text(body, content_type) == expected


def test_body_text_skips_before_fetching_for_binary_types():
    # A non-bytes body with a skip type must be rejected by type, not decoded.
    assert pa._body_text("not-really-binary", "image/png") is None


# --- _redact_headers --------------------------------------------------------

def test_redact_headers_removes_secrets():
    h = {
        "Authorization": "Bearer secret",
        "X-Api-Key": "k",
        "Cookie": "s=1",
        "Content-Type": "application/json",
        "X-Other": "keep",
    }
    r = pa._redact_headers(h)
    assert r["Authorization"] == "[REDACTED]"
    assert r["X-Api-Key"] == "[REDACTED]"
    assert r["Cookie"] == "[REDACTED]"
    assert r["Content-Type"] == "application/json"
    assert r["X-Other"] == "keep"
    assert "secret" not in str(r)


def test_redact_headers_case_insensitive():
    r = pa._redact_headers({"SET-COOKIE": "sid=abc", "authorization": "x"})
    assert r["SET-COOKIE"] == "[REDACTED]"
    assert r["authorization"] == "[REDACTED]"


# --- _build_content ---------------------------------------------------------

def test_build_content_structure():
    content = pa._build_content("GET", "https://e.com/", {"A": "B"}, "q=1", 200, {"C": "D"}, "resp")
    assert content.startswith("GET https://e.com/")
    assert "STATUS 200" in content
    assert "REQUEST HEADERS" in content
    assert "    A: B" in content
    assert "REQUEST BODY" in content
    assert "q=1" in content
    assert "RESPONSE BODY" in content


def test_build_content_marks_empty_body():
    content = pa._build_content("GET", "https://e.com/", {}, "", 204, {}, "")
    assert "RESPONSE BODY" in content
    assert "(non-text or empty)" in content


# --- _record -----------------------------------------------------------------

def test_record_delegates_to_store():
    store = FakeStore()
    path, is_new = pa._record(store, Path("/tmp/x"), "POST", "https://e.com/a",
                              {"X": "1"}, "body", 201, {"Y": "2"}, "resp", 100)
    assert is_new is True
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["entry_type"] == "request"
    assert call["source"] == "https://e.com/a"
    assert call["title"] == "POST https://e.com/a -> 201"
    assert call["base_dir"] == Path("/tmp/x")


def test_record_truncates_bodies():
    store = FakeStore()
    big = "a" * 5000
    pa._record(store, Path("/tmp/y"), "GET", "https://e.com/", {}, big, 200, {}, big, 100)
    content = store.calls[0]["content"]
    assert "a" * 100 in content
    assert "a" * 101 not in content


# --- normalize_url ----------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("example.com", "https://example.com"),
    ("https://example.com", "https://example.com"),
    ("http://example.com", "http://example.com"),
    ("  example.com  ", "https://example.com"),
])
def test_normalize_url_ok(url, expected):
    assert pa.normalize_url(url) == expected


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "data:text/plain,hi",
    "ftp://example.com",
])
def test_normalize_url_rejects_unsupported_schemes(bad):
    with pytest.raises(ValueError):
        pa.normalize_url(bad)
