"""Offline contract tests for industry_eval using synthetic DuckDB Parquet."""

from __future__ import annotations

import csv
import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "industry_eval.py"
spec = importlib.util.spec_from_file_location("industry_eval", MODULE_PATH)
assert spec and spec.loader
industry_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(industry_eval)


def _make_synthetic_inputs(tmp_path: Path) -> tuple[Path, Path]:
    duckdb = industry_eval._duckdb()
    price_root = tmp_path / "schema_version=daily_1min_v1"
    price_rows: list[tuple] = []
    first = date(2021, 1, 4)
    for offset in range(5):
        d = first + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        # Symbol 000001 is strict-available on day one.  Symbol 000002 is
        # effective on day one but its update date is later; this distinguishes
        # strict_update from start_only in the first snapshot.
        price_rows.extend(
            [
                (d, "000001.SZ", 100.0 + offset, 101.0 + offset, "valid_trading", True, False),
                (d, "000002.SZ", 200.0 + offset, 202.0 + offset, "valid_trading", True, False),
            ]
        )
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE p(trade_date DATE, ts_code VARCHAR, adj_open DOUBLE, adj_close DOUBLE, "
            "record_status VARCHAR, stock_is_listed_asof BOOLEAN, is_suspended BOOLEAN)"
        )
        con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?)", price_rows)
        for row in con.execute("SELECT DISTINCT trade_date FROM p ORDER BY trade_date").fetchall():
            d = row[0]
            path = price_root / str(d.year) / d.strftime("%Y%m%d") / "part-000.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            con.execute(
                f"COPY (SELECT * FROM p WHERE trade_date = ?) TO '{path}' (FORMAT PARQUET)",
                [d],
            )
    finally:
        con.close()

    sw_history = tmp_path / "sw_industry_history.csv"
    with sw_history.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "start_date", "industry_code", "update_time"])
        writer.writeheader()
        writer.writerow({"symbol": "000001", "start_date": "2021-01-01", "industry_code": "110101", "update_time": "2021-01-01"})
        writer.writerow({"symbol": "000002", "start_date": "2021-01-01", "industry_code": "220101", "update_time": "2021-01-05"})
    return price_root, sw_history


class IndustryEvalTests(unittest.TestCase):
  def test_strict_and_start_only_membership_and_labels(self) -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
      tmp_path = Path(td)
      price_root, sw_history = _make_synthetic_inputs(tmp_path)
      result = industry_eval.build_industry_eval(
          price_root,
          sw_history,
          tmp_path / "out",
          start_date="2021-01-04",
          end_date="2021-01-04",
          horizons=(2,),
          min_members=1,
          min_coverage=0.0,
      )
      self.assertTrue(Path(result["metadata_path"]).exists())
      con = industry_eval._duckdb().connect()
      try:
          strict_members_path = tmp_path / "out" / "strict_update" / "members.parquet"
          start_members_path = tmp_path / "out" / "start_only" / "members.parquet"
          strict_members = con.execute(
              f"SELECT symbol, industry_code2 FROM read_parquet('{strict_members_path}')"
          ).fetchall()
          start_members = con.execute(
              f"SELECT symbol, industry_code2 FROM read_parquet('{start_members_path}')"
          ).fetchall()
          self.assertEqual(strict_members, [("000001", "11")])
          self.assertEqual(set(start_members), {("000001", "11"), ("000002", "22")})
          strict_label_path = tmp_path / "out" / "strict_update" / "industry_labels_2d.parquet"
          strict_label = con.execute(
              f"SELECT industry_return, label_valid FROM read_parquet('{strict_label_path}') WHERE industry_code2='11'"
          ).fetchone()
          self.assertIs(strict_label[1], True)
          self.assertAlmostEqual(strict_label[0], (103.0 / 100.0) - 1.0)
          coverage_path = tmp_path / "out" / "strict_update" / "coverage_audit.csv"
          coverage = con.execute(
              f"SELECT missing_universe_count, universe_count FROM read_csv('{coverage_path}', AUTO_DETECT=TRUE) WHERE industry_code2='11'"
          ).fetchone()
          self.assertEqual(coverage, (1, 2))
      finally:
          con.close()

  def test_schema_error_is_clear(self) -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
      tmp_path = Path(td)
      duckdb = industry_eval._duckdb()
      root = tmp_path / "schema_version=daily_1min_v1" / "2021" / "20210104"
      root.mkdir(parents=True)
      con = duckdb.connect()
      try:
          con.execute("CREATE TABLE p(trade_date DATE, ts_code VARCHAR)")
          con.execute(f"COPY p TO '{root / 'part-000.parquet'}' (FORMAT PARQUET)")
      finally:
          con.close()
      sw = tmp_path / "sw.csv"
      sw.write_text("symbol,start_date,industry_code,update_time\n000001,2021-01-01,110101,2021-01-01\n", encoding="utf-8")
      with self.assertRaisesRegex(industry_eval.IndustryEvalError, "缺少行业回测所需字段"):
          industry_eval.build_industry_eval(root.parents[1], sw, tmp_path / "out", start_date="2021-01-04", end_date="2021-01-04", horizons=(2,))


if __name__ == "__main__":
  unittest.main()
