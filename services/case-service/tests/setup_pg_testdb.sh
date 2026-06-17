#!/usr/bin/env bash
# Set up the opt-in Postgres test database for the case-service suite.
# The conftest builds the schema itself (DROP SCHEMA public CASCADE + create_all +
# helix_case_seq), so this only needs to create an EMPTY helix_test database.
#
# Usage:
#   bash tests/setup_pg_testdb.sh
#   export VELARIS_TEST_DATABASE_URL="postgresql+asyncpg://helix:<PW>@localhost:5432/helix_test"
#   pytest                       # now runs against Postgres (REQUIRES_PG tests included)
#
# Get <PW> from the container:  docker exec docker-compose-helix-db-1 env | grep POSTGRES_PASSWORD
# SERIAL ONLY: the schema is built once per run via a module-global flag — do NOT
# use pytest-xdist (-n); parallel workers would concurrently DROP SCHEMA the same DB.
set -euo pipefail
C="${DB_CONTAINER:-docker-compose-helix-db-1}"
docker exec "$C" psql -U helix -d postgres -q \
  -c "DROP DATABASE IF EXISTS helix_test;" \
  -c "CREATE DATABASE helix_test OWNER helix;"
echo "helix_test created. Set VELARIS_TEST_DATABASE_URL and run pytest."
