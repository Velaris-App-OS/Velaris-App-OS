"""HxGuard — central authorization decision point (Architecture Report §3.2/§8.3, roadmap #16).

Phase A: in-process PDP wrapping the existing RBAC inputs behind ONE
fail-closed entry point. The backend is swappable (OpenFGA/SpiceDB is a
Phase-C backend swap, not a rewrite).
"""
from .service import (
    Decision,
    Subject,
    check,
    counters,
    guard,
    invalidate_cache,
    require,
    require_case,
    subject_from_user,
)
from . import tuples

__all__ = [
    "Decision", "Subject", "check", "counters", "guard",
    "invalidate_cache", "require", "require_case", "subject_from_user",
    "tuples",
]
