"""Build frozen A-share trading-day cutoffs for 09:25 Asia/Shanghai.

The script reads only the A-share partition directory names and, optionally,
private cached Polymarket JSON files to determine the overlap bounds.  It does
not read prices into the research input and never calls the model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from collections.abc import Iterable, Sequence
from typing import Any


LOCAL_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
BUCKET_SECONDS = 43_200


def _date_dirs(ashare_root: pathlib.Path) -> list[dt.date]:
    # Expected layout: schema_version=daily_1min_v1/YYYY/YYYYMMDD/part-*.parquet
    candidates = ashare_root.glob("schema_version=daily_1min_v1/*/*")
    dates: list[dt.date] = []
    for path in candidates:
        if not path.is_dir() or len(path.name) != 8 or not path.name.isdigit():
            continue
        try:
            dates.append(dt.datetime.strptime(path.name, "%Y%m%d").date())
        except ValueError:
            continue
    return sorted(set(dates))


def _iter_points(raw_dir: pathlib.Path) -> Iterable[dict[str, Any]]:
    for path in sorted(raw_dir.glob("**/*.json")):
        if path.name == "manifest_with_cache.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        points = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(points, list):
            continue
        for point in points:
            if isinstance(point, dict):
                yield point


def _poly_bounds(raw_dir: pathlib.Path) -> tuple[int, int] | None:
    min_end: int | None = None
    max_timestamp: int | None = None
    for point in _iter_points(raw_dir):
        try:
            timestamp = int(point["timestamp"])
            resolution = int(point["resolution_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if resolution <= 0:
            continue
        point_end = timestamp + resolution
        min_end = point_end if min_end is None else min(min_end, point_end)
        max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)
    if min_end is None or max_timestamp is None:
        return None
    # A cutoff on the date of the latest bucket start can still use that
    # bucket's preceding completed observation.  Bound by one bucket span.
    return min_end, max_timestamp + BUCKET_SECONDS


def _cutoff_epoch(trade_date: dt.date) -> int:
    local = dt.datetime.combine(trade_date, dt.time(9, 25), tzinfo=LOCAL_TZ)
    return int(local.astimezone(dt.timezone.utc).timestamp())


def build_schedule(
    ashare_root: str | pathlib.Path,
    output: str | pathlib.Path,
    *,
    raw_dir: str | pathlib.Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    dates = _date_dirs(pathlib.Path(ashare_root))
    if not dates:
        raise FileNotFoundError("no A-share trading-date directories found")
    if start_date:
        dates = [d for d in dates if d >= dt.date.fromisoformat(start_date)]
    if end_date:
        dates = [d for d in dates if d <= dt.date.fromisoformat(end_date)]
    bounds = _poly_bounds(pathlib.Path(raw_dir)) if raw_dir else None
    if bounds:
        first_end, last_start_plus_bucket = bounds
        dates = [d for d in dates if _cutoff_epoch(d) >= first_end and _cutoff_epoch(d) <= last_start_plus_bucket]
    cutoffs = [
        {
            "cutoff_id": d.strftime("%Y%m%dT0125Z"),
            "trade_date": d.isoformat(),
            "cutoff_utc": dt.datetime.fromtimestamp(_cutoff_epoch(d), dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for d in dates
    ]
    result = {
        "schema_version": "poly_cutoff_schedule.v1",
        "timezone": "Asia/Shanghai",
        "cutoff_local": "09:25",
        "bucket_seconds": BUCKET_SECONDS,
        "source": {
            "ashare_root": str(pathlib.Path(ashare_root)),
            "raw_dir": str(pathlib.Path(raw_dir)) if raw_dir else None,
            "poly_bounds_epoch": list(bounds) if bounds else None,
        },
        "cutoff_count": len(cutoffs),
        "cutoffs": cutoffs,
    }
    target = pathlib.Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite schedule: {target}")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ashare-root", type=pathlib.Path, required=True)
    parser.add_argument("--raw-dir", type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args(argv)
    result = build_schedule(
        args.ashare_root,
        args.out,
        raw_dir=args.raw_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print(json.dumps({"cutoff_count": result["cutoff_count"], "output": str(args.out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
