# Acceptance checklist

This public checklist records requirements without identifying a deployment
host, user, data location or secret.

| Check | Required state | Evidence to record privately |
|---|---|---|
| Package import | PASS | package/version output |
| Python environment | PASS | interpreter and dependency versions |
| Offline tests | PASS | test command and timestamp |
| Read-only Polymarket probe | OPTIONAL | request manifest with redacted credentials |
| Historical cache replay | PASS before formal use | private cache manifest and hashes |
| Model invocation | intentionally disabled by default | reviewed adapter and sealed run log |
| Long-running service | intentionally not started | operator approval |
| Trading | intentionally not configured | no account/order credentials in repository |

Results must be updated with actual command timestamps; planned work is not
treated as completed. Do not commit private evidence files to the public tree.
