import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from poly_pit_replay import (  # noqa: E402
    SchemaValidationError,
    seal_json_artifact,
    validate_research_input,
    validate_signal_output,
    validate_world_output,
    verify_seal,
)
from poly_ashare.artifacts import freeze_artifact  # noqa: E402


class ArtifactContractsTest(unittest.TestCase):
    @staticmethod
    def _research_input():
        return {
            "schema_version": "poly_price_snapshot.v1",
            "experiment": {
                "poly_only": True,
                "external_access": "disabled",
                "ashare_visibility": "none",
            },
            "cutoff_utc": "2025-04-04T12:00:00Z",
            "rows": [
                {
                    "token_id": "synthetic-token",
                    "timestamp": "2025-04-04T10:00:00Z",
                    "point_end": "2025-04-04T11:00:00Z",
                    "price": 0.5,
                    "resolution_seconds": 3600,
                }
            ],
            "coverage": {"tokens_requested": 1, "tokens_with_usable_point": 1},
            "audit": [{"token_id": "synthetic-token", "usable": True}],
        }

    def test_existing_debug_snapshot_is_valid_research_input(self):
        obj = self._research_input()
        self.assertEqual(validate_research_input(obj)["schema_version"], "poly_price_snapshot.v1")

    def test_research_input_rejects_future_point(self):
        obj = {
            "schema_version": "poly_price_snapshot.v1",
            "experiment": {
                "poly_only": True,
                "external_access": "disabled",
                "ashare_visibility": "none",
            },
            "cutoff_utc": "2025-01-01T00:00:00Z",
            "rows": [
                {
                    "token_id": "t",
                    "timestamp": 1735689600,
                    "point_end": 1735693200,
                    "price": 0.5,
                    "resolution_seconds": 3600,
                }
            ],
            "coverage": {"tokens_requested": 1, "tokens_with_usable_point": 1},
            "audit": [{}],
        }
        with self.assertRaises(SchemaValidationError):
            validate_research_input(obj)

    def test_research_input_rejects_a_share_fields(self):
        obj = self._research_input()
        obj["A_share_rows_at_T"] = []
        with self.assertRaises(SchemaValidationError):
            validate_research_input(obj)

    def test_world_and_signal_contracts(self):
        world = {
            "schema_version": "world_output.v1",
            "run_id": "run-1",
            "cutoff_utc": "2025-01-01T00:00:00Z",
            "input_sha256": "a" * 64,
            "poly_only": True,
            "industry_ranking": [
                {
                    "industry_code": "27",
                    "rank": 1,
                    "confidence": 0.8,
                    "maturity": "developing",
                }
            ],
            "relation_chains": [],
            "falsifiers": [],
        }
        self.assertEqual(validate_world_output(world)["run_id"], "run-1")
        signal = {
            "schema_version": "signal_output.v1",
            "signal_id": "sig-1",
            "world_output_sha256": "b" * 64,
            "cutoff_utc": "2025-01-01T00:00:00Z",
            "status": "ACTIVE",
            "portfolio_id": "top5",
            "targets": [
                {"industry_code": "27", "action": "BUY", "target_weight": 0.2}
            ],
        }
        self.assertEqual(validate_signal_output(signal)["status"], "ACTIVE")

        no_trade = dict(signal)
        no_trade["status"] = "NO_TRADE"
        no_trade["targets"] = []
        self.assertEqual(validate_signal_output(no_trade)["status"], "NO_TRADE")

    def test_seal_refuses_overwrite_and_detects_tamper(self):
        obj = {
            "schema_version": "world_output.v1",
            "run_id": "run-1",
            "cutoff_utc": "2025-01-01T00:00:00Z",
            "input_sha256": "a" * 64,
            "poly_only": True,
            "industry_ranking": [
                {
                    "industry_code": "27",
                    "rank": 1,
                    "confidence": "high",
                    "maturity": "developing",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "world_output.json"
            seal = seal_json_artifact(path, obj, artifact_type="world_output")
            self.assertTrue(verify_seal(path, seal["sha256"]))
            with self.assertRaises(FileExistsError):
                seal_json_artifact(path, obj, artifact_type="world_output")
            path.write_text(path.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_seal(path, seal["sha256"])

    def test_legacy_freeze_entrypoint_validates_known_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "world.json"
            path.write_text(json.dumps({"schema_version": "world_output.v1"}), encoding="utf-8")
            with self.assertRaises(SchemaValidationError):
                freeze_artifact(path, artifact_type="world_output")


if __name__ == "__main__":
    unittest.main()
