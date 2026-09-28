# Implementation status

The repository contains a read-only replay framework for Polymarket point-in-
time snapshots and A-share industry evaluation. It includes cache adapters,
leakage-safe snapshot construction, artifact schemas and seals, industry label
evaluation, signal compilation and snapshot evaluation scaffolding.

The public tree deliberately contains no host-specific deployment record, raw
API response, A-share dataset, model output or credential. A private deployment
may record its own environment in an ignored local report.

Current model identifier: `gpt6-Astra-ultra`.
Current execution mode: `disabled`.
Current service state: not started.

The official Polymarket API and the A-share data source must be audited by the
operator before a formal replay. Missing historical market directories, rule
versions or point-in-time company data must be reported as coverage limits;
they must not be silently filled with current values.
