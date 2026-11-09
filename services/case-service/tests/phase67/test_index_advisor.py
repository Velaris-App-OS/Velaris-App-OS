"""HxDBManager's AI index advisor (2026-10-23): what it sends and what it keeps.

It sent the 20 heaviest statements whole — 10 KB, 180 s on the local model, past
the gateway's 120 s — and passed back indexes on primary keys and insert-only
tables. Now: only statements an index could help, slow enough to matter, short;
nothing slow → no AI call; answers checked against the existing indexes.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from case_service.api.routers import hxdbmanager as h


def _q(sql, mean=50.0, total=5000, calls=100):
    return {"query": sql, "mean_exec_time": mean, "total_exec_time": total, "calls": calls}


def test_only_statements_an_index_can_help_and_slow_enough():
    qs = [
        _q("SELECT a, b, c FROM cases WHERE status = $1 ORDER BY created_at"),
        _q("INSERT INTO health_check_results (id) VALUES ($1)"),
        _q("BEGIN"),
        _q("SELECT relname FROM pg_catalog.pg_class WHERE oid = $1"),
        _q("SELECT x FROM platform_generations"),                       # no WHERE: a full read anyway
        _q("SELECT y FROM email_messages WHERE case_id = $1", mean=0.08),  # sub-millisecond
        _q("UPDATE job_leases SET holder = $1 WHERE job = $2", total=9000),
    ]
    got = h._advisor_candidates(qs, 5.0)
    assert [r["q"] for r in got] == ["UPDATE job_leases SET holder = $1 WHERE job = $2",
                                     "SELECT … FROM cases WHERE status = $1 ORDER BY created_at"]
    assert len(h._advisor_candidates([_q("SELECT " + "c, " * 500 + "d FROM t WHERE x = 1")] * 30, 5.0)) \
        == h._ADVISOR_MAX_QUERIES


def test_answers_are_checked_against_the_tables_and_existing_indexes():
    existing = {"cases": ["CREATE UNIQUE INDEX cases_pkey ON public.cases USING btree (id)",
                          "CREATE INDEX ix_cases_status ON public.cases USING btree (status, created_at)"]}
    recs = [
        {"table": "cases", "columns": ["id"], "ddl": "CREATE INDEX x ON cases (id)"},             # the PK
        {"table": "cases", "columns": ["status"], "ddl": "CREATE INDEX y ON cases (status)"},     # covered
        {"table": "cases", "columns": ["tenant_id"], "ddl": "CREATE INDEX z ON cases (tenant_id)", "reason": "r"},
        {"table": "other", "ddl": "CREATE INDEX w ON other (a)"},                                 # not queried
        {"table": "cases", "ddl": "DROP TABLE cases"},                                            # not an index
        "junk",
    ]
    kept, dropped = h._advisor_keep(recs, {"cases"}, existing)
    assert [k["ddl"] for k in kept] == ["CREATE INDEX z ON cases (tenant_id)"]
    assert kept[0]["column"] == "tenant_id" and dropped == 5
    assert h._advisor_keep(None, {"cases"}, existing) == ([], 0)


def test_a_column_only_written_is_never_indexed_and_one_run_is_noise():
    """Seen live: `UPDATE sso_providers SET config=… WHERE id=…`, run once,
    came back as a GIN index on `config` — it would only slow that UPDATE."""
    upd = "UPDATE sso_providers SET config=$1::JSONB WHERE sso_providers.id = $2::UUID"
    assert h._advisor_candidates([_q(upd, mean=9.1, calls=1)], 5.0) == []          # one run
    qs = h._advisor_candidates([_q(upd, mean=9.1, calls=50)], 5.0)
    rec = [{"table": "sso_providers", "columns": ["config"], "index_type": "GIN",
            "ddl": "CREATE INDEX ix_cfg ON sso_providers USING gin (config)"},
           {"table": "sso_providers", "columns": ["id", "enabled"],
            "ddl": "CREATE INDEX ix_id_en ON sso_providers (id, enabled)"}]
    kept, dropped = h._advisor_keep(rec, {"sso_providers"}, {}, qs)
    assert [k["ddl"] for k in kept] == ["CREATE INDEX ix_id_en ON sso_providers (id, enabled)"]
    assert dropped == 1
