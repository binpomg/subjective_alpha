import pathlib
import sys
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from build_poly_snapshot import build_snapshot, select_latest_usable_point  # noqa: E402


class SnapshotBuilderTest(unittest.TestCase):
    def test_rejects_incomplete_bucket_and_terminal_point(self):
        point, audit = select_latest_usable_point(
            [
                {
                    "data": [
                        {"timestamp": 100, "price": 0.20, "resolution_seconds": 50},
                        {"timestamp": 120, "price": 0.30, "resolution_seconds": 100},
                        {"timestamp": 150, "price": 1.00, "resolution_seconds": 0},
                    ]
                }
            ],
            160,
        )
        self.assertEqual(point["timestamp"], 100)
        self.assertEqual(audit["bucket_not_complete"], 1)
        self.assertEqual(audit["resolution_zero_excluded"], 1)

    def test_snapshot_keeps_manifest_identity_and_coverage(self):
        snapshot = build_snapshot(
            [
                {
                    "market_id": "m1",
                    "outcome": "Yes",
                    "token_id": "t1",
                    "question": "Synthetic question",
                    "payload": {
                        "data": [
                            {"timestamp": 100, "price": 0.6, "resolution_seconds": 10}
                        ]
                    },
                },
                {
                    "market_id": "m2",
                    "outcome": "No",
                    "token_id": "t2",
                    "payload": {"data": []},
                },
            ],
            120,
        )
        self.assertEqual(snapshot["coverage"]["tokens_requested"], 2)
        self.assertEqual(snapshot["coverage"]["tokens_with_usable_point"], 1)
        self.assertEqual(snapshot["rows"][0]["market_id"], "m1")
        self.assertEqual(snapshot["rows"][0]["question"], "Synthetic question")
        self.assertEqual(snapshot["rows"][0]["point_end"], 110)


if __name__ == "__main__":
    unittest.main()
