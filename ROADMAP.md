# web-archive-mcp — Roadmap

## v0.2 — Production Readiness

### P0: Unit & Integration Tests
**Effort**: 1-2 days

No test coverage exists. Minimum viable suite before shipping:

```
tests/
├── conftest.py              # pytest-asyncio, tmp_path, httpx mocks
├── test_storage.py          # 15+ tests
│   ├── store/dedup (fetch + search)
│   ├── concurrent writes
│   ├── path traversal rejection
│   ├── list_files edge cases
│   └── read_entries corrupt JSON
├── test_server.py           # 15+ tests
│   ├── web_fetch happy/error/timeout/size-limit
│   ├── web_search happy/empty/captcha
│   ├── archive_list date filters
│   ├── archive_read traversal rejection
│   └── rebuild binary-missing/timeout
└── test_validation.py       # 5 tests
    ├── SSRF URL validation (loopback, private, multicast)
    ├── DDG URL decoding
    └── HTML/markdown helpers
```

Target: 35+ tests, all passing, CI enabled.

### P1: Concurrent Write Protection
**Effort**: half day

Multiple MCP tools calling `store()` simultaneously can interleave JSONL lines.

- Use `fcntl.flock` (or `filelock` library) around the read-check-write section
- Write each entry as a single atomic write (binary fd + `os.write`)
- Add a `test_concurrency.py` that spawns 10 threads hammering `store()` and verifies no interleaved lines

### P1: Async Rebuild
**Effort**: half day

`rebuild()` currently calls `subprocess.run()` synchronously, potentially blocking the event loop for 120s.

- Make `rebuild` async
- Use `asyncio.create_subprocess_exec`
- Stream stdout/stderr to provide progress feedback
- Include both stdout and stderr in success/failure output

### P2: Robust HTML Parsing for DuckDuckGo
**Effort**: 1 day

The current regex parser works but is brittle. DDG can change class names, add CAPTCHAs, or reorder attributes.

- Add `beautifulsoup4` as optional dependency
- Use CSS selectors: `soup.select(".result__a")`, `soup.select(".result__snippet")`
- Fall back to regex if bs4 is unavailable
- Add integration test with a captured DDG HTML fixture
- Detect and surface DDG rate-limit/CAPTCHA signals explicitly

---

## v0.3 — Hardening

### P2: Redirect Validation in Web Fetch
**Effort**: half day

Currently validates the *initial* URL but doesn't re-validate after redirects (DNS rebinding attack).

- Hook into httpx redirect events
- Re-resolve and re-validate the redirect target's IP before following
- Block if redirect targets internal addresses

### P2: Per-File Size Limits
**Effort**: 1 hour

A single JSONL file can grow unbounded over time (same slug, different days = many entries).

- Cap entries per file (e.g., 1000)
- When exceeded, start a new file with a sequential suffix
- OR: add a configurable `--max-file-size` / `--max-file-entries`

### P3: Configurable Archive Directory
**Effort**: 1 hour

Currently hardcoded to `~/.local/share/web-archive`.

- Respect `WEB_ARCHIVE_DIR` environment variable
- Respect `XDG_DATA_HOME`
- Accept `--archive-dir` CLI flag
- Thread through to all storage functions

### P3: Content-Type Handling
**Effort**: 1 hour

`application/xhtml+xml` responses bypass markdown conversion. PDF, JSON, and other structured formats could benefit from type-specific extractors.

- `application/xhtml+xml` → same path as `text/html`
- `application/json` → pretty-print and store
- `text/plain` → store as-is (already works but could strip control chars)

### P3: Logging
**Effort**: 1 hour

No logging anywhere — errors are strings returned to the model.

- Add `logging` with configurable level
- Log fetches (URL, size, status) and searches (query, result count)
- Log storage events (new/duplicate, file path)
- Keep error responses generic; log details server-side only

---

## v0.4 — Ecosystem

### Dedup Across Days
Currently dedup only checks today's file. A URL fetched yesterday and re-fetched today creates a new file.

- Scan the most recent N files for the same slug on each `store()` call
- OR: maintain a lightweight content-hash index (`hashes.jsonl`) checked per-write
- Tradeoff: I/O cost of scanning vs. storage cost of duplicates

### Auto-Index on Write
Currently requires manual `rebuild()` call. Could auto-trigger after N new entries.

- Debounced background indexer
- Configurable threshold (e.g., after every 10 new entries or 5 minutes)
- Respect `--no-auto-index` flag for batching

### MCP Resource Exposure
Expose the archive as MCP resources for direct client browsing.

- `web-archive://recent` — last N entries
- `web-archive://search?q=...` — quick search without unified-history-mcp
- `web-archive://domains` — list of unique domains fetched

---

## Priorities at a glance

| Priority | Item | Effort |
|---|---|---|
| 🔴 P0 | Unit & integration tests | 1-2 days |
| 🔴 P1 | Concurrent write protection | half day |
| 🔴 P1 | Async rebuild | half day |
| 🟡 P2 | Robust DDG parsing (bs4) | 1 day |
| 🟡 P2 | Redirect validation | half day |
| 🟡 P2 | Per-file size limits | 1 hour |
| 🟢 P3 | Configurable archive dir | 1 hour |
| 🟢 P3 | Content-type handling | 1 hour |
| 🟢 P3 | Logging | 1 hour |
| ⚪ v0.4 | Cross-day dedup | 1 day |
| ⚪ v0.4 | Auto-index on write | 1 day |
| ⚪ v0.4 | MCP resource exposure | half day |
