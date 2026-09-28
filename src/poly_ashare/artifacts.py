from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from poly_pit_replay.sealing import load_and_validate_json


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


def write_json(path: str | Path, obj: Any) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    return sha256_bytes(data)


def freeze_artifact(path: str | Path, *, artifact_type: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    path = Path(path)
    # Known experiment artifacts must pass the strict contract before their
    # hash is recorded.  Other artifacts (for example model-run diagnostics)
    # retain the original hash-only behavior.
    if artifact_type in {"research_input", "world_output", "signal_output"}:
        load_and_validate_json(path, artifact_type=artifact_type)
    digest = sha256_bytes(path.read_bytes())
    manifest = {
        "artifact_type": artifact_type,
        "path": str(path.resolve()),
        "sha256": digest,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": dict(metadata or {}),
    }
    write_json(path.with_suffix(path.suffix + ".manifest.json"), manifest)
    return manifest
