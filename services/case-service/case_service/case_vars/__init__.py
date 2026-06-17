"""Case Variables — typed, namespaced variable system (spec v2)."""
from .service import (
    CallerContext,
    VariableError,
    get,
    get_all,
    get_all_bulk,
    get_namespace,
    set_granted,
    set_variable as set,  # spec API name; aliased so the module keeps builtin set
)

__all__ = [
    "CallerContext", "VariableError",
    "set", "set_granted", "get", "get_all", "get_namespace", "get_all_bulk",
]
