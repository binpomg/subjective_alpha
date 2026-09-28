# Poly → A股行业排序封闭实验系统

这是一个本地、只读、可复算的研究框架。它把 Polymarket 的历史价格截面交给封闭模型，要求模型输出 31 个申万一级行业的排序，再由独立评估端读取外部提供的 A 股历史数据计算行业标签和组合结果。仓库只包含代码、契约、配置模板和测试；数据文件由使用者在本地准备。

系统当前配置的模型标识是 `gpt6-Astra-ultra`，但默认执行模式是 `disabled`。构建、缓存、校验、信号编译和回测代码已经准备好；模型调用、常驻采集和真实下单均未启动。

## 安全边界

本仓库不保存 A 股行情、Polymarket 原始响应或模型/API 密钥。请把这些内容放在仓库目录之外，或放入已被 `.gitignore` 排除的本地目录；密钥只通过运行环境的安全凭据注入，绝不要写进 JSON、提示词、日志或命令行参数。发布前请检查 `git diff --cached --name-only` 和 `git diff --cached`。

## 目录结构

```text
cache/                    # Polymarket 原始历史缓存，可重复调用
manifests/                # token universe、行业分类和版本登记
snapshots/                # 单个历史截止点的 Poly-only 输入
batch_snapshots/          # 批量冻结的历史截面与 batch_manifest
outputs/                  # world_output、signal_output、评估结果
scripts/                  # API、缓存、截面和行业标签脚本
poly_pit_replay/          # 研究输入/模型输出/信号的严格契约与哈希封存
src/poly_ashare/          # 配置、模型门禁、信号、指标和回测接口
tests/                    # 离线单元测试
```

## 重要边界

- 研究输入不包含 A 股价格、行业标签、公司资料、新闻、未来收益或结算结果。
- 聚合盘口点必须满足 `timestamp + resolution_seconds <= cutoff`；`resolution_seconds=0` 默认拒绝。
- token 历史先保存到 `cache/raw/`，单日截面只从本地缓存生成；不重复请求官方 API。
- `poly_pit_replay.seal_json_artifact` 是正式封存入口，默认拒绝覆盖已封存文件。
- 当前官方 API 无法恢复完整历史市场目录和历史规则版本，正式结果只能称为预登记 token 的可重建子集。

## 常用命令

在项目根目录使用已配置的 Python（以下命令不依赖具体机器路径）：

```powershell
$py = 'python'
$root = (Get-Location).Path
$env:PYTHONPATH = "$root\src;$root"

& $py -m unittest discover -s "$root\tests" -v
& $py -m poly_ashare.cli validate-config --config "$root\config\experiment_config.json"
& $py -m poly_ashare.cli prepare --config "$root\config\experiment_config.json"
```

先在项目外准备 token 清单。全量市场目录应使用 Gamma keyset 分页发现并冻结：

```powershell
& $py "$root\scripts\discover_poly_universe.py" `
  --out "$root\cache\private_universe\universe.json" `
  --start-date-min '<POLY_HISTORY_START>' `
  --start-date-max '<ASHARE_END_DATE>' `
  --end-date-min '<POLY_HISTORY_START>'
```

根据外部 A 股交易日目录和私有 Poly 缓存生成 09:25 日程：

```powershell
& $py "$root\scripts\build_cutoff_schedule.py" `
  --ashare-root $env:POLY_AGENT_ASHARE_ROOT `
  --raw-dir "$root\cache\private_windows" `
  --out "$root\cache\private_schedule.json"
```

大 universe 推荐按交易日流式生成截面。每个 cutoff 只请求有限的窗口，保存原始
响应哈希，封存 snapshot 后再进入下一个交易日；已有文件会自动跳过以支持断点续跑：

```powershell
& $py "$root\scripts\build_streaming_poly_snapshots.py" `
  --manifest "$root\cache\private_universe\universe.json" `
  --schedule "$root\cache\private_schedule.json" `
  --snapshot-dir "$root\snapshots\private_batch" `
  --raw-dir "$root\cache\private_windows" `
  --bucket-seconds 43200
```

`cache_poly_history.py` 仍可用于小规模调试或需要完整 12 小时序列的离线缓存，
但不应把所有 token 的完整历史同时加载到正式截面生成进程。

然后使用缓存 manifest 生成截面：

```powershell
& $py "$root\scripts\build_poly_snapshot.py" `
  --manifest "$root\cache\raw\manifest_with_cache.json" `
  --raw-dir "$root\cache\raw" `
  --cutoff '<UTC_CUTOFF>' `
  --out "$root\snapshots\<CUTOFF_ID>.json"
```

正式历史回放应先批量生成并封存所有截面，再启动模型。`schedule.json` 只包含预先冻结的 cutoff（A 股交易日 09:25 北京时间应写成对应的 UTC 01:25）及可选 `trade_date`：

```json
{
  "schema_version": "poly_cutoff_schedule.v1",
  "timezone": "Asia/Shanghai",
  "cutoffs": [
    {"cutoff_id": "20250404T0125Z", "trade_date": "2025-04-04", "cutoff_utc": "2025-04-04T01:25:00Z"}
  ]
}
```

批量生成器只读取本地 raw cache，每个 JSON 都经过 `research_input` 校验并拒绝覆盖已有文件：

```powershell
& $py "$root\scripts\batch_build_poly_snapshots.py" `
  --manifest "$root\cache\raw\manifest_with_cache.json" `
  --raw-dir "$root\cache\raw" `
  --schedule "$root\schedule.json" `
  --out-dir "$root\batch_snapshots\sealed"
```

批次会保存每个截面的覆盖、审计和 SHA256。当前 12 小时历史桶在 09:25 截面可能比 cutoff 早约 85 分钟；正式全市场回放应先固定合适的 API 粒度（长期历史通常至少使用 3 小时桶），并在批次清单中记录这一时间差，不能把旧值当作精确的 09:25 观测。

行业标签由独立评估端生成：

```powershell
& $py "$root\scripts\industry_eval.py" `
  --price-root $env:POLY_AGENT_ASHARE_PRICE_ROOT `
  --sw-history $env:POLY_AGENT_SW_HISTORY `
  --output-dir "$root\outputs\industry_eval" `
  --start-date 2021-08-02 --end-date 2026-05-31 `
  --horizons 20,60,120 --min-members 5 --min-coverage 0.8
```

`industry_eval.py` 只写新目录，不覆盖行情源文件。严格 `update_time` 版本和 `start_only` 敏感性版本分别输出。

冻结信号后由独立评估器计算 TopK 截面收益：

```powershell
& $py "$root\scripts\run_snapshot_backtest.py" `
  --signals-dir "$root\outputs\signals" `
  --labels-root "$root\outputs\industry_eval" `
  --mapping-mode strict_update `
  --horizons 20,60,120 `
  --out "$root\outputs\evaluation\snapshot_results.json"
```

## 模型输出协议

模型输出必须通过 `poly_pit_replay.schemas.validate_world_output`，至少包含 `world_output.v1`、`run_id`、输入哈希、截止时间、`poly_only=true` 和行业排序。排序封存后，`compile-signal --top-k 1|3|5` 生成对应行业篮子信号；模型永远不在行业篮子内部挑股票。

当前模型运行器只提供门禁和 `NOT_STARTED` 状态，避免在没有正式启动授权时调用模型。开启真实模型前必须单独登记模型适配器、提示词版本、调用预算、网络隔离和运行日志。
