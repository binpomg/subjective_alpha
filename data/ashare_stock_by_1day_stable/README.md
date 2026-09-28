# Remote A-share data mount point

The large Windows `stock_by_1day_stable` dataset is intentionally not copied
by deployment. Mount or copy a compatible stable daily Parquet dataset here
before running `scripts/industry_eval.py`.

Required fields include `trade_date`, `ts_code`, `adj_open`, `adj_close`,
`record_status`, `stock_is_listed_asof`, and `is_suspended`. Keep raw source
data outside this repository when possible; the evaluator writes derived
memberships and labels under `outputs/`.
