"""HxReplay — counterfactual case replay ("what-if on real history").

Deterministic re-execution of RECORDED cases against a candidate configuration
(an HxBranch snapshot), from the first point the change bites. Baseline = the
recorded reality in ``case_event_log``; only the tail after the divergence is
recomputed, and only deterministic nodes — human/external outcomes are held
fixed from the record, and anything unrecoverable is marked indeterminate and
excluded, never guessed.

Read-only by construction: replay consumes the event log / lineage / variables
and writes ONLY to ``replay_runs`` / ``replay_results``.

Design: docs/Future/Done/hxreplay-counterfactual-replay.md

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
