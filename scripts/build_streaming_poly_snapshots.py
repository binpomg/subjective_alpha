"""Build Poly-only snapshots one cutoff at a time with bounded API windows.

Unlike the full-history cache, this runner requests only a small window ending
at each cutoff and immediately persists the raw response and sealed snapshot.
It is the preferred path when the token universe is large: memory is bounded
by one cutoff's rows rather than by every token's complete history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from poly_pit_replay.sealing import seal_json_artifact

from build_poly_snapshot import build_snapshot, iso_utc, load_manifest, parse_epoch, select_latest_usable_point


DEFAULT_ENDPOINT = "https://data-api.polymarket.com/v2/prices-history"
SCHEMA_VERSION = "poly_streaming_snapshot_batch.v1"


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get_json(url: str, *, retries: int = 4) -> tuple[dict[str, Any], dict[str, str]]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "poly-streaming-snapshot/0.1"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                headers = {str(k): str(v) for k, v in response.headers.items()}
            return json.loads(body.decode("utf-8")), headers
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 >= retries:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Data API HTTP {exc.code}: {body[:500]}") from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2**attempt
            except (TypeError, ValueError):
                delay = 2**attempt
            time.sleep(max(0.0, delay))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 >= retries:
                raise RuntimeError(f"Data API request failed: {exc}") from exc
            time.sleep(2**attempt)
    raise RuntimeError(f"Data API request failed: {last}")


def _load_schedule(path: pathlib.Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    values = obj.get("cutoffs", obj) if isinstance(obj, Mapping) else obj
    if not isinstance(values, list) or not values:
        raise ValueError("schedule must contain a non-empty cutoffs list")
    rows: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, Mapping):
            cutoff = item.get("cutoff_utc", item.get("cutoff"))
            cutoff_id = str(item.get("cutoff_id", ""))
            trade_date = item.get("trade_date")
        else:
            cutoff = item
            cutoff_id = ""
            trade_date = None
        epoch = parse_epoch(cutoff)
        if not cutoff_id:
            cutoff_id = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rows.append({"cutoff_id": cutoff_id, "cutoff_epoch": epoch, "cutoff_utc": iso_utc(epoch), "trade_date": trade_date})
    return sorted(rows, key=lambda row: (row["cutoff_epoch"], row["cutoff_id"]))


def _load_token_records(path: pathlib.Path) -> list[dict[str, Any]]:
    """Load either a JSON manifest or the resume-safe SQLite universe."""
    if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        con = sqlite3.connect(path)
        try:
            return [json.loads(row[0]) for row in con.execute("SELECT payload FROM tokens ORDER BY token_id")]
        finally:
            con.close()
    return load_manifest(path)


def _raw_path(raw_root: pathlib.Path, cutoff_id: str, token_id: str) -> pathlib.Path:
    return raw_root / cutoff_id / f"token_{token_id}.json"


def _fetch_window(
    token_id: str,
    cutoff_epoch: int,
    *,
    endpoint: str,
    bucket_seconds: int,
    window_buckets: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    start = cutoff_epoch - bucket_seconds * max(1, window_buckets)
    params = {
        "token_id": token_id,
        "start": start,
        "end": cutoff_epoch,
        "bucket_seconds": bucket_seconds,
    }
    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
    payload, headers = _get_json(url)
    payload.setdefault("provenance", {})
    payload["provenance"].update(
        {
            "request_url": url,
            "request_params": params,
            "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "trace_id": headers.get("x-trace-id"),
        }
    )
    return payload, params


def build_streaming_batch(
    manifest_path: str | pathlib.Path,
    schedule_path: str | pathlib.Path,
    snapshot_dir: str | pathlib.Path,
    raw_dir: str | pathlib.Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    bucket_seconds: int = 43_200,
    window_buckets: int = 2,
    sleep_seconds: float = 0.0,
    resume: bool = True,
    batch_id: str | None = None,
) -> dict[str, Any]:
    manifest_path = pathlib.Path(manifest_path)
    schedule_path = pathlib.Path(schedule_path)
    snapshot_dir = pathlib.Path(snapshot_dir)
    raw_dir = pathlib.Path(raw_dir)
    records = _load_token_records(manifest_path)
    schedule = _load_schedule(schedule_path)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    batch_id = batch_id or dt.datetime.now(dt.timezone.utc).strftime("stream_%Y%m%dT%H%M%SZ")
    entries: list[dict[str, Any]] = []
    for item in schedule:
        cutoff_id = str(item["cutoff_id"])
        target = snapshot_dir / f"{cutoff_id}.json"
        if target.exists() and resume:
            entries.append({"cutoff_id": cutoff_id, "status": "SKIPPED_EXISTING", "output": str(target), "sha256": _sha(target)})
            continue
        hydrated: list[dict[str, Any]] = []
        raw_hashes: dict[str, str] = {}
        fetch_errors: dict[str, str] = {}
        for index, record in enumerate(records):
            token_id = str(record.get("token_id", ""))
            if not token_id:
                continue
            path = _raw_path(raw_dir, cutoff_id, token_id)
            try:
                if path.exists() and resume:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                else:
                    payload, _ = _fetch_window(
                        token_id,
                        int(item["cutoff_epoch"]),
                        endpoint=endpoint,
                        bucket_seconds=bucket_seconds,
                        window_buckets=window_buckets,
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(f".{path.name}.tmp")
                    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    temp.replace(path)
                raw_hashes[token_id] = _sha(path)
                hydrated.append({**record, "payloads": [payload]})
            except Exception as exc:  # preserve a missing token and continue the cutoff
                fetch_errors[token_id] = f"{type(exc).__name__}: {exc}"
                hydrated.append({**record, "payloads": []})
            if sleep_seconds > 0 and index + 1 < len(records):
                time.sleep(sleep_seconds)
        snapshot = build_snapshot(hydrated, int(item["cutoff_epoch"]))
        for audit in snapshot["audit"]:
            token_id = str(audit.get("token_id", ""))
            if token_id in fetch_errors:
                audit["fetch_error"] = fetch_errors[token_id]
        sealed = seal_json_artifact(target, snapshot, artifact_type="research_input")
        entries.append(
            {
                "cutoff_id": cutoff_id,
                "trade_date": item.get("trade_date"),
                "cutoff_utc": item["cutoff_utc"],
                "status": "OK",
                "output": str(target),
                "sha256": sealed["sha256"],
                "coverage": snapshot["coverage"],
                "raw_token_count": len(raw_hashes),
                "raw_file_sha256": raw_hashes,
                "fetch_error_count": len(fetch_errors),
                "fetch_errors": fetch_errors,
            }
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": _sha(manifest_path),
        "schedule": str(schedule_path),
        "schedule_sha256": _sha(schedule_path),
        "endpoint": endpoint,
        "bucket_seconds": bucket_seconds,
        "window_buckets": window_buckets,
        "snapshot_count": len(entries),
        "status": "OK",
        "snapshots": entries,
    }
    manifest_out = snapshot_dir / "batch_manifest.json"
    if manifest_out.exists() and not resume:
        raise FileExistsError(f"refusing to overwrite batch manifest: {manifest_out}")
    temp = manifest_out.with_name(f".{manifest_out.name}.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(manifest_out)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--schedule", type=pathlib.Path, required=True)
    parser.add_argument("--snapshot-dir", type=pathlib.Path, required=True)
    parser.add_argument("--raw-dir", type=pathlib.Path, required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--bucket-seconds", type=int, default=43_200)
    parser.add_argument("--window-buckets", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--batch-id")
    args = parser.parse_args(argv)
    result = build_streaming_batch(
        args.manifest,
        args.schedule,
        args.snapshot_dir,
        args.raw_dir,
        endpoint=args.endpoint,
        bucket_seconds=args.bucket_seconds,
        window_buckets=args.window_buckets,
        sleep_seconds=args.sleep_seconds,
        resume=not args.no_resume,
        batch_id=args.batch_id,
    )
    print(json.dumps({"status": result["status"], "snapshot_count": result["snapshot_count"], "batch_id": result["batch_id"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
