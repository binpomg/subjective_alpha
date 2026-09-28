"""Evaluate frozen industry signals against independently-built labels.

This evaluator reads signal files and label Parquet files only after the model
artifact is sealed. It computes per-signal TopK snapshot returns; it does not
feed labels back to the research process and does not place orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path
from typing import Any

from poly_pit_replay.schemas import SchemaValidationError, validate_signal_output


def _duckdb():
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("回测评估需要 DuckDB，请安装可选数据依赖") from exc
    return duckdb


def _signal_date(cutoff_utc: str) -> str:
    value = dt.datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00"))
    return value.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()


def _load_labels(
    path: Path,
    signal_date: str,
    *,
    con: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Load one date from a label file.

    ``con`` is optional to preserve the small helper's standalone API.  The
    evaluator supplies one connection for the whole run so repeated
    signal/horizon lookups avoid paying DuckDB startup cost 300 times on a
    production-sized signal batch.
    """
    owns_connection = con is None
    if owns_connection:
        duckdb = _duckdb()
        con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT industry_code2, industry_return, excess_return, label_valid, benchmark_return "
            "FROM read_parquet(?) WHERE CAST(signal_date AS DATE)=CAST(? AS DATE)",
            [str(path), signal_date],
        ).fetchall()
    finally:
        if owns_connection:
            con.close()
    return {
        str(code): {
            "industry_return": float(industry_return) if industry_return is not None else None,
            "excess_return": float(excess_return) if excess_return is not None else None,
            "label_valid": bool(label_valid),
            "benchmark_return": float(benchmark_return) if benchmark_return is not None else None,
        }
        for code, industry_return, excess_return, label_valid, benchmark_return in rows
    }


def evaluate(signals_dir: Path, labels_root: Path, mapping_mode: str, horizons: tuple[int, ...], out: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    con = None
    try:
        for signal_path in sorted(signals_dir.glob("*.json")):
            if signal_path.name.endswith(".manifest.json"):
                continue
            signal = json.loads(signal_path.read_text(encoding="utf-8"))
            if signal.get("schema_version") != "signal_output.v1":
                continue
            try:
                validate_signal_output(signal)
            except SchemaValidationError as exc:
                results.append({"signal": signal_path.name, "status": "INVALID_SIGNAL", "errors": list(exc.issues)})
                continue
            signal_date = _signal_date(str(signal["cutoff_utc"]))
            for horizon in horizons:
                label_path = labels_root / mapping_mode / f"industry_labels_{horizon}d.parquet"
                if not label_path.exists():
                    results.append({"signal": signal_path.name, "signal_date": signal_date, "horizon": horizon, "status": "NO_LABEL_FILE"})
                    continue
                if con is None:
                    con = _duckdb().connect()
                labels = _load_labels(label_path, signal_date, con=con)
                usable = [
                    labels[str(target["industry_code"])]
                    for target in signal.get("targets", [])
                    if str(target.get("industry_code")) in labels
                    and labels[str(target.get("industry_code"))]["label_valid"]
                    and labels[str(target.get("industry_code"))]["industry_return"] is not None
                ]
                if not usable:
                    results.append({"signal": signal_path.name, "signal_date": signal_date, "horizon": horizon, "status": "NO_VALID_TARGET"})
                    continue
                results.append({
                    "signal": signal_path.name,
                    "signal_date": signal_date,
                    "horizon": horizon,
                    "status": "OK",
                    "top_k": signal.get("portfolio_id"),
                    "target_count": len(usable),
                    "portfolio_return": statistics.fmean(x["industry_return"] for x in usable),
                    "portfolio_excess_return": statistics.fmean(x["excess_return"] for x in usable if x["excess_return"] is not None) if any(x["excess_return"] is not None for x in usable) else None,
                })
    finally:
        if con is not None:
            con.close()
    summary: dict[str, Any] = {"results": results, "summary": []}
    for horizon in horizons:
        values = [x["portfolio_excess_return"] for x in results if x.get("status") == "OK" and x.get("horizon") == horizon and x.get("portfolio_excess_return") is not None]
        summary["summary"].append({"horizon": horizon, "n": len(values), "mean_excess_return": statistics.fmean(values) if values else None})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signals-dir", type=Path, required=True)
    ap.add_argument("--labels-root", type=Path, required=True)
    ap.add_argument("--mapping-mode", default="strict_update", choices=["strict_update", "start_only"])
    ap.add_argument("--horizons", default="20,60,120")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    result = evaluate(args.signals_dir, args.labels_root, args.mapping_mode, horizons, args.out)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
