"""Build and seal a reproducible batch of Polymarket point-in-time snapshots.

The token history is read from the immutable local cache.  This orchestration
layer deliberately reuses ``build_poly_snapshot.build_snapshot`` so the
point-end and settlement-point rules have one implementation.  A schedule is
an external, frozen JSON file containing either UTC strings or records with
``cutoff_id``, ``cutoff_utc`` and an optional A-share ``trade_date`` label.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from poly_pit_replay.sealing import seal_json_artifact

from build_poly_snapshot import build_snapshot, iso_utc, load_manifest, parse_epoch


SCHEMA_VERSION = "poly_snapshot_batch.v1"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_schedule(path: pathlib.Path) -> list[dict[str, str | int | None]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    values: Any = obj.get("cutoffs_utc", obj.get("cutoffs", obj)) if isinstance(obj, Mapping) else obj
    if not isinstance(values, list) or not values:
        raise ValueError("schedule must be a non-empty JSON list or an object with cutoffs_utc")
    records: list[dict[str, str | int | None]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        if isinstance(item, Mapping):
            raw_cutoff = item.get("cutoff_utc", item.get("cutoff"))
            trade_date = item.get("trade_date")
            cutoff_id = str(item.get("cutoff_id", "")).strip()
        else:
            raw_cutoff = item
            trade_date = None
            cutoff_id = ""
        if raw_cutoff is None:
            raise ValueError(f"schedule[{index}] missing cutoff_utc")
        epoch = parse_epoch(raw_cutoff)
        if epoch <= 0:
            raise ValueError(f"schedule[{index}] cutoff must be positive")
        cutoff_utc = iso_utc(epoch)
        assert cutoff_utc is not None
        if not cutoff_id:
            cutoff_id = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if cutoff_id in seen:
            raise ValueError(f"duplicate cutoff_id: {cutoff_id}")
        seen.add(cutoff_id)
        records.append({
            "cutoff_id": cutoff_id,
            "cutoff_utc": cutoff_utc,
            "cutoff_epoch": epoch,
            "trade_date": str(trade_date) if trade_date is not None else None,
        })
    records.sort(key=lambda row: (int(row["cutoff_epoch"]), str(row["cutoff_id"])))
    return records


def _hydrate_records(manifest_path: pathlib.Path, raw_dir: pathlib.Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    records = load_manifest(manifest_path)
    hydrated: list[dict[str, Any]] = []
    raw_hashes: dict[str, str] = {}
    for record in records:
        item = dict(record)
        payloads: list[Any] = []
        raw_file = item.get("raw_file")
        if raw_file:
            raw_name = str(raw_file).replace("\\", "/")
            path = raw_dir / raw_name
            if path.exists():
                payloads.append(json.loads(path.read_text(encoding="utf-8")))
                raw_hashes[raw_name] = sha256(path)
        item["payloads"] = payloads
        hydrated.append(item)
    return hydrated, raw_hashes


def build_batch(
    manifest_path: str | pathlib.Path,
    raw_dir: str | pathlib.Path,
    schedule_path: str | pathlib.Path,
    output_dir: str | pathlib.Path,
    *,
    batch_id: str | None = None,
    allow_exact_ticks: bool = False,
) -> dict[str, Any]:
    manifest_path = pathlib.Path(manifest_path)
    raw_dir = pathlib.Path(raw_dir)
    schedule_path = pathlib.Path(schedule_path)
    output_dir = pathlib.Path(output_dir)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    if not raw_dir.exists():
        raise FileNotFoundError(raw_dir)
    if not schedule_path.exists():
        raise FileNotFoundError(schedule_path)
    schedule = _load_schedule(schedule_path)
    records, raw_hashes = _hydrate_records(manifest_path, raw_dir)
    batch_id = batch_id or dt.datetime.now(dt.timezone.utc).strftime("batch_%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    output_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for item in schedule:
        cutoff_id = str(item["cutoff_id"])
        target = output_dir / f"{cutoff_id}.json"
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing snapshot: {target}")
        snapshot = build_snapshot(records, int(item["cutoff_epoch"]), allow_exact_ticks=allow_exact_ticks)
        sealed = seal_json_artifact(target, snapshot, artifact_type="research_input")
        staleness = [
            int(item["cutoff_epoch"]) - int(audit_row["selected_point_end"])
            for audit_row in snapshot["audit"]
            if audit_row.get("usable") and audit_row.get("selected_point_end") is not None
        ]
        entries.append({
            "cutoff_id": cutoff_id,
            "trade_date": item.get("trade_date"),
            "cutoff_utc": item["cutoff_utc"],
            "output": str(target),
            "sha256": sealed["sha256"],
            "status": "OK",
            "coverage": snapshot["coverage"],
            "audit": snapshot["audit"],
            "staleness_seconds": {
                "min": min(staleness) if staleness else None,
                "max": max(staleness) if staleness else None,
                "mean": sum(staleness) / len(staleness) if staleness else None,
            },
        })
    result = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "builder": "batch_build_poly_snapshots.py",
        "builder_sha256": sha256(pathlib.Path(__file__).resolve()),
        "source_manifest": str(manifest_path),
        "source_manifest_sha256": sha256(manifest_path),
        "raw_dir": str(raw_dir),
        "raw_file_sha256": raw_hashes,
        "schedule": str(schedule_path),
        "schedule_sha256": sha256(schedule_path),
        "point_policy": {
            "aggregate_rule": "timestamp + resolution_seconds <= cutoff",
            "resolution_zero": "included" if allow_exact_ticks else "excluded_conservatively",
            "as_of_is_not_sufficient_without_point_end_filter": True,
        },
        "cutoff_count": len(schedule),
        "snapshot_count": len(entries),
        "status": "OK",
        "snapshots": entries,
        "strict_pit_eligible": bool(json.loads(manifest_path.read_text(encoding="utf-8")).get("strict_pit_eligible", False)),
    }
    manifest_out = output_dir / "batch_manifest.json"
    if manifest_out.exists():
        raise FileExistsError(f"refusing to overwrite existing batch manifest: {manifest_out}")
    temp = manifest_out.with_name(f".{manifest_out.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(manifest_out)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--raw-dir", type=pathlib.Path, required=True)
    parser.add_argument("--schedule", type=pathlib.Path, required=True)
    parser.add_argument("--out-dir", type=pathlib.Path, required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--allow-exact-ticks", action="store_true")
    args = parser.parse_args(argv)
    result = build_batch(
        args.manifest,
        args.raw_dir,
        args.schedule,
        args.out_dir,
        batch_id=args.batch_id,
        allow_exact_ticks=args.allow_exact_ticks,
    )
    print(json.dumps({"batch_id": result["batch_id"], "status": result["status"], "snapshot_count": result["snapshot_count"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
