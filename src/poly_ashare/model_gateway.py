from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .artifacts import sha256_json
from .world import build_prompt, validate_world_output


@dataclass(frozen=True)
class ModelRun:
    model_id: str
    treatment_id: str
    cutoff_utc: str
    prompt_hash: str
    execution: str
    status: str
    output: Mapping[str, Any] | None
    error: str | None
    created_at_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "treatment_id": self.treatment_id,
            "cutoff_utc": self.cutoff_utc,
            "prompt_hash": self.prompt_hash,
            "execution": self.execution,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "created_at_utc": self.created_at_utc,
        }


def run_sealed_model(
    research_input: Mapping[str, Any],
    *,
    model_id: str,
    treatment_id: str = "W3",
    execution: str = "disabled",
) -> ModelRun:
    """Run gate for the sealed model.

    The default is deliberately non-executing. A future explicit adapter may
    call the configured local model, but it must return a structured JSON object
    that passes ``validate_world_output`` before it can be frozen.
    """
    prompt = build_prompt(research_input, treatment_id=treatment_id)
    prompt_hash = sha256_json({"prompt": prompt, "model_id": model_id, "treatment_id": treatment_id})
    now = datetime.now(timezone.utc).isoformat()
    if model_id != "gpt6-Astra-ultra":
        return ModelRun(model_id, treatment_id, str(research_input.get("cutoff_utc")), prompt_hash, execution, "INVALID", None, "model_id_mismatch", now)
    if execution != "explicit":
        return ModelRun(model_id, treatment_id, str(research_input.get("cutoff_utc")), prompt_hash, execution, "NOT_STARTED", None, "model_execution_disabled_until_explicit_start", now)
    return ModelRun(model_id, treatment_id, str(research_input.get("cutoff_utc")), prompt_hash, execution, "BLOCKED", None, "no_local_model_adapter_registered", now)


def accept_world_output(output: Mapping[str, Any], *, cutoff_utc: str) -> dict[str, Any]:
    errors = validate_world_output(output, expected_cutoff=cutoff_utc)
    if errors:
        raise ValueError("world_output schema validation failed: " + ",".join(errors))
    return dict(output)
