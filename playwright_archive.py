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
import sys
from pathlib import Path

# Make web-archive-mcp's storage module importable regardless of CWD.
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))

from web_archive_mcp import playwright_recorder  # noqa: E402


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
    ap.add_argument("--archive", default=str(playwright_recorder.default_archive_dir()),
                    help="archive directory")
    args = ap.parse_args()

    if args.max_entries < 1:
        ap.error("--max-entries must be >= 1")
    if args.max_body < 1:
        ap.error("--max-body must be >= 1")

    try:
        result = playwright_recorder.record_session(
            args.urls,
            wait=args.wait, timeout=args.timeout, max_body=args.max_body,
            max_entries=args.max_entries, executable=args.executable,
            headful=args.headful, redact_auth=args.redact_auth,
            archive_dir=Path(args.archive), progress=lambda line: print(line, flush=True),
        )
    except RuntimeError as e:
        sys.exit(f"Error: {e}")

    if result["limit"]:
        print(f"\nStopped early: hit max_entries ({args.max_entries}).")
    for err in result["errors"]:
        print(f"  !! {err}")
    print(f"\nDone. {result['count']} request/response recorded "
          f"({result['new']} new, {result['dup']} in-run dupes skipped).")
    print("Run web_archive_rebuild to make them searchable.")


if __name__ == "__main__":
    main()
