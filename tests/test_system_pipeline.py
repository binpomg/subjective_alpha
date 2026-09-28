import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from poly_ashare.config import load_config  # noqa: E402
from poly_ashare.model_gateway import run_sealed_model  # noqa: E402
from poly_ashare.signals import compile_signal  # noqa: E402
from poly_ashare.world import validate_world_output  # noqa: E402
from poly_pit_replay.sealing import seal_json_artifact  # noqa: E402


class SystemPipelineTest(unittest.TestCase):
    def _world(self):
        manifest = json.loads((ROOT / "manifests" / "sw2021_level1_v1.json").read_text(encoding="utf-8"))
        return {
            "schema_version": "world_output.v1",
            "run_id": "synthetic-run",
            "cutoff_utc": "2025-04-04T12:00:00Z",
            "input_sha256": "a" * 64,
            "poly_only": True,
            "industry_ranking": [
                {"industry_code": row["code"], "rank": i + 1, "confidence": 0.9 if i < 5 else 0.6, "maturity": "developing"}
                for i, row in enumerate(manifest["industries"])
            ],
            "relation_chains": [],
            "falsifiers": [],
        }

    def test_config_and_model_gate(self):
        cfg = load_config(ROOT / "config" / "experiment_config.json")
        self.assertEqual(cfg.model_id, "gpt6-Astra-ultra")
        snapshot = {
            "schema_version": "poly_price_snapshot.v1",
            "experiment": {
                "poly_only": True,
                "external_access": "disabled",
                "ashare_visibility": "none",
            },
            "cutoff_utc": "2025-04-04T12:00:00Z",
            "rows": [],
            "coverage": {"tokens_requested": 0, "tokens_with_usable_point": 0},
            "audit": [],
        }
        run = run_sealed_model(snapshot, model_id=cfg.model_id, execution=cfg.model_execution)
        self.assertEqual(run.status, "NOT_STARTED")

    def test_world_to_top5_signal_is_sealed(self):
        world = self._world()
        self.assertEqual(validate_world_output(world), [])
        signal = compile_signal(world, top_k=5)
        self.assertEqual(signal["status"], "ACTIVE")
        self.assertEqual(len(signal["targets"]), 5)
        with tempfile.TemporaryDirectory() as tmp:
            seal = seal_json_artifact(pathlib.Path(tmp) / "signal.json", signal, artifact_type="signal_output")
            self.assertEqual(seal["artifact_type"], "signal_output")


if __name__ == "__main__":
    unittest.main()
