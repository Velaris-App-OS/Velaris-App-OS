# pooler-socket

PgBouncer's UNIX socket appears here (`.s.PGSQL.6432`) when
`VELARIS_CASE_DATABASE_POOLER_SOCKET=true`: the platform then reaches the pooler
through it instead of 127.0.0.1:6432 (R5d, Nov_TODO). The pooler container makes
this folder 0700 and runs as its owner, so no other local user can reach — or
impersonate — the pooler. The folder ships with the release so the container's
bind mount always has a source. Leave it in place.
