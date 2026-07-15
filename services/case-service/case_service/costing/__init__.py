"""Case Costing — automatic time rollup + per-tenant rate cards (HxReplay P4).

Automatic time is FREE: ``case_event_log.duration_seconds`` is already recorded
per activity, so time-per-case/-stage needs zero new capture. cost = manual
recorded time × the tenant's rate card. Rates are commercially sensitive:
HxGuard-gated (``costing.rates``), per-tenant, never exposed to portal
identities. Design: docs/Future/Done/hxreplay-counterfactual-replay.md §11
(manual timers / timesheets / billing export are later slices).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
