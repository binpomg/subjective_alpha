"""研究输入、世界输出和行业信号的轻量结构校验。

这些校验是实验边界的机器检查，不是对模型经济推理质量的评分器。未知
字段允许保留，便于报告扩展；关键字段缺失、未来时间、非法概率和违反
Poly-only 隔离时直接失败。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from typing import Any, Callable


class SchemaValidationError(ValueError):
    """包含所有可定位校验错误的契约异常。"""

    def __init__(self, artifact_type: str, issues: Sequence[str]) -> None:
        self.artifact_type = artifact_type
        self.issues = tuple(issues)
        message = "; ".join(self.issues) or "unknown validation error"
        super().__init__(f"{artifact_type} schema validation failed: {message}")


def _as_mapping(obj: Any, artifact_type: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise SchemaValidationError(artifact_type, ["root must be a JSON object"])
    return obj


def _required(root: Mapping[str, Any], keys: Sequence[str], issues: list[str]) -> None:
    for key in keys:
        if key not in root:
            issues.append(f"missing required field: {key}")


def _epoch(value: Any, field: str, issues: list[str]) -> int | None:
    if isinstance(value, bool):
        issues.append(f"{field} must be an ISO-8601 timestamp or epoch seconds")
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                raise ValueError
            return int(value)
        text = str(value).strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.astimezone(dt.timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError):
        issues.append(f"{field} must be an ISO-8601 timestamp or epoch seconds")
        return None


def _is_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_confidence(value: Any) -> bool:
    """允许可审计的等级或 0—1 数值，不替模型规定唯一表达方式。"""
    if _is_string(value):
        return True
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and 0 <= number <= 1


def _check_experiment(root: Mapping[str, Any], issues: list[str], artifact_type: str) -> None:
    experiment = root.get("experiment")
    if not isinstance(experiment, Mapping):
        issues.append("experiment must be an object")
        return
    if experiment.get("poly_only") is not True:
        issues.append("experiment.poly_only must be true")
    if artifact_type == "research_input":
        if experiment.get("external_access") not in ("disabled", "none"):
            issues.append("experiment.external_access must be disabled/none")
        if experiment.get("ashare_visibility") not in ("none", "evaluation_only"):
            issues.append("experiment.ashare_visibility must be none/evaluation_only")


def _check_research_forbidden_fields(root: Mapping[str, Any], rows: Sequence[Any], issues: list[str]) -> None:
    """Catch common A-share/future-label fields before sealing a Poly-only cut."""
    forbidden_exact = {
        "A_share_selected_codes",
        "A_share_rows_at_T",
        "future_price_history",
        "settlement_result",
        "label_*",
    }
    for key in root:
        text = str(key)
        if text in forbidden_exact or text.lower().startswith(("a_share", "ashare", "label_")):
            issues.append(f"forbidden Poly-only field at root: {text}")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        for key in row:
            text = str(key)
            if text.lower().startswith(("a_share", "ashare", "label_", "future_")):
                issues.append(f"forbidden Poly-only field at rows[{index}]: {text}")


def validate_research_input(obj: Any) -> dict[str, Any]:
    """Validate a Poly-only `research_input` snapshot and return a copy."""
    root = _as_mapping(obj, "research_input")
    issues: list[str] = []
    _required(root, ("schema_version", "cutoff_utc", "experiment", "rows", "coverage"), issues)
    if root.get("schema_version") != "poly_price_snapshot.v1":
        issues.append("schema_version must be poly_price_snapshot.v1")
    _check_experiment(root, issues, "research_input")
    cutoff = _epoch(root.get("cutoff_utc"), "cutoff_utc", issues)
    rows = root.get("rows")
    if not isinstance(rows, list):
        issues.append("rows must be an array")
        rows = []
    _check_research_forbidden_fields(root, rows, issues)
    for index, row in enumerate(rows):
        prefix = f"rows[{index}]"
        if not isinstance(row, Mapping):
            issues.append(f"{prefix} must be an object")
            continue
        _required(row, ("token_id", "timestamp", "point_end", "price", "resolution_seconds"), issues)
        if not _is_string(row.get("token_id")):
            issues.append(f"{prefix}.token_id must be a non-empty string")
        timestamp = _epoch(row.get("timestamp"), f"{prefix}.timestamp", issues)
        point_end = _epoch(row.get("point_end"), f"{prefix}.point_end", issues)
        try:
            price = float(row.get("price"))
            resolution = int(row.get("resolution_seconds"))
            if not math.isfinite(price) or not 0 <= price <= 1:
                raise ValueError
            if resolution < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            issues.append(f"{prefix}.price/resolution_seconds is invalid")
            price = None
            resolution = None
        if timestamp is not None and point_end is not None and resolution is not None:
            if point_end != timestamp + resolution:
                issues.append(f"{prefix}.point_end must equal timestamp + resolution_seconds")
            if cutoff is not None and point_end > cutoff:
                issues.append(f"{prefix} extends beyond cutoff_utc")
            if timestamp > point_end:
                issues.append(f"{prefix}.timestamp cannot exceed point_end")
        if resolution == 0:
            issues.append(f"{prefix}.resolution_seconds=0 is forbidden in strict research_input")
    coverage = root.get("coverage")
    if not isinstance(coverage, Mapping):
        issues.append("coverage must be an object")
    else:
        expected = coverage.get("tokens_requested")
        available = coverage.get("tokens_with_usable_point")
        if expected != len(root.get("audit", [])):
            issues.append("coverage.tokens_requested must equal audit length")
        if available != len(rows):
            issues.append("coverage.tokens_with_usable_point must equal rows length")
    if issues:
        raise SchemaValidationError("research_input", issues)
    return dict(root)


def _validate_ranking(ranking: Any, issues: list[str]) -> None:
    if not isinstance(ranking, list) or not ranking:
        issues.append("industry_ranking must be a non-empty array")
        return
    seen: set[str] = set()
    for index, row in enumerate(ranking):
        prefix = f"industry_ranking[{index}]"
        if not isinstance(row, Mapping):
            issues.append(f"{prefix} must be an object")
            continue
        if not _is_string(row.get("industry_code")):
            issues.append(f"{prefix}.industry_code must be a non-empty string")
        code = str(row.get("industry_code", ""))
        if code in seen:
            issues.append(f"{prefix}.industry_code is duplicated")
        seen.add(code)
        try:
            rank = int(row.get("rank"))
            if rank < 1:
                raise ValueError
        except (TypeError, ValueError):
            issues.append(f"{prefix}.rank must be a positive integer")
        if not _is_confidence(row.get("confidence")):
            issues.append(f"{prefix}.confidence must be a level string or a number in [0,1]")
        if not _is_string(row.get("maturity")):
            issues.append(f"{prefix}.maturity must be a non-empty level string")


def validate_world_output(obj: Any) -> dict[str, Any]:
    """Validate the frozen Poly-only model output used for industry ranking."""
    root = _as_mapping(obj, "world_output")
    issues: list[str] = []
    _required(root, ("schema_version", "run_id", "cutoff_utc", "input_sha256", "poly_only", "industry_ranking"), issues)
    if root.get("schema_version") != "world_output.v1":
        issues.append("schema_version must be world_output.v1")
    if not _is_string(root.get("run_id")):
        issues.append("run_id must be a non-empty string")
    if not _is_string(root.get("input_sha256")):
        issues.append("input_sha256 must be a non-empty string")
    if root.get("poly_only") is not True:
        issues.append("poly_only must be true")
    _epoch(root.get("cutoff_utc"), "cutoff_utc", issues)
    _validate_ranking(root.get("industry_ranking"), issues)
    for key in ("relation_chains", "falsifiers"):
        if key in root and not isinstance(root[key], list):
            issues.append(f"{key} must be an array when present")
    if issues:
        raise SchemaValidationError("world_output", issues)
    return dict(root)


def validate_signal_output(obj: Any) -> dict[str, Any]:
    """Validate an industry-level signal after world output is sealed."""
    root = _as_mapping(obj, "signal_output")
    issues: list[str] = []
    _required(root, ("schema_version", "signal_id", "world_output_sha256", "cutoff_utc", "status", "portfolio_id", "targets"), issues)
    if root.get("schema_version") != "signal_output.v1":
        issues.append("schema_version must be signal_output.v1")
    for key in ("signal_id", "world_output_sha256", "portfolio_id"):
        if not _is_string(root.get(key)):
            issues.append(f"{key} must be a non-empty string")
    if root.get("status") not in {"ACTIVE", "WATCH", "NO_TRADE"}:
        issues.append("status must be ACTIVE, WATCH, or NO_TRADE")
    _epoch(root.get("cutoff_utc"), "cutoff_utc", issues)
    targets = root.get("targets")
    if not isinstance(targets, list):
        issues.append("targets must be an array")
        targets = []
    seen: set[str] = set()
    weight_sum = 0.0
    for index, target in enumerate(targets):
        prefix = f"targets[{index}]"
        if not isinstance(target, Mapping):
            issues.append(f"{prefix} must be an object")
            continue
        code = target.get("industry_code")
        if not _is_string(code):
            issues.append(f"{prefix}.industry_code must be a non-empty string")
        elif code in seen:
            issues.append(f"{prefix}.industry_code is duplicated")
        else:
            seen.add(str(code))
        action = target.get("action")
        if action not in {"BUY", "HOLD", "REDUCE", "EXIT"}:
            issues.append(f"{prefix}.action is invalid")
        try:
            weight = float(target.get("target_weight"))
            if not math.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError
            weight_sum += weight
        except (TypeError, ValueError, OverflowError):
            issues.append(f"{prefix}.target_weight must be in [0,1]")
    if weight_sum > 1.0000001:
        issues.append("sum(targets.target_weight) cannot exceed 1")
    if root.get("status") == "ACTIVE" and not targets:
        issues.append("ACTIVE signal must contain at least one target")
    if root.get("status") == "NO_TRADE" and targets:
        issues.append("NO_TRADE signal must have an empty targets array")
    if issues:
        raise SchemaValidationError("signal_output", issues)
    return dict(root)


VALIDATORS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "research_input": validate_research_input,
    "world_output": validate_world_output,
    "signal_output": validate_signal_output,
}
