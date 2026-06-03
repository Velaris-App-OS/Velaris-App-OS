from __future__ import annotations
import time
from collections import deque
from threading import Lock
from typing import Any, Deque

_MAX = 200
_spans: Deque[dict[str, Any]] = deque(maxlen=_MAX)
_lock = Lock()


def record_span(**kw: Any) -> None:
    kw.setdefault("timestamp", time.time())
    with _lock:
        _spans.append(kw)


def recent_spans(limit: int = 50) -> list[dict]:
    with _lock:
        out = list(_spans)
    return list(reversed(out))[:limit]


def clear_spans() -> None:
    with _lock:
        _spans.clear()
