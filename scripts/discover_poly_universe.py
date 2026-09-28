"""Discover and freeze a Polymarket token universe from Gamma's keyset API.

This is a data-collection step, not a research-input builder.  The resulting
manifest is intended to stay in a private data directory.  It records only
pre-registered market identity and timing fields needed to replay price
history; raw API responses are never committed to the repository.
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
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_ENDPOINT = "https://gamma-api.polymarket.com/markets/keyset"


def _get_json(url: str, *, retries: int = 4) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "poly-universe-freezer/0.1"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 >= retries:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Gamma HTTP {exc.code}: {body[:500]}") from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except (TypeError, ValueError):
                delay = 2**attempt
            time.sleep(max(0.0, delay))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 >= retries:
                raise RuntimeError(f"Gamma request failed: {exc}") from exc
            time.sleep(2**attempt)
    raise RuntimeError(f"Gamma request failed: {last}")


def _parse_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _iso(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _market_record(market: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tokens = _parse_tokens(market.get("clobTokenIds"))
    outcomes = _parse_tokens(market.get("outcomes"))
    market_id = str(market.get("id", ""))
    base = {
        "market_id": market_id,
        "condition_id": market.get("conditionId"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "start_date": _iso(market.get("startDate")),
        "end_date": _iso(market.get("endDate")),
        "created_at": _iso(market.get("createdAt")),
        "updated_at": _iso(market.get("updatedAt")),
        "active": market.get("active"),
        "closed": market.get("closed"),
        "archived": market.get("archived"),
        "outcomes": outcomes,
        "token_ids": tokens,
    }
    token_rows = [
        {
            "token_id": token,
            "market_id": market_id,
            "condition_id": market.get("conditionId"),
            "question": market.get("question"),
            "slug": market.get("slug"),
            "outcome": outcomes[index] if index < len(outcomes) else None,
            "start_date": base["start_date"],
            "end_date": base["end_date"],
            "created_at": base["created_at"],
            "metadata_status": "gamma_snapshot_at_discovery",
        }
        for index, token in enumerate(tokens)
    ]
    return base, token_rows


def discover(
    output: str | pathlib.Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    start_date_min: str | None = None,
    start_date_max: str | None = None,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
    sleep_seconds: float = 0.05,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be in [1,100]")
    target = pathlib.Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    markets: list[dict[str, Any]] = []
    tokens_by_id: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        params: dict[str, Any] = {
            "limit": page_size,
            "order": "id",
            "ascending": "false",
        }
        if start_date_max:
            params["start_date_max"] = start_date_max
        if start_date_min:
            params["start_date_min"] = start_date_min
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
        pages += 1
        for market in rows:
            if not isinstance(market, Mapping):
                continue
            market_row, token_rows = _market_record(market)
            if not market_row["market_id"]:
                continue
            markets.append(market_row)
            for token_row in token_rows:
                tokens_by_id.setdefault(token_row["token_id"], token_row)
        if max_pages is not None and pages >= max_pages:
            break
        next_cursor = payload.get("next_cursor")
        if not rows or not next_cursor:
            break
        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            raise RuntimeError("Gamma keyset cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    markets.sort(key=lambda row: (str(row.get("market_id")), str(row.get("slug"))))
    tokens = sorted(tokens_by_id.values(), key=lambda row: row["token_id"])
    result = {
        "manifest_version": "poly_token_universe.v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "discovery_endpoint": endpoint,
        "discovery_query": {
            "page_size": page_size,
            "order": "id",
            "ascending": False,
            "start_date_max": start_date_max,
            "start_date_min": start_date_min,
            "end_date_min": end_date_min,
            "end_date_max": end_date_max,
        },
        "pages": pages,
        "market_count": len(markets),
        "token_count": len(tokens),
        "strict_pit_eligible": False,
        "metadata_status": "Gamma snapshot at discovery; historical rule/version visibility requires private audit",
        "markets": markets,
        "tokens": tokens,
    }
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    result["manifest_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--start-date-max", help="exclude markets starting after this RFC3339 date")
    parser.add_argument("--start-date-min", help="exclude markets starting before this RFC3339 date")
    parser.add_argument("--end-date-min", help="exclude markets ending before this RFC3339 date")
    parser.add_argument("--end-date-max", help="exclude markets ending after this RFC3339 date")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    args = parser.parse_args(argv)
    result = discover(
        args.out,
        endpoint=args.endpoint,
        start_date_min=args.start_date_min,
        start_date_max=args.start_date_max,
        end_date_min=args.end_date_min,
        end_date_max=args.end_date_max,
        page_size=args.page_size,
        max_pages=args.max_pages,
        sleep_seconds=args.sleep_seconds,
    )
    print(json.dumps({k: result[k] for k in ("pages", "market_count", "token_count", "manifest_sha256")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
