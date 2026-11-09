"""R6 failure test 2026-11-03: PostgreSQL killed mid-create answered 500.

A route swallowed the lost connection (`except Exception`), the next query
raised PendingRollbackError. Lost connection => 503 "database unavailable";
any other pending rollback stays a 500.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import PendingRollbackError

_LOST = ("This Session's transaction has been rolled back due to a previous exception during "
         "flush. Original exception was: Can't reconnect until invalid transaction is rolled back.")
_OTHER = "This Session's transaction has been rolled back due to a previous exception during flush."


def _app(msg):
    from velaris_shared.app import apply_platform_error_handlers
    app = FastAPI()
    apply_platform_error_handlers(app, "t")

    @app.get("/x")
    async def x():
        raise PendingRollbackError(msg)
    return TestClient(app, raise_server_exceptions=False)


def test_shared_lost_connection_is_503():
    r = _app(_LOST).get("/x")
    assert r.status_code == 503 and "unavailable" in r.text


def test_shared_other_pending_rollback_stays_500():
    assert _app(_OTHER).get("/x").status_code == 500


def test_case_service_registers_the_handler():
    from case_service.main import app
    h = app.exception_handlers.get(PendingRollbackError)
    assert h is not None and h.__name__ == "_pending_rollback_handler"


# Pooler killed mid-query (R6 failure test 2026-11-03): a plain DBAPIError with
# connection_invalidated answered 500.
from sqlalchemy.exc import DBAPIError  # noqa: E402


def _dbapi_app(invalidated):
    from velaris_shared.app import apply_platform_error_handlers
    app = FastAPI()
    apply_platform_error_handlers(app, "t")

    @app.get("/x")
    async def x():
        raise DBAPIError("SELECT 1", {}, Exception("connection was closed in the middle of operation"),
                         connection_invalidated=invalidated)
    return TestClient(app, raise_server_exceptions=False)


def test_shared_dropped_connection_is_503():
    r = _dbapi_app(True).get("/x")
    assert r.status_code == 503 and "unavailable" in r.text


def test_shared_other_dbapi_error_stays_500():
    assert _dbapi_app(False).get("/x").status_code == 500


def test_case_service_registers_the_dbapi_handler():
    from case_service.main import app
    h = app.exception_handlers.get(DBAPIError)
    assert h is not None and h.__name__ == "_dbapi_handler"
