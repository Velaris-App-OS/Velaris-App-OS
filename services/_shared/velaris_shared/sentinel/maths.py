"""The risk model's two scoring formulas — the logistic link and AUC.

Moved out of `case_service.sentinel.model` (2026-10-19, step 7 — the commerce
split): the model market (commerce-service) scores a publisher's model against
a tenant's history with them. Unchanged; `sentinel.model` imports them back.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import numpy as np


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60, 60)))


def _auc(y: np.ndarray, scores: np.ndarray) -> float | None:
    """Mann-Whitney AUC. None when a class is absent (undefined)."""
    npos = int((y == 1).sum())
    nneg = int((y == 0).sum())
    if npos == 0 or nneg == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    auc = (ranks[y == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg)
    return round(float(auc), 3)
