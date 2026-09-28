from __future__ import annotations

import json
from typing import Any, Mapping

from poly_pit_replay.schemas import SchemaValidationError, validate_world_output as strict_validate_world_output


REQUIRED_WORLD_FIELDS = {
    "schema_version",
    "run_id",
    "cutoff_utc",
    "input_sha256",
    "poly_only",
    "industry_ranking",
}


def build_prompt(research_input: Mapping[str, Any], *, treatment_id: str = "W3") -> str:
    """Construct a sealed prompt; the caller supplies only the snapshot JSON."""
    payload = json.dumps(research_input, ensure_ascii=False, sort_keys=True)
    return (
        f"你是封闭式历史截面研究器。实验臂={treatment_id}。\n"
        "只允许使用下面 JSON 内的信息，禁止联网、搜索、读取 A 股资料、公司资料、新闻、未来标签或当前状态。\n"
        "请先分析盘口的期限、程度和跨盘口约束，再输出 31 个申万一级行业的完整未来相对收益排序。\n"
        "没有充分证据时使用 NO_CALL 或低置信度；不得补写未在输入中出现的事实。\n"
        "输出必须是 JSON，字段包括 schema_version=world_output.v1、run_id、cutoff_utc、input_sha256、poly_only=true、industry_ranking、relation_chains、falsifiers。\n"
        f"研究输入：\n{payload}\n"
    )


def validate_world_output(obj: Mapping[str, Any], *, expected_cutoff: str | None = None) -> list[str]:
    errors: list[str] = []
    try:
        strict_validate_world_output(obj)
    except SchemaValidationError as exc:
        errors.extend(exc.issues)
    ranking = obj.get("industry_ranking")
    if isinstance(ranking, list) and len(ranking) != 31:
        errors.append(f"industry_count:{len(ranking)}; formal run requires 31")
    if expected_cutoff is not None and obj.get("cutoff_utc") != expected_cutoff:
        errors.append("cutoff_mismatch")
    return errors
