"""Build a leakage-safe Polymarket price cross-section.

The official ``/v2/prices-history`` endpoint returns aggregate points whose
``timestamp`` is the bucket start.  A point with ``resolution_seconds > 0``
therefore describes the half-open interval
``[timestamp, timestamp + resolution_seconds)``.  An ``as_of=T`` response can
contain a point that starts before T but has not finished by T; that point is
not available at T and must be discarded.  Resolution-zero points are exact
ticks, but the terminal settlement point also has resolution zero.  This
builder excludes them by default so that settlement cannot leak into a
research package.

The module is deliberately independent of pandas and can be used on archived
API responses or, with ``--fetch``, directly against the public API.  It does
not infer market metadata or event rules; callers must supply a pre-registered
token manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping, Sequence

API_URL = "https://data-api.polymarket.com/v2/prices-history"


def parse_epoch(value: str | int | float | dt.datetime) -> int:
    """Parse an ISO-8601 UTC timestamp or epoch seconds."""
    if isinstance(value, dt.datetime):
        obj = value
    elif isinstance(value, (int, float)):
        return int(value)
    else:
        text = str(value).strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        obj = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if obj.tzinfo is None:
        obj = obj.replace(tzinfo=dt.timezone.utc)
    return int(obj.astimezone(dt.timezone.utc).timestamp())


def iso_utc(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _as_points(payload: Any) -> list[Mapping[str, Any]]:
    """Extract price points from either a v2 envelope or a bare list."""
    if isinstance(payload, Mapping):
        points = payload.get("data", [])
    else:
        points = payload
    if not isinstance(points, list):
        return []
    return [p for p in points if isinstance(p, Mapping)]


def _normalise_point(point: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        timestamp = int(point["timestamp"])
        resolution = int(point["resolution_seconds"])
        price = float(point["price"])
    except (KeyError, TypeError, ValueError):
        return None
    if timestamp < 0 or resolution < 0 or not 0 <= price <= 1:
        return None
    return {
        "timestamp": timestamp,
        "price": price,
        "resolution_seconds": resolution,
        "point_end": timestamp + resolution,
    }


def select_latest_usable_point(
    payloads: Iterable[Any],
    cutoff: str | int | float | dt.datetime,
    *,
    allow_exact_ticks: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Select the freshest point observable at ``cutoff``.

    ``point_end <= cutoff`` is required for aggregate points.  Exact ticks are
    excluded by default because a resolved market's terminal settlement point
    has the same ``resolution_seconds=0`` marker as a genuine trade tick.
    The second return value is an audit count keyed by exclusion reason.
    """
    cutoff_epoch = parse_epoch(cutoff)
    counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        for raw in _as_points(payload):
            point = _normalise_point(raw)
            if point is None:
                counts["invalid_point"] = counts.get("invalid_point", 0) + 1
                continue
            if point["timestamp"] > cutoff_epoch:
                counts["starts_after_cutoff"] = counts.get("starts_after_cutoff", 0) + 1
                continue
            if point["resolution_seconds"] == 0:
                if not allow_exact_ticks:
                    counts["resolution_zero_excluded"] = counts.get("resolution_zero_excluded", 0) + 1
                    continue
            elif point["point_end"] > cutoff_epoch:
                counts["bucket_not_complete"] = counts.get("bucket_not_complete", 0) + 1
                continue
            candidates.append(point)
    if not candidates:
        counts["no_usable_point"] = 1
        return None, counts
    # Prefer the newest observation; for ties prefer the narrower bucket.
    candidates.sort(key=lambda p: (p["timestamp"], -p["resolution_seconds"]))
    return candidates[-1], counts


def build_snapshot(
    token_records: Sequence[Mapping[str, Any]],
    cutoff: str | int | float | dt.datetime,
    *,
    allow_exact_ticks: bool = False,
) -> dict[str, Any]:
    """Build a JSON-serialisable cross-section from token payload records.

    Each record needs ``token_id`` and either ``payload`` (already decoded) or
    ``payloads`` (a list of decoded envelopes).  Optional manifest fields such
    as ``market_id`` and ``outcome`` are copied to the selected row.
    """
    cutoff_epoch = parse_epoch(cutoff)
    rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for record in token_records:
        token_id = str(record.get("token_id", ""))
        payloads = record.get("payloads")
        if payloads is None:
            payloads = [record.get("payload", {"data": []})]
        if not isinstance(payloads, list):
            payloads = [payloads]
        point, counts = select_latest_usable_point(
            payloads, cutoff_epoch, allow_exact_ticks=allow_exact_ticks
        )
        audit_row = {
            "token_id": token_id,
            "market_id": record.get("market_id"),
            "outcome": record.get("outcome"),
            "usable": point is not None,
            "exclusion_counts": counts,
        }
        if point is not None:
            row = {
                "token_id": token_id,
                "market_id": record.get("market_id"),
                "outcome": record.get("outcome"),
                "timestamp": point["timestamp"],
                "timestamp_utc": iso_utc(point["timestamp"]),
                "price": point["price"],
                "resolution_seconds": point["resolution_seconds"],
                "point_end": point["point_end"],
                "point_end_utc": iso_utc(point["point_end"]),
            }
            # Only copy metadata explicitly pre-registered by the caller.  The
            # builder never discovers or fetches current Gamma status fields.
            for key in (
                "condition_id",
                "question",
                "slug",
                "description",
                "metadata_version",
                "metadata_status",
            ):
                if key in record:
                    row[key] = record[key]
            rows.append(row)
            audit_row["selected_timestamp"] = point["timestamp"]
            audit_row["selected_point_end"] = point["point_end"]
        audit.append(audit_row)
    return {
        "schema_version": "poly_price_snapshot.v1",
        "experiment": {
            "poly_only": True,
            "external_access": "disabled",
            "ashare_visibility": "none",
            "research_input_role": "world_prediction_input",
            "forbidden_fields": [
                "future_price_history",
                "settlement_result",
                "current_volume_or_status",
                "a_share_prices_or_industry_labels",
                "label_*",
            ],
        },
        "cutoff_epoch": cutoff_epoch,
        "cutoff_utc": iso_utc(cutoff_epoch),
        "point_policy": {
            "aggregate_rule": "timestamp + resolution_seconds <= cutoff",
            "resolution_zero": "included" if allow_exact_ticks else "excluded_conservatively",
            "as_of_is_not_sufficient_without_point_end_filter": True,
        },
        "rows": rows,
        "coverage": {
            "tokens_requested": len(token_records),
            "tokens_with_usable_point": len(rows),
            "tokens_without_usable_point": len(token_records) - len(rows),
        },
        "audit": audit,
    }


def fetch_json(url: str, *, retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "poly-snapshot-builder/1.0"})
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 503) or attempt + 1 >= retries:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"HTTP {exc.code} while fetching {url}: {body[:500]}"
                ) from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = max(0.0, float(retry_after)) if retry_after else 2 ** attempt
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_token_history(token_id: str, *, cutoff: int, endpoint: str = API_URL) -> list[dict[str, Any]]:
    """Fetch an as-of candidate and a permanent 12-hour history fallback.

    The as-of request is retained in the returned payload list for provenance.
    The max history is needed when the as-of candidate is an unfinished bucket.
    """
    asof_query = urllib.parse.urlencode({"token_id": token_id, "as_of": cutoff, "limit": 10000})
    max_query = urllib.parse.urlencode({"token_id": token_id, "interval": "max", "bucket_seconds": 43200, "limit": 10000})
    payloads = [fetch_json(f"{endpoint}?{asof_query}")]
    payload = fetch_json(f"{endpoint}?{max_query}")
    payloads.append(payload)
    # Follow cursors for unusually long histories; max/12h generally fits one page.
    cursor = (payload.get("pagination") or {}).get("next_cursor") if isinstance(payload, Mapping) else None
    while cursor:
        query = urllib.parse.urlencode({"token_id": token_id, "interval": "max", "bucket_seconds": 43200, "cursor": cursor})
        payload = fetch_json(f"{endpoint}?{query}")
        payloads.append(payload)
        cursor = (payload.get("pagination") or {}).get("next_cursor") if isinstance(payload, Mapping) else None
    return payloads


def load_manifest(path: pathlib.Path) -> list[dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(obj, Mapping):
        records = obj.get("tokens", obj.get("data", []))
    else:
        records = obj
    if not isinstance(records, list):
        raise ValueError("manifest must be a list or an object with a 'tokens' list")
    return [dict(r) for r in records if isinstance(r, Mapping)]


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a leakage-safe Polymarket price cross-section")
    ap.add_argument("--manifest", type=pathlib.Path, required=True, help="pre-registered JSON token manifest")
    ap.add_argument("--cutoff", required=True, help="UTC ISO timestamp or epoch seconds")
    ap.add_argument("--raw-dir", type=pathlib.Path, help="directory containing raw JSON files named by manifest raw_file")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--fetch", action="store_true", help="fetch missing records from the official API")
    ap.add_argument("--allow-exact-ticks", action="store_true", help="include resolution-zero ticks (not recommended for formal runs)")
    args = ap.parse_args()
    cutoff_epoch = parse_epoch(args.cutoff)
    records = load_manifest(args.manifest)
    hydrated: list[dict[str, Any]] = []
    for record in records:
        rec = dict(record)
        payloads: list[Any] = []
        raw_file = rec.get("raw_file")
        if raw_file and args.raw_dir:
            # Cache manifests may be generated on Windows and replayed on
            # Linux. Normalize the recorded separator without changing the
            # manifest hash or the raw filename identity.
            raw_name = str(raw_file)
            if os.name != "nt":
                raw_name = raw_name.replace("\\", "/")
            path = args.raw_dir / raw_name
            if path.exists():
                payloads.append(json.loads(path.read_text(encoding="utf-8")))
        if not payloads and args.fetch:
            payloads = fetch_token_history(str(rec["token_id"]), cutoff=cutoff_epoch)
        rec["payloads"] = payloads
        hydrated.append(rec)
    snapshot = build_snapshot(hydrated, cutoff_epoch, allow_exact_ticks=args.allow_exact_ticks)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "builder": "build_poly_snapshot.py",
        "builder_sha256": sha256(pathlib.Path(__file__).resolve()),
        "manifest": str(args.manifest),
        "manifest_sha256": sha256(args.manifest),
        "snapshot": str(args.out),
        "snapshot_sha256": sha256(args.out),
    }
    print(json.dumps({"snapshot": snapshot["coverage"], "provenance": manifest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
