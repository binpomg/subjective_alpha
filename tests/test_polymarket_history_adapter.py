import json
import pathlib
import sys
import unittest
from urllib.parse import parse_qs, urlparse


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from polymarket_history_adapter import (  # noqa: E402
    PolymarketHistoryAPI,
    PricePoint,
    latest_completed_before,
)


class FakeResponse:
    def __init__(self, payload):
        self.status = 200
        self.headers = {"x-trace-id": "synthetic-trace"}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


class PagingOpener:
    def __init__(self):
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        query = parse_qs(urlparse(request.full_url).query)
        if "cursor" in query:
            return FakeResponse(
                {
                    "data": [
                        {"timestamp": 200, "price": 0.4, "resolution_seconds": 100},
                        {"timestamp": 300, "price": 1.0, "resolution_seconds": 0},
                    ],
                    "pagination": {"has_more": False, "next_cursor": None},
                }
            )
        return FakeResponse(
            {
                "data": [{"timestamp": 0, "price": 0.3, "resolution_seconds": 100}],
                "pagination": {"has_more": True, "next_cursor": "opaque-cursor"},
            }
        )


class AdapterTest(unittest.TestCase):
    def test_cursor_paging_keeps_filters_and_deduplicates(self):
        opener = PagingOpener()
        api = PolymarketHistoryAPI(opener=opener)
        points = api.prices_history(
            "token", interval="max", bucket_seconds=43_200, limit=10
        )
        self.assertEqual([p.timestamp for p in points], [0, 200, 300])
        self.assertEqual(len(opener.urls), 2)
        second_query = parse_qs(urlparse(opener.urls[1]).query)
        self.assertEqual(second_query["interval"], ["max"])
        self.assertEqual(second_query["bucket_seconds"], ["43200"])
        self.assertEqual(second_query["cursor"], ["opaque-cursor"])

    def test_complete_bucket_cutoff_and_terminal_safety(self):
        points = [
            PricePoint(timestamp=100, price=0.3, resolution_seconds=100),
            PricePoint(timestamp=200, price=0.4, resolution_seconds=100),
            PricePoint(timestamp=300, price=1.0, resolution_seconds=0),
        ]
        selected = latest_completed_before(points, 299)
        self.assertEqual(selected.timestamp, 100)
        self.assertIsNone(latest_completed_before(points, 99))
        self.assertEqual(
            latest_completed_before(points, 300, exclude_exact_ticks=False).timestamp, 300
        )

    def test_window_contract_rejects_ambiguous_bounds(self):
        api = PolymarketHistoryAPI(opener=PagingOpener())
        with self.assertRaises(ValueError):
            api.prices_history("token", as_of=100, interval="max")
        with self.assertRaises(ValueError):
            api.prices_history("token", end=100)
        with self.assertRaises(ValueError):
            api.prices_history("token", start=0)
        with self.assertRaises(ValueError):
            api.prices_history("token", as_of=100, bucket_seconds=300)
        with self.assertRaises(ValueError):
            api.prices_history("token", interval="MAX")


if __name__ == "__main__":
    unittest.main()
