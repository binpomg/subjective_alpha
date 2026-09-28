import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from batch_build_poly_snapshots import build_batch  # noqa: E402


class BatchSnapshotBuilderTest(unittest.TestCase):
    def test_builds_sealed_snapshots_from_one_cached_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw"
            out = root / "snapshots"
            raw.mkdir()
            payload = {
                "data": [
                    {"timestamp": 100, "price": 0.4, "resolution_seconds": 10},
                    {"timestamp": 110, "price": 0.5, "resolution_seconds": 10},
                    {"timestamp": 120, "price": 1.0, "resolution_seconds": 0},
                ]
            }
            (raw / "token.json").write_text(json.dumps(payload), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "strict_pit_eligible": False,
                        "tokens": [{"token_id": "t1", "market_id": "m1", "raw_file": "token.json"}],
                    }
                ),
                encoding="utf-8",
            )
            schedule = root / "schedule.json"
            schedule.write_text(
                json.dumps(
                    {
                        "schema_version": "poly_cutoff_schedule.v1",
                        "cutoffs": [
                            {"cutoff_id": "c2", "cutoff_utc": 125},
                            {"cutoff_id": "c1", "cutoff_utc": 115, "trade_date": "2025-01-02"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = build_batch(manifest, raw, schedule, out, batch_id="test_batch")
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["snapshot_count"], 2)
            self.assertEqual([x["cutoff_id"] for x in result["snapshots"]], ["c1", "c2"])
            first = json.loads((out / "c1.json").read_text(encoding="utf-8"))
            second = json.loads((out / "c2.json").read_text(encoding="utf-8"))
            self.assertEqual(first["rows"][0]["timestamp"], 100)
            self.assertEqual(second["rows"][0]["timestamp"], 110)
            self.assertEqual(first["coverage"]["tokens_with_usable_point"], 1)
            self.assertEqual(second["coverage"]["tokens_with_usable_point"], 1)
            self.assertTrue((out / "batch_manifest.json").exists())

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw"
            out = root / "snapshots"
            raw.mkdir()
            (raw / "token.json").write_text(json.dumps({"data": []}), encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps({"tokens": [{"token_id": "t1", "raw_file": "token.json"}]}), encoding="utf-8"
            )
            (root / "schedule.json").write_text(json.dumps([100]), encoding="utf-8")
            build_batch(root / "manifest.json", raw, root / "schedule.json", out, batch_id="first")
            with self.assertRaises(FileExistsError):
                build_batch(root / "manifest.json", raw, root / "schedule.json", out, batch_id="second")


if __name__ == "__main__":
    unittest.main()
