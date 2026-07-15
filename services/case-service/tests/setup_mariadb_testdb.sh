#!/usr/bin/env bash
# Set up the opt-in MariaDB test database for the case-service suite (DB SDK — MariaDB).
# The conftest builds the schema itself under MYSQL_MODE (FK-checks-off drop_all +
# create_all from the ORM metadata; MariaDB uses the mysql scheme), so this only needs to
# create an EMPTY helix_test database — no migration apply. The introspector dispatch
# routes to MariadbIntrospector at runtime via the live bind's _is_mariadb flag.
#
# Usage:
#   docker run -d --name velaris-mariadb-test -e MARIADB_ROOT_PASSWORD=roottest \
#     -p 127.0.0.1:3308:3306 mariadb:11        # one-time: a throwaway MariaDB server
#   bash tests/setup_mariadb_testdb.sh
#   uv pip install aiomysql pymysql            # one-time: MySQL-family drivers (optional extra)
#   export VELARIS_TEST_DATABASE_URL="mysql+aiomysql://root:roottest@127.0.0.1:3308/helix_test"
#   pytest                                     # now runs against MariaDB
#
# Container/password overridable:  DB_CONTAINER=... MYSQL_PWD=... bash tests/setup_mariadb_testdb.sh
# Dedicated helix_test ONLY (NEVER a dev DB — the suite TRUNCATEs every table).
# SERIAL ONLY: schema is built once per run via a module-global flag — do NOT use
# pytest-xdist (-n); parallel workers would concurrently drop/recreate the same DB.
set -euo pipefail
C="${DB_CONTAINER:-velaris-mariadb-test}"
PW="${MYSQL_PWD:-roottest}"
# The mariadb:11 image ships the `mariadb` client (the `mysql` command was dropped).
docker exec -e MYSQL_PWD="$PW" "$C" mariadb -uroot \
  -e "DROP DATABASE IF EXISTS helix_test;
      CREATE DATABASE helix_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
echo "helix_test created on '$C'. Set VELARIS_TEST_DATABASE_URL and run pytest."
