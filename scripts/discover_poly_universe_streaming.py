"""Resume-safe Gamma keyset discovery backed by SQLite.

The ordinary discovery helper is convenient for small probes.  Use this
variant for the full catalog: every page is committed before the next request,
so a network interruption does not discard the discovered universe or require
holding all market metadata in Python memory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from discover_poly_universe import _get_json, _market_record, DEFAULT_ENDPOINT


def _init_db(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS markets(market_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS tokens(token_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()
    return con


def _state(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM state WHERE key=?", [key]).fetchone()
    return str(row[0]) if row else default


def _set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", [key, value])


def discover_streaming(
    db_path: str | pathlib.Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    start_date_min: str | None = None,
    start_date_max: str | None = None,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    page_size: int = 20,
    sleep_seconds: float = 0.1,
    max_pages: int | None = None,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be in [1,100]")
    db_path = pathlib.Path(db_path)
    con = _init_db(db_path)
    try:
        cursor = _state(con, "next_cursor")
        pages = int(_state(con, "pages", "0") or 0)
        done = _state(con, "done", "0") == "1"
        while not done:
            params: dict[str, Any] = {"limit": page_size, "order": "id", "ascending": False}
            if start_date_min:
                params["start_date_min"] = start_date_min
            if start_date_max:
                params["start_date_max"] = start_date_max
            if end_date_min:
                params["end_date_min"] = end_date_min
            if end_date_max:
                params["end_date_max"] = end_date_max
            if cursor:
                params["after_cursor"] = cursor
            url = f"{endpoint}?{urllib.parse.urlencode(params)}"
            payload = _get_json(url)
            rows = payload.get("markets", [])
            if not isinstance(rows, list):
                raise ValueError("Gamma keyset response markets must be a list")
            for market in rows:
                if not isinstance(market, Mapping):
                    continue
                market_row, token_rows = _market_record(market)
                if not market_row["market_id"]:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO markets(market_id,payload) VALUES(?,?)",
                    [market_row["market_id"], json.dumps(market_row, ensure_ascii=False)],
                )
                for token_row in token_rows:
                    con.execute(
                        "INSERT OR IGNORE INTO tokens(token_id,payload) VALUES(?,?)",
                        [token_row["token_id"], json.dumps(token_row, ensure_ascii=False)],
                    )
            pages += 1
            next_cursor = payload.get("next_cursor")
            done = not rows or not next_cursor or (max_pages is not None and pages >= max_pages)
            _set_state(con, "pages", str(pages))
            _set_state(con, "next_cursor", str(next_cursor or ""))
            _set_state(con, "done", "1" if done else "0")
            con.commit()
            if done:
                break
            cursor = str(next_cursor)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        result = {
            "manifest_version": "poly_token_universe.sqlite.v1",
            "db_path": str(db_path),
            "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "discovery_endpoint": endpoint,
            "discovery_query": {
                "page_size": page_size,
                "order": "id",
                "ascending": False,
                "start_date_min": start_date_min,
                "start_date_max": start_date_max,
                "end_date_min": end_date_min,
                "end_date_max": end_date_max,
            },
            "pages": pages,
            "market_count": con.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
            "token_count": con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0],
            "complete": done,
            "strict_pit_eligible": False,
            "metadata_status": "Gamma snapshot at discovery; historical rules require private audit",
        }
        return result
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--start-date-min")
    parser.add_argument("--start-date-max")
    parser.add_argument("--end-date-min")
    parser.add_argument("--end-date-max")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args(argv)
    result = discover_streaming(
        args.db,
        endpoint=args.endpoint,
        start_date_min=args.start_date_min,
        start_date_max=args.start_date_max,
        end_date_min=args.end_date_min,
        end_date_max=args.end_date_max,
        page_size=args.page_size,
        sleep_seconds=args.sleep_seconds,
        max_pages=args.max_pages,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
