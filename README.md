# web-archive-mcp

MCP server for persistent web fetch and search archiving. Every `web_fetch` and `web_search` result is saved as timestamped JSONL, indexed by [fst-indexer](https://github.com/palimpsest-labs/fst-indexer), and searchable via [unified-history-mcp](https://github.com/palimpsest-labs/unified-history-mcp).

Part of the [Palimpsest](https://github.com/palimpsest-labs/palimpsest) investigative toolkit.

## Why

`web_fetch` and `web_search` results normally evaporate when a session ends. Pages change, get deleted, or get memory-holed. This closes that gap — every result is persisted, content-addressed for dedup, and fed into the same search pipeline as your session logs and transcripts. Three months later, a `search(domain="all", query="target name")` still finds the page that's been 404'd since July.

## Architecture

```
web_fetch / web_search          playwright-archive-mcp (browser capture)
        │                                │
        ▼                                ▼
  web-archive-store  (shared JSONL write-path + SSRF URL validation)
        │
        ▼
  ~/.local/share/web-archive/*.jsonl
        │                                │
        │                        fst-indexer (Jsonl extractor)
        │                                │
        ▼                                ▼
  returns content              index.fst + manifest.json
                                       │
                                       ▼
                              unified-history-mcp
                              domain: "web-archive"
```

## Tools

| Tool | Description |
|---|---|
| `web_fetch(url, timeout)` | Fetch a URL, convert to markdown, persist, return |
| `web_search(query)` | Search the web (DuckDuckGo), persist results |
| `archive_list(date_from, date_to, max)` | List archived entries with metadata |
| `archive_read(id, max_entries)` | Read entries from an archive file |
| `rebuild` | Rebuild FST index for the web-archive domain |

## Installation

`web-archive-mcp` depends on the shared [`web-archive-store`](https://github.com/palimpsest-labs/web-archive-store) package (archive write-path + SSRF URL validation). Install it first:

```bash
git clone https://github.com/palimpsest-labs/web-archive-store
cd web-archive-store
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cd ..
git clone https://github.com/palimpsest-labs/web-archive-mcp
cd web-archive-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Once `web-archive-store` is published to PyPI this becomes a single `pip install -e .`.

> Browser-driven traffic capture (the `playwright_*` tools) moved to the
> separate [`playwright-archive-mcp`](https://github.com/palimpsest-labs/playwright-archive-mcp)
> server, which records HTTP traffic into the same store.

## Integration with unified-history-mcp

Add to your unified-history TOML config:

```toml
[domains.web-archive]
dir = "~/.local/share/web-archive"
pattern = "*.jsonl"
extractor = "jsonl"
label = "web-archive entry"
filters = []
```

Then rebuild: `search(domain="web-archive", query="rebuild")` or call `rebuild` directly.

Once indexed, a `search(domain="all", query="your search")` scans your sessions, transcripts, notifications, **and** every web page you've ever fetched — in a single query.

## Entry format

```json
{
  "type": "fetch",
  "source": "https://example.com/page",
  "title": "Example Page",
  "content": "# Example\n\nMarkdown content...",
  "timestamp": "2026-07-30T21:15:00Z",
  "content_hash": "abc123..."
}
```

For searches, `source` holds the query string and `type` is `"search"`.

Content-addressed dedup prevents storing identical entries. Same source + same content hash = skipped.

## License

MIT
