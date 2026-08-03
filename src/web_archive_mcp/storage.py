"""Archive storage — JSONL writer with content-addressed dedup."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _default_dir() -> Path:
    """Default archive directory: ~/.local/share/web-archive"""
    return Path.home() / ".local" / "share" / "web-archive"


def _archive_path(base: Path, entry_type: str, slug: str, ts: datetime) -> Path:
    """Build a path like YYYY-MM-DD-{type}-{slug}.jsonl"""
    date_str = ts.strftime("%Y-%m-%d")
    safe_slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug)[:60]
    return base / f"{date_str}-{entry_type}-{safe_slug}.jsonl"


def _content_hash(content: str) -> str:
    """SHA-256 of content for dedup."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _is_duplicate(filepath: Path, source_key: str, content: str) -> bool:
    """Check if this exact source+content combination already exists in today's file.

    The ``source_key`` is compared against the stored ``source`` field, which
    always holds the URL (for fetches) or query string (for searches).
    """
    if not filepath.exists():
        return False

    ch = _content_hash(content)
    try:
        for line in filepath.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("source") == source_key and entry.get("content_hash") == ch:
                return True
    except OSError:
        return False
    return False


def _backfill_raw_html(filepath: Path, source_key: str, content: str, raw_html: str) -> None:
    """Atomic in-place add of raw_html to the existing duplicate entry.

    Writes to a temp file then os.replace() — never truncates the original
    in place.  Failures are silent (best-effort); the duplicate is still
    reported regardless.
    """
    ch = _content_hash(content)
    try:
        lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    changed = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if entry.get("source") == source_key and entry.get("content_hash") == ch:
            if not entry.get("raw_html"):
                entry["raw_html"] = raw_html
                lines[idx] = json.dumps(entry, ensure_ascii=False)
                changed = True
            break

    if not changed:
        return

    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, filepath)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def store(
    entry_type: str,
    source: str,
    title: str,
    content: str,
    base_dir: Optional[Path] = None,
    raw_html: Optional[str] = None,
) -> tuple[Path, bool]:
    """Persist a web fetch or search result. Returns (path, is_new).

    Args:
        entry_type: "fetch" or "search"
        source: URL for fetches, query string for searches
        title: Page title or search query label
        content: Markdown/text content
        base_dir: Archive directory (default: ~/.local/share/web-archive)
        raw_html: Optional raw HTML body to persist alongside the markdown.
            Dedup is still based on ``content`` only.

    Returns:
        (filepath, is_new) — is_new is False if this was a duplicate
    """
    base = base_dir or _default_dir()
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)

    ts = datetime.now(timezone.utc)
    slug = source.split("://")[-1] if "://" in source else source
    slug = slug.split("/")[0] if "/" in slug else slug
    slug = slug[:40]

    filepath = _archive_path(base, entry_type, slug, ts)

    if _is_duplicate(filepath, source, content):
        if raw_html is not None and raw_html.strip():
            _backfill_raw_html(filepath, source, content, raw_html)
        return filepath, False

    entry = {
        "type": entry_type,
        "source": source,
        "title": title,
        "content": content,
        "timestamp": ts.isoformat(),
        "content_hash": _content_hash(content),
    }
    if raw_html is not None and raw_html.strip():
        entry["raw_html"] = raw_html

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    os.chmod(filepath, 0o600)

    return filepath, True


def list_files(
    base_dir: Optional[Path] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """List archive files with metadata."""
    base = base_dir or _default_dir()
    if not base.is_dir():
        return []

    from datetime import date

    d_from = None
    d_to = None
    if date_from:
        try:
            d_from = date.fromisoformat(date_from)
        except ValueError:
            pass
    if date_to:
        try:
            d_to = date.fromisoformat(date_to)
        except ValueError:
            pass

    files = []
    for f in sorted(base.glob("*.jsonl"), reverse=True):
        if not f.is_file():
            continue

        fname = f.name
        fd = None
        if len(fname) >= 10:
            try:
                fd = date.fromisoformat(fname[:10])
            except ValueError:
                pass
            if d_from and fd and fd < d_from:
                continue
            if d_to and fd and fd > d_to:
                continue

        entry_count = 0
        summary = ""
        first_line = None
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    if first_line is None:
                        first_line = line
                    entry_count += 1
        except (OSError, UnicodeDecodeError):
            entry_count = 0

        if first_line:
            try:
                first = json.loads(first_line)
                summary = first.get("title", "")[:80]
            except (json.JSONDecodeError, KeyError):
                pass

        files.append({
            "name": f.name,
            "date": fname[:10] if fname[:10].count("-") == 2 else "?",
            "entries": entry_count,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "summary": summary,
        })

    return files


def read_entries(
    file_id: str,
    base_dir: Optional[Path] = None,
    max_entries: int = 50,
) -> tuple[list[dict], Path | None]:
    """Read entries from an archive file. Rejects path traversal."""
    base = base_dir or _default_dir()

    if not file_id or file_id.isspace():
        return [], None
    if "/" in file_id or "\\" in file_id:
        return [], None
    if ".." in file_id:
        return [], None
    if not file_id.endswith(".jsonl"):
        return [], None

    target = (base / file_id).resolve()
    if base.resolve() not in target.parents and target != base.resolve():
        return [], None

    if not target.is_file():
        return [], None

    try:
        lines = [
            ln.strip()
            for ln in target.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    except (OSError, UnicodeDecodeError):
        return [], None

    lines.reverse()
    lines = lines[:max_entries]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return entries, target
