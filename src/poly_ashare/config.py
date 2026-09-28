from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ExperimentConfig:
    root: Path
    model_id: str
    model_provider: str
    model_execution: str
    cutoff_local: str
    cutoff_timezone: str
    horizons: tuple[int, ...]
    top_k: tuple[int, ...]
    main_top_k: int
    mapping_modes: tuple[str, ...]
    ashare_root: Path
    industry_manifest: Path
    token_manifest: Path
    output_root: Path
    cache_root: Path

    @classmethod
    def from_json(cls, path: str | Path) -> "ExperimentConfig":
        path = Path(path).resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent.parent
        paths = raw.get("paths", {})
        model = raw.get("model", {})
        experiment = raw.get("experiment", {})

        def resolve(value: str) -> Path:
            p = Path(value)
            return p if p.is_absolute() else (base / p).resolve()

        cfg = cls(
            root=base,
            model_id=str(model.get("id", "")),
            model_provider=str(model.get("provider", "")),
            model_execution=str(model.get("execution", "disabled")),
            cutoff_local=str(experiment.get("cutoff_local", "09:25")),
            cutoff_timezone=str(experiment.get("cutoff_timezone", "Asia/Shanghai")),
            horizons=tuple(int(x) for x in experiment.get("horizons", [20, 60, 120])),
            top_k=tuple(int(x) for x in experiment.get("top_k", [1, 3, 5])),
            main_top_k=int(experiment.get("main_top_k", 5)),
            mapping_modes=tuple(str(x) for x in experiment.get("mapping_modes", ["strict_update", "start_only_frozen"])),
            ashare_root=resolve(str(paths["ashare_root"])),
            industry_manifest=resolve(str(paths["industry_manifest"])),
            token_manifest=resolve(str(paths["token_manifest"])),
            output_root=resolve(str(paths["output_root"])),
            cache_root=resolve(str(paths.get("cache_root", "cache"))),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.model_id != "gpt6-Astra-ultra":
            raise ValueError(f"模型标识必须为 gpt6-Astra-ultra，当前为 {self.model_id!r}")
        if self.model_execution not in {"disabled", "dry_run", "explicit"}:
            raise ValueError("model.execution 必须是 disabled、dry_run 或 explicit")
        if self.cutoff_local != "09:25" or self.cutoff_timezone != "Asia/Shanghai":
            raise ValueError("首版截面必须锁定为 Asia/Shanghai 09:25")
        if self.horizons != (20, 60, 120):
            raise ValueError("首版预测窗口必须为 20/60/120 交易日")
        if self.top_k != (1, 3, 5) or self.main_top_k != 5:
            raise ValueError("首版 TopK 必须为 1/3/5，主组合为 Top5")
        if not self.industry_manifest.exists():
            raise FileNotFoundError(self.industry_manifest)


def load_config(path: str | Path) -> ExperimentConfig:
    return ExperimentConfig.from_json(path)
