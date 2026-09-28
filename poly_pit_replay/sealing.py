"""JSON canonicalisation, validation-before-write and SHA256 sealing."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
from typing import Any, Mapping

from .schemas import VALIDATORS, SchemaValidationError


def canonical_json_bytes(obj: Any) -> bytes:
    """Return stable UTF-8 JSON bytes used for all artifact hashes."""
    return (
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | pathlib.Path) -> str:
    return sha256_bytes(pathlib.Path(path).read_bytes())


def seal_json_artifact(
    path: str | pathlib.Path,
    obj: Mapping[str, Any],
    *,
    artifact_type: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate and write one artifact, returning its immutable hash record.

    Existing files are refused by default to prevent silently changing a
    frozen input/output.  The seal record contains no model output beyond the
    SHA256 and schema identity.
    """
    validator = VALIDATORS.get(artifact_type)
    if validator is None:
        raise ValueError(f"unsupported artifact_type: {artifact_type}")
    validated = validator(obj)
    target = pathlib.Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite sealed artifact: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(validated) + b"\n"
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_bytes(data)
    temp.replace(target)
    return {
        "artifact_type": artifact_type,
        "schema_version": str(validated.get("schema_version")),
        "path": str(target),
        "sha256": sha256_bytes(data),
        "sealed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def verify_seal(path: str | pathlib.Path, expected_sha256: str) -> bool:
    """Return whether a file's bytes match an expected seal hash."""
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(f"seal mismatch for {path}: expected {expected_sha256}, got {actual}")
    return True


def load_and_validate_json(path: str | pathlib.Path, *, artifact_type: str) -> dict[str, Any]:
    """Read one JSON artifact and apply its registered schema validator."""
    validator = VALIDATORS.get(artifact_type)
    if validator is None:
        raise ValueError(f"unsupported artifact_type: {artifact_type}")
    obj = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return validator(obj)


__all__ = [
    "SchemaValidationError",
    "canonical_json_bytes",
    "load_and_validate_json",
    "seal_json_artifact",
    "sha256_bytes",
    "sha256_file",
    "verify_seal",
]
