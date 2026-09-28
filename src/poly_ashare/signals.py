from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def compile_signal(
    world_output: Mapping[str, Any],
    *,
    top_k: int = 5,
    main_top_k: int = 5,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    if top_k not in (1, 3, 5):
        raise ValueError("top_k must be 1, 3, or 5")
    rankings = sorted(world_output.get("industry_ranking", []), key=lambda x: int(x["rank"]))
    valid = [r for r in rankings if str(r.get("stance", "BUY")).upper() not in {"NO_CALL", "REJECT"}]
    selected = []
    for row in valid:
        confidence = row.get("confidence", 0)
        confidence_value = float(confidence) if not isinstance(confidence, str) else 1.0 if confidence.lower() in {"high", "高"} else 0.7 if confidence.lower() in {"medium", "中"} else 0.4
        if confidence_value >= min_confidence:
            selected.append(row)
        if len(selected) >= top_k:
            break
    weight = 1.0 / len(selected) if selected else 0.0
    targets = [{"industry_code": str(r["industry_code"]), "action": "BUY", "target_weight": weight} for r in selected]
    raw = json.dumps(world_output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    world_hash = hashlib.sha256(raw).hexdigest()
    return {
        "schema_version": "signal_output.v1",
        "signal_id": f"{world_output.get('run_id', 'run')}-top{top_k}",
        "world_output_sha256": world_hash,
        "cutoff_utc": world_output.get("cutoff_utc"),
        "poly_only": True,
        "mapping_version": "sw2021_level1_v1",
        "status": "ACTIVE" if targets else "NO_TRADE",
        "portfolio_id": f"top{top_k}",
        "targets": targets,
        "execution": {"rebalance": "next_tradeable_open", "position_type": "long_only", "industry_basket": "fixed_equal_weight_members", "no_stock_level_selection": True, "main_top_k": int(main_top_k)},
    }
