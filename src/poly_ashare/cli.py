from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import freeze_artifact, write_json
from .config import load_config
from .model_gateway import run_sealed_model
from .signals import compile_signal
from .world import validate_world_output
from poly_pit_replay.sealing import seal_json_artifact


def _json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="poly-ashare")
    sub = ap.add_subparsers(dest="command", required=True)

    p_config = sub.add_parser("validate-config")
    p_config.add_argument("--config", required=True)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--config", required=True)

    p_model = sub.add_parser("dry-run-model")
    p_model.add_argument("--config", required=True)
    p_model.add_argument("--input", required=True)
    p_model.add_argument("--treatment", default="W3")
    p_model.add_argument("--out", required=True)

    p_signal = sub.add_parser("compile-signal")
    p_signal.add_argument("--world", required=True)
    p_signal.add_argument("--out", required=True)
    p_signal.add_argument("--top-k", type=int, default=5, choices=[1, 3, 5])

    p_validate = sub.add_parser("validate-world")
    p_validate.add_argument("--world", required=True)

    args = ap.parse_args(argv)
    if args.command == "prepare":
        cfg = load_config(args.config)
        for name in ("raw", "research_inputs", "world_outputs", "signals", "evaluation", "logs"):
            (cfg.output_root / name).mkdir(parents=True, exist_ok=True)
        cfg.cache_root.mkdir(parents=True, exist_ok=True)
        plan = {
            "status": "READY_NOT_STARTED",
            "model_id": cfg.model_id,
            "model_execution": cfg.model_execution,
            "poly_only": True,
            "cutoff": f"{cfg.cutoff_timezone} {cfg.cutoff_local}",
            "horizons": list(cfg.horizons),
            "top_k": list(cfg.top_k),
            "main_top_k": cfg.main_top_k,
            "cache_root": str(cfg.cache_root),
            "output_root": str(cfg.output_root),
            "formal_start": False,
        }
        target = cfg.output_root / "run_plan.json"
        write_json(target, plan)
        freeze_artifact(target, artifact_type="run_plan", metadata={"model_id": cfg.model_id})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-config":
        cfg = load_config(args.config)
        print(json.dumps({"status": "OK", "model_id": cfg.model_id, "execution": cfg.model_execution, "horizons": cfg.horizons, "top_k": cfg.top_k}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "dry-run-model":
        cfg = load_config(args.config)
        run = run_sealed_model(_json(args.input), model_id=cfg.model_id, treatment_id=args.treatment, execution=cfg.model_execution)
        write_json(args.out, run.as_dict())
        freeze_artifact(args.out, artifact_type="model_run", metadata={"model_id": cfg.model_id})
        print(json.dumps(run.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-world":
        obj = _json(args.world)
        errors = validate_world_output(obj, expected_cutoff=obj.get("cutoff_utc"))
        print(json.dumps({"status": "OK" if not errors else "INVALID", "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    if args.command == "compile-signal":
        world = _json(args.world)
        errors = validate_world_output(world, expected_cutoff=world.get("cutoff_utc"))
        if errors:
            raise SystemExit("world_output invalid: " + ",".join(errors))
        signal = compile_signal(world, top_k=args.top_k)
        seal_json_artifact(args.out, signal, artifact_type="signal_output")
        print(json.dumps(signal, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
