"""Cache official Polymarket history locally for repeatable replay.

The cache contains raw API envelopes and a manifest.  It is deliberately
separate from the research input: the model only receives a generated cutoff
snapshot, while the evaluator can retain the full historical cache.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


ENDPOINT = "https://data-api.polymarket.com/v2/prices-history"


def _get_json(url: str, *, retries: int = 3) -> tuple[dict[str, Any], dict[str, str]]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "poly-ashare-cache/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                headers = {str(k): str(v) for k, v in response.headers.items()}
            return json.loads(body.decode("utf-8")), headers
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 503) or attempt + 1 >= retries:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except (TypeError, ValueError):
                delay = 2**attempt
            time.sleep(max(0.0, delay))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 >= retries:
                raise RuntimeError(f"fetch failed: {url}: {exc}") from exc
            time.sleep(2**attempt)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_tokens(path: pathlib.Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    records = obj.get("tokens", obj) if isinstance(obj, Mapping) else obj
    if not isinstance(records, list):
        raise ValueError("manifest must contain a tokens list")
    return [dict(x) for x in records]


def fetch_token(token_id: str, *, endpoint: str, bucket_seconds: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"token_id": token_id, "interval": "max", "bucket_seconds": bucket_seconds, "limit": 10000}
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        url = f"{endpoint}?{query}"
        payload, headers = _get_json(url)
        pages.append(payload)
        requests.append({"url": url, "params": params, "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "trace_id": headers.get("x-trace-id"), "count": len(payload.get("data", []))})
        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("next_cursor")
        if not pagination.get("has_more") or not next_cursor:
            break
        if str(next_cursor) == cursor:
            raise RuntimeError(f"repeated cursor for token {token_id}")
        cursor = str(next_cursor)
    return pages, requests


def main() -> int:
    ap = argparse.ArgumentParser(description="Cache Polymarket price history locally")
    ap.add_argument("--manifest", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--bucket-seconds", type=int, default=43200)
    ap.add_argument("--dry-run", action="store_true", help="only create the cache plan; do not call the API")
    args = ap.parse_args()
    records = load_tokens(args.manifest)
    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    enriched: list[dict[str, Any]] = []
    for record in records:
        token_id = str(record["token_id"])
        filename = f"token_{token_id}_max_{args.bucket_seconds}s.json"
        raw_path = raw_dir / filename
        rec = dict(record)
        rec["raw_file"] = str(pathlib.Path("raw") / filename)
        if not args.dry_run:
            pages, requests = fetch_token(token_id, endpoint=args.endpoint, bucket_seconds=args.bucket_seconds)
            combined: dict[str, Any] = {
                "data": [point for page in pages for point in page.get("data", [])],
                "pagination": {"has_more": False, "next_cursor": None, "pages": len(pages)},
                "provenance": {"requests": requests, "endpoint": args.endpoint, "token_id": token_id},
            }
            raw_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
            rec["raw_sha256"] = _sha(raw_path)
            rec["point_count"] = len(combined["data"])
        enriched.append(rec)
    cache_manifest = {
        "cache_schema_version": "poly_history_cache.v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_manifest": str(args.manifest.resolve()),
        "endpoint": args.endpoint,
        "bucket_seconds": args.bucket_seconds,
        "dry_run": bool(args.dry_run),
        "tokens": enriched,
    }
    manifest_path = args.out / "manifest_with_cache.json"
    manifest_path.write_text(json.dumps(cache_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PLAN_ONLY" if args.dry_run else "CACHED", "manifest": str(manifest_path), "tokens": len(enriched)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
