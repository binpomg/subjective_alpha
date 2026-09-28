# Runtime environment template

This file is a public template. It intentionally omits hostnames, usernames,
absolute paths, datasets and credentials. Record deployment-specific details in
an untracked local copy (for example `docs/environment.local.md`).

- Host: `<HOST_ALIAS>`
- User: `<USER_NAME>`
- Project root: `<PROJECT_ROOT>`
- Python: `<PROJECT_ROOT>/.venv/bin/python` (Python 3.10+)
- DuckDB: `<DUCKDB_VERSION>`
- Model execution: `disabled` until an explicitly reviewed adapter is registered
- Long-running service: not enabled by default
- Trading account/order API: not configured
- A-share data: supplied separately through a local mount or environment variable
- Polymarket history: supplied separately through a private cache; no raw response is committed
- Credentials: injected by the runtime secret store only; never put them in this file
