#!/usr/bin/env python3
"""Build point-in-time SW level-1 industry baskets and forward labels.

The module is deliberately read-only with respect to the source data.  It
uses DuckDB to read the daily Parquet partitions and the normalized SW history
CSV, then writes new Parquet/CSV artifacts under an output directory.

Two mapping modes are available:

``strict_update``
    A classification row is usable only when both ``start_date`` and
    ``update_time`` are no later than the signal date.  This is conservative
    when ``update_time`` is treated as an availability proxy.

``start_only``
    Uses the effective ``start_date`` only.  It is useful as a frozen mapping
    sensitivity, but must be reported as non-strict PIT unless archived source
    snapshots prove that the row was observable at the signal date.

The generated labels are equal-weight stock returns from the signal-day
adjusted open to the close on the H-th following trading day.  The output
contains member/return coverage, so callers can impose their own minimum
coverage policy rather than silently filling missing observations.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence


INDUSTRIES: tuple[tuple[str, str], ...] = (
    ("11", "农林牧渔"),
    ("22", "基础化工"),
    ("23", "钢铁"),
    ("24", "有色金属"),
    ("27", "电子"),
    ("28", "汽车"),
    ("33", "家用电器"),
    ("34", "食品饮料"),
    ("35", "纺织服饰"),
    ("36", "轻工制造"),
    ("37", "医药生物"),
    ("41", "公用事业"),
    ("42", "交通运输"),
    ("43", "房地产"),
    ("45", "商贸零售"),
    ("46", "社会服务"),
    ("48", "银行"),
    ("49", "非银金融"),
    ("51", "综合"),
    ("61", "建筑材料"),
    ("62", "建筑装饰"),
    ("63", "电力设备"),
    ("64", "机械设备"),
    ("65", "国防军工"),
    ("71", "计算机"),
    ("72", "传媒"),
    ("73", "通信"),
    ("74", "煤炭"),
    ("75", "石油石化"),
    ("76", "环保"),
    ("77", "美容护理"),
)
INDUSTRY_CODES = tuple(code for code, _ in INDUSTRIES)
VALID_MODES = ("strict_update", "start_only")


class IndustryEvalError(RuntimeError):
    """Raised when inputs fail the minimum schema or configuration checks."""


def _duckdb():
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise IndustryEvalError(
            "本模块需要 DuckDB。请先安装：`py -3.13 -m pip install duckdb`，"
            "然后重新运行。"
        ) from exc
    return duckdb


def _sql_literal(value: str | Path) -> str:
    """Return a safely quoted SQL string literal for a local path/value."""

    return "'" + str(value).replace("'", "''") + "'"


def _date_literal(value: date | str | None, default: str) -> str:
    if value is None:
        return f"DATE '{default}'"
    text = value.isoformat() if isinstance(value, date) else str(value)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise IndustryEvalError(f"日期格式必须是 YYYY-MM-DD：{text}") from exc
    return f"DATE '{text}'"


def _parse_horizons(values: Iterable[int] | str) -> tuple[int, ...]:
    if isinstance(values, str):
        values = (int(v.strip()) for v in values.split(",") if v.strip())
    result = tuple(sorted({int(v) for v in values}))
    if not result or any(v <= 0 for v in result):
        raise IndustryEvalError("horizons 必须是正整数，例如 20,60,120")
    return result


def _validate_paths(price_root: Path, sw_history: Path) -> str:
    if not price_root.exists():
        raise IndustryEvalError(f"价格目录不存在：{price_root}")
    if not sw_history.exists():
        raise IndustryEvalError(f"SW历史分类文件不存在：{sw_history}")
    # DuckDB accepts forward slashes on Windows and Linux.  Keep the glob
    # narrow to YYYY/YYYYMMDD/part*.parquet partitions.
    return (price_root / "*" / "*" / "*.parquet").as_posix()


def _copy_query(con, query: str, output: Path, fmt: str = "PARQUET") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    options = "FORMAT PARQUET" if fmt.upper() == "PARQUET" else "HEADER, DELIMITER ','"
    con.execute(f"COPY ({query}) TO {_sql_literal(output)} ({options})")


def _create_industry_values(con) -> None:
    values = ",".join(f"('{code}', '{name}')" for code, name in INDUSTRIES)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE industry_codes AS "
        f"SELECT * FROM (VALUES {values}) AS x(code2, industry_name)"
    )


def _check_price_schema(con, parquet_glob: str) -> None:
    try:
        rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet({_sql_literal(parquet_glob)}) LIMIT 0"
        ).fetchall()
    except Exception as exc:
        raise IndustryEvalError(
            "价格目录中找不到可读取的 Parquet 分区，期望路径为 YYYY/YYYYMMDD/*.parquet："
            + parquet_glob
        ) from exc
    names = {str(row[0]) for row in rows}
    required = {
        "trade_date",
        "ts_code",
        "adj_open",
        "adj_close",
        "record_status",
        "stock_is_listed_asof",
        "is_suspended",
    }
    missing = sorted(required - names)
    if missing:
        raise IndustryEvalError(
            "稳定行情 Parquet 缺少行业回测所需字段：" + ", ".join(missing)
        )


def _prepare_prices(con, parquet_glob: str, start_date: str | None, load_end: date | None) -> None:
    start_expr = _date_literal(start_date, "1900-01-01")
    end_expr = _date_literal(load_end, "2999-12-31")
    # Keep only fields needed for membership, endpoint returns and audit.  The
    # source files remain untouched; this is a DuckDB temporary table.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE prices AS
        SELECT
            CAST(trade_date AS DATE) AS trade_date,
            SUBSTR(CAST(ts_code AS VARCHAR), 1, 6) AS symbol,
            TRY_CAST(adj_open AS DOUBLE) AS adj_open,
            TRY_CAST(adj_close AS DOUBLE) AS adj_close,
            CAST(record_status AS VARCHAR) AS record_status,
            COALESCE(CAST(stock_is_listed_asof AS BOOLEAN), FALSE) AS listed,
            COALESCE(CAST(is_suspended AS BOOLEAN), FALSE) AS suspended
        FROM read_parquet({_sql_literal(parquet_glob)})
        WHERE CAST(trade_date AS DATE) >= {start_expr}
          AND CAST(trade_date AS DATE) <= {end_expr}
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE calendar AS
        SELECT trade_date,
               ROW_NUMBER() OVER (ORDER BY trade_date) AS date_index
        FROM (
            SELECT DISTINCT trade_date
            FROM prices
            WHERE record_status = 'valid_trading' AND listed
        )
        """
    )


def _prepare_mapping(con, sw_history: Path) -> None:
    path = _sql_literal(sw_history.as_posix())
    codes = ",".join(_sql_literal(code) for code in INDUSTRY_CODES)
    try:
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE sw_history AS
            SELECT
                LPAD(CAST(symbol AS VARCHAR), 6, '0') AS symbol,
                TRY_CAST(start_date AS DATE) AS start_date,
                LPAD(CAST(industry_code AS VARCHAR), 6, '0') AS industry_code6,
                SUBSTR(LPAD(CAST(industry_code AS VARCHAR), 6, '0'), 1, 2) AS code2,
                TRY_CAST(update_time AS DATE) AS update_time
            FROM read_csv_auto({path}, HEADER = TRUE)
            WHERE SUBSTR(LPAD(CAST(industry_code AS VARCHAR), 6, '0'), 1, 2)
                  IN ({codes})
            """
        )
    except Exception as exc:
        raise IndustryEvalError(
            "SW历史分类CSV无法读取，且必须包含 symbol/start_date/industry_code/update_time："
            + str(sw_history)
        ) from exc


def _prepare_targets(con, signal_start: str | None, signal_end: str | None, horizons: Sequence[int]) -> None:
    start_expr = _date_literal(signal_start, "1900-01-01")
    end_expr = _date_literal(signal_end, "2999-12-31")
    values = ",".join(f"({int(h)})" for h in horizons)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE targets AS
        SELECT c.trade_date AS signal_date,
               h.horizon,
               target.trade_date AS target_date
        FROM calendar c
        CROSS JOIN (VALUES {values}) AS h(horizon)
        LEFT JOIN calendar target
          ON target.date_index = c.date_index + h.horizon
        WHERE c.trade_date >= {start_expr} AND c.trade_date <= {end_expr}
        """
    )


def _build_membership(con, mode: str) -> None:
    if mode not in VALID_MODES:
        raise IndustryEvalError(f"未知 mapping mode：{mode}")
    update_condition = "AND s.update_time <= c.trade_date" if mode == "strict_update" else ""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE mapping_asof AS
        SELECT trade_date AS signal_date, symbol, industry_code6, code2,
               start_date, update_time
        FROM (
            SELECT c.trade_date, s.symbol, s.industry_code6, s.code2,
                   s.start_date, s.update_time,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.trade_date, s.symbol
                       ORDER BY s.start_date DESC, s.update_time DESC, s.industry_code6 DESC
                   ) AS rn
            FROM calendar c
            JOIN sw_history s
              ON s.start_date <= c.trade_date
             {update_condition}
        )
        WHERE rn = 1
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE membership AS
        SELECT p.trade_date AS signal_date, p.symbol, m.code2, m.industry_code6,
               m.start_date, m.update_time, p.adj_open, p.suspended
        FROM prices p
        JOIN (SELECT DISTINCT signal_date FROM targets) d
          ON d.signal_date = p.trade_date
        JOIN mapping_asof m
          ON m.signal_date = p.trade_date AND m.symbol = p.symbol
        WHERE p.record_status = 'valid_trading'
          AND p.listed
          AND p.adj_open IS NOT NULL AND p.adj_open > 0
        """
    )


def _labels_query(horizon: int, min_members: int, min_coverage: float, mode: str) -> str:
    # ``targets`` contains one row per signal date and horizon.  The endpoint
    # join intentionally requires a valid trading row on the target date.  A
    # suspended/absent endpoint therefore lowers return coverage and is never
    # silently forward-filled.
    return f"""
    WITH member_counts AS (
        SELECT signal_date, code2, COUNT(*) AS member_count,
               COUNT(*) FILTER (WHERE NOT suspended) AS eligible_member_count,
               COUNT(*) FILTER (WHERE suspended) AS suspended_member_count
        FROM membership
        GROUP BY signal_date, code2
    ),
    stock_returns AS (
        SELECT m.signal_date, m.code2, m.symbol, t.target_date,
               p.adj_close / m.adj_open - 1.0 AS stock_return
        FROM membership m
        JOIN targets t
          ON t.signal_date = m.signal_date AND t.horizon = {int(horizon)}
        JOIN prices p
          ON p.trade_date = t.target_date AND p.symbol = m.symbol
         AND p.record_status = 'valid_trading' AND p.listed
         AND NOT p.suspended
         AND p.adj_close IS NOT NULL AND p.adj_close > 0
        WHERE NOT m.suspended
    ),
    returns AS (
        SELECT signal_date, code2, MAX(target_date) AS target_date,
               COUNT(*) AS return_count, AVG(stock_return) AS industry_return
        FROM stock_returns
        GROUP BY signal_date, code2
    ),
    raw AS (
        SELECT t.signal_date, t.target_date, t.horizon,
               i.code2, i.industry_name,
               COALESCE(mc.member_count, 0) AS member_count,
               COALESCE(mc.eligible_member_count, 0) AS eligible_member_count,
               COALESCE(mc.suspended_member_count, 0) AS suspended_member_count,
               COALESCE(r.return_count, 0) AS return_count,
               CASE WHEN COALESCE(mc.member_count, 0) > 0
                    THEN COALESCE(r.return_count, 0)::DOUBLE / mc.member_count
                    ELSE 0.0 END AS coverage_ratio,
               r.industry_return,
               (COALESCE(mc.member_count, 0) >= {int(min_members)}
                AND COALESCE(r.return_count, 0) >= {int(min_members)}
                AND CASE WHEN COALESCE(mc.member_count, 0) > 0
                         THEN COALESCE(r.return_count, 0)::DOUBLE / mc.member_count
                         ELSE 0.0 END >= {float(min_coverage)}) AS label_valid
        FROM targets t
        CROSS JOIN industry_codes i
        LEFT JOIN member_counts mc
          ON mc.signal_date = t.signal_date AND mc.code2 = i.code2
        LEFT JOIN returns r
          ON r.signal_date = t.signal_date AND r.code2 = i.code2
         AND t.horizon = {int(horizon)}
        WHERE t.horizon = {int(horizon)}
    ),
    scored AS (
        SELECT raw.*,
               AVG(industry_return) FILTER (WHERE label_valid)
                   OVER (PARTITION BY signal_date, horizon) AS benchmark_return,
               COUNT(*) FILTER (WHERE label_valid)
                   OVER (PARTITION BY signal_date, horizon) AS benchmark_industry_count
        FROM raw
    )
    SELECT '{mode}' AS mapping_mode, signal_date, target_date, horizon, code2 AS industry_code2,
           industry_name, member_count, eligible_member_count,
           suspended_member_count, return_count, coverage_ratio,
           industry_return, benchmark_return,
           CASE WHEN benchmark_industry_count = {len(INDUSTRIES)}
                THEN industry_return - benchmark_return ELSE NULL END AS excess_return,
           label_valid,
           benchmark_industry_count,
           (benchmark_industry_count = {len(INDUSTRIES)}) AS benchmark_valid
    FROM scored
    """


def _coverage_query(mode: str) -> str:
    # Include all 31 rows per signal date.  ``universe_count`` is the number of
    # listed/valid rows before mapping; ``missing_count`` makes future mapping
    # gaps visible instead of dropping them from the report.
    return """
    WITH universe AS (
        SELECT trade_date AS signal_date, symbol,
               COUNT(*) FILTER (WHERE NOT suspended) AS eligible_row,
               COUNT(*) FILTER (WHERE suspended) AS suspended_row
        FROM prices
        WHERE record_status = 'valid_trading' AND listed
        GROUP BY trade_date, symbol
    ),
    mapped AS (
        SELECT u.signal_date, u.symbol, m.code2,
               u.eligible_row, u.suspended_row
        FROM universe u
        LEFT JOIN mapping_asof m
          ON m.signal_date = u.signal_date AND m.symbol = u.symbol
    ),
    counts AS (
        SELECT signal_date, code2,
               COUNT(*) AS mapped_count,
               COUNT(*) FILTER (WHERE eligible_row = 1) AS eligible_member_count,
               COUNT(*) FILTER (WHERE suspended_row = 1) AS suspended_member_count
        FROM mapped
        WHERE code2 IS NOT NULL
        GROUP BY signal_date, code2
    ),
    universe_counts AS (
        SELECT signal_date, COUNT(*) AS universe_count,
               COUNT(*) FILTER (WHERE code2 IS NULL) AS missing_universe_count
        FROM mapped
        GROUP BY signal_date
    ),
    signal_dates AS (SELECT DISTINCT signal_date FROM targets)
    SELECT '{mode}' AS mapping_mode, d.signal_date, i.code2 AS industry_code2, i.industry_name,
           COALESCE(c.mapped_count, 0) AS mapped_count,
           COALESCE(c.eligible_member_count, 0) AS eligible_member_count,
           COALESCE(c.suspended_member_count, 0) AS suspended_member_count,
           COALESCE(u.missing_universe_count, 0) AS missing_universe_count,
           COALESCE(u.universe_count, 0) AS universe_count,
           CASE WHEN COALESCE(u.universe_count, 0) > 0
                THEN 1.0 - COALESCE(u.missing_universe_count, 0)::DOUBLE / u.universe_count
                ELSE 0.0 END AS mapping_coverage_ratio,
           COALESCE(c.eligible_member_count, 0)::DOUBLE /
               NULLIF(COALESCE(c.mapped_count, 0), 0) AS eligible_ratio
    FROM signal_dates d
    CROSS JOIN industry_codes i
    LEFT JOIN counts c
      ON c.signal_date = d.signal_date AND c.code2 = i.code2
    LEFT JOIN universe_counts u
      ON u.signal_date = d.signal_date
    """


def build_industry_eval(
    price_root: str | Path,
    sw_history: str | Path,
    output_dir: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    modes: Sequence[str] = VALID_MODES,
    horizons: Iterable[int] = (20, 60, 120),
    min_members: int = 5,
    min_coverage: float = 0.8,
) -> dict[str, object]:
    """Build members, labels and coverage artifacts for the selected period.

    Parameters are intentionally explicit so a run can be serialized into a
    manifest.  ``end_date`` refers to signal dates; the loader automatically
    reads up to 550 calendar days later when necessary for 120-trading-day
    endpoints.  No source file is overwritten.
    """

    price_root = Path(price_root)
    sw_history = Path(sw_history)
    output_dir = Path(output_dir)
    parquet_glob = _validate_paths(price_root, sw_history)
    horizons_tuple = _parse_horizons(horizons)
    if isinstance(modes, str):
        modes = (modes,)
    modes_tuple = tuple(dict.fromkeys(modes))
    if any(mode not in VALID_MODES for mode in modes_tuple):
        raise IndustryEvalError(f"modes 只能取 {VALID_MODES}")
    if min_members <= 0:
        raise IndustryEvalError("min_members 必须为正整数")
    if not 0.0 <= float(min_coverage) <= 1.0:
        raise IndustryEvalError("min_coverage 必须在 0 到 1 之间")
    if start_date and end_date and date.fromisoformat(start_date) > date.fromisoformat(end_date):
        raise IndustryEvalError("start_date 不能晚于 end_date")

    # The extra load window supplies endpoints for the final signal dates.  If
    # the source ends sooner, labels are retained as invalid with null returns.
    load_end = date.fromisoformat(end_date) + timedelta(days=550) if end_date else None
    duckdb = _duckdb()
    con = duckdb.connect()
    try:
        _check_price_schema(con, parquet_glob)
        _create_industry_values(con)
        _prepare_prices(con, parquet_glob, start_date, load_end)
        _prepare_mapping(con, sw_history)
        _prepare_targets(con, start_date, end_date, horizons_tuple)
        results: dict[str, object] = {
            "price_root": str(price_root),
            "sw_history": str(sw_history),
            "start_date": start_date,
            "end_date": end_date,
            "horizons": list(horizons_tuple),
            "min_members": int(min_members),
            "min_coverage": float(min_coverage),
            "modes": {},
        }
        for mode in modes_tuple:
            _build_membership(con, mode)
            mode_dir = output_dir / mode
            mode_dir.mkdir(parents=True, exist_ok=True)
            members_path = mode_dir / "members.parquet"
            # Do not write rows with no mapping into the member table; they are
            # retained in coverage_audit.csv as missing rows.
            _copy_query(
                con,
                f"SELECT '{mode}' AS mapping_mode, signal_date, symbol, "
                "code2 AS industry_code2, industry_code6, start_date, update_time, suspended "
                "FROM membership",
                members_path,
            )
            label_paths: list[str] = []
            for horizon in horizons_tuple:
                label_path = mode_dir / f"industry_labels_{horizon}d.parquet"
                _copy_query(con, _labels_query(horizon, min_members, float(min_coverage), mode), label_path)
                label_paths.append(str(label_path))
            coverage_path = mode_dir / "coverage_audit.csv"
            _copy_query(con, _coverage_query(mode), coverage_path, fmt="CSV")
            mode_counts = con.execute(
                "SELECT COUNT(*) AS n FROM membership"
            ).fetchone()[0]
            results["modes"][mode] = {
                "members_path": str(members_path),
                "label_paths": label_paths,
                "coverage_path": str(coverage_path),
                "member_rows": int(mode_counts),
            }
        metadata_path = output_dir / "run_metadata.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        results["metadata_path"] = str(metadata_path)
        metadata_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return results
    finally:
        con.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-root", required=True, help="schema_version=daily_1min_v1 目录")
    parser.add_argument("--sw-history", required=True, help="sw_industry_history.csv")
    parser.add_argument("--output-dir", required=True, help="新建输出目录")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--modes", default=",".join(VALID_MODES))
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--min-coverage", type=float, default=0.8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = build_industry_eval(
            args.price_root,
            args.sw_history,
            args.output_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            modes=tuple(v.strip() for v in args.modes.split(",") if v.strip()),
            horizons=args.horizons,
            min_members=args.min_members,
            min_coverage=args.min_coverage,
        )
    except (IndustryEvalError, ValueError, OSError) as exc:
        print(f"industry_eval error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
