#!/usr/bin/env bash
# Set up the opt-in MySQL test database for the case-service suite (DB SDK Phase 1b).
# The conftest builds the schema itself under MYSQL_MODE (FK-checks-off drop_all +
# create_all from the ORM metadata), so this only needs to create an EMPTY helix_test
# database — no migration apply.
#
# Usage:
#   bash tests/setup_mysql_testdb.sh
#   uv pip install aiomysql pymysql      # one-time: the MySQL drivers are an optional extra
#   export VELARIS_TEST_DATABASE_URL="mysql+aiomysql://root:roottest@127.0.0.1:3307/helix_test"
#   pytest                               # now runs against MySQL (REQUIRES_EXTERNAL_DB tests included)
#
# Container/password are overridable:  DB_CONTAINER=... MYSQL_PWD=... bash tests/setup_mysql_testdb.sh
# Dedicated helix_test ONLY (NEVER a dev DB — the suite TRUNCATEs every table).
# SERIAL ONLY: the schema is built once per run via a module-global flag — do NOT use
# pytest-xdist (-n); parallel workers would concurrently drop/recreate the same DB.
set -euo pipefail
C="${DB_CONTAINER:-velaris-mysql-test}"
PW="${MYSQL_PWD:-roottest}"
docker exec -e MYSQL_PWD="$PW" "$C" mysql -uroot \
  -e "DROP DATABASE IF EXISTS helix_test;
      CREATE DATABASE helix_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
echo "helix_test created on '$C'. Set VELARIS_TEST_DATABASE_URL and run pytest."
