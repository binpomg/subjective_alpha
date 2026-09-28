from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def snapshot_trade(signal: Mapping[str, Any], labels: Mapping[str, Mapping[str, float]], *, top_k: int, horizon: int) -> dict[str, Any]:
    portfolio = signal.get("portfolios", {}).get(f"top{top_k}", {})
    weights = portfolio.get("target_weights", {})
    values = [float(labels[code][f"return_{horizon}"]) for code in weights if code in labels]
    if not values:
        return {"status": "NO_LABEL", "top_k": top_k, "horizon": horizon, "return": None, "n": 0}
    return {
        "status": "OK",
        "top_k": top_k,
        "horizon": horizon,
        "return": sum(values) / len(values),
        "n": len(values),
        "missing_industries": [code for code in weights if code not in labels],
    }


def run_snapshot_book(signals: Iterable[Mapping[str, Any]], labels_by_cutoff: Mapping[str, Mapping[str, Mapping[str, float]]], *, top_ks: Sequence[int] = (1, 3, 5), horizons: Sequence[int] = (20, 60, 120)) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for signal in signals:
        cutoff = str(signal.get("cutoff_utc"))
        labels = labels_by_cutoff.get(cutoff, {})
        for top_k in top_ks:
            for horizon in horizons:
                output.append(snapshot_trade(signal, labels, top_k=top_k, horizon=horizon))
    return output
