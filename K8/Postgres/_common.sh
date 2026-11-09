#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes — shared helpers
#  Sourced by setup-velaris-k8s.sh, update-velaris-k8s.sh and
#  stop-velaris-k8s.sh. Not meant to be run directly.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail

K8_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find the repository root by walking up until the things we need are there,
# rather than counting directory levels. These scripts can then be moved or
# nested further without an edit.
VELARIS_DIR="$K8_DIR"
while [ "$VELARIS_DIR" != "/" ]; do
  if [ -d "$VELARIS_DIR/migrations/postgresql" ] && [ -d "$VELARIS_DIR/deploy/helm" ]; then
    break
  fi
  VELARIS_DIR="$(dirname "$VELARIS_DIR")"
done
[ "$VELARIS_DIR" = "/" ] && {
  echo "Cannot locate the Velaris repository root above $K8_DIR" >&2
  echo "(looked for a directory holding both migrations/postgresql and deploy/helm)" >&2
  exit 1
}
CHART_DIR="${VELARIS_CHART_DIR:-$VELARIS_DIR/deploy/helm/velaris}"
MIGRATIONS_DIR="$VELARIS_DIR/migrations/postgresql"

NAMESPACE="${VELARIS_K8S_NAMESPACE:-velaris}"
RELEASE="${VELARIS_K8S_RELEASE:-velaris}"

# ── External database ──────────────────────────────────────────────
# Set to reuse a database that already exists — typically an earlier Velaris
# install being moved into Kubernetes. When set, the bundled subchart is not
# installed and every query below runs from a short-lived client pod instead
# of exec'ing into a database pod that does not exist.
EXTERNAL_URL="${VELARIS_K8S_EXTERNAL_URL:-}"
DB_CLIENT_IMAGE="${VELARIS_DB_CLIENT_IMAGE:-postgres:18-alpine}"

db_is_external() { [ -n "$EXTERNAL_URL" ]; }

# Parse the pieces we need out of a SQLAlchemy DSN, without echoing the
# password anywhere. Accepts postgresql+asyncpg://user:pass@host:port/db
_dsn_part() {  # _dsn_part <user|pass|host|port|name>
  python3 - "$EXTERNAL_URL" "$1" <<'PYEOF'
import sys
from urllib.parse import urlsplit, unquote
u = urlsplit(sys.argv[1].split("+", 1)[0] + "://" + sys.argv[1].split("://", 1)[1])
part = sys.argv[2]
print({"user": unquote(u.username or ""), "pass": unquote(u.password or ""),
       "host": u.hostname or "", "port": str(u.port or 5432),
       "name": (u.path or "/").lstrip("/")}[part])
PYEOF
}

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; NC=$'\033[0m'

step() { echo; echo "${BLUE}${BOLD}==> $*${NC}"; }
ok()   { echo "  ${GREEN}✓${NC} $*"; }
warn() { echo "  ${YELLOW}!${NC} $*"; }
err()  { echo "  ${RED}✗${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

need_tool() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."
}

require_cluster() {
  need_tool kubectl
  need_tool helm
  kubectl cluster-info >/dev/null 2>&1 \
    || die "No reachable Kubernetes cluster (check KUBECONFIG / current context)."
  ok "cluster: $(kubectl config current-context)"
}

release_exists() {
  helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1
}

# The services that hold a database connection pool. Used for connection-count
# arithmetic and for stop/start ordering.
WORKLOADS=(case-service engine worker analytics-service hxgraph-service hxmigrate-service commerce-service portal-service user-service jobs)

deploy_name() { echo "${RELEASE}-helix-$1"; }

# Postgres access. With the bundled subchart we exec into its pod; with an
# external database the operator supplies VELARIS_K8S_DB_* and we run a
# short-lived client pod instead.
db_bundled() {
  db_is_external && return 1
  kubectl -n "$NAMESPACE" get pod "${RELEASE}-postgresql-0" >/dev/null 2>&1
}

#: Name of the helper pod used for an external database. Kept alive for the
#: duration of a run so migrations are copied once, not once per file.
CLIENT_POD="velaris-dbclient"

ensure_client_pod() {
  kubectl -n "$NAMESPACE" get pod "$CLIENT_POD" >/dev/null 2>&1 && return 0
  kubectl -n "$NAMESPACE" run "$CLIENT_POD" --image="$DB_CLIENT_IMAGE" --restart=Never \
    --command -- sleep 7200 >/dev/null 2>&1 || return 1
  local i
  for i in $(seq 1 60); do
    [ "$(kubectl -n "$NAMESPACE" get pod "$CLIENT_POD" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] && return 0
    sleep 2
  done
  return 1
}

remove_client_pod() {
  kubectl -n "$NAMESPACE" delete pod "$CLIENT_POD" --force --grace-period=0 >/dev/null 2>&1
}

# The helper pod exists only for the duration of a run. Nothing called this
# before, so every external-database run left a `sleep 7200` pod behind in the
# operator's namespace. The trap fires on a clean exit and on die() alike.
cleanup_client_pod() { db_is_external && remove_client_pod; return 0; }
trap cleanup_client_pod EXIT

psql_q() {  # psql_q <sql>  -> tuples only, no headers
  if db_is_external; then
    ensure_client_pod || { err "could not start the database client pod"; return 1; }
    kubectl -n "$NAMESPACE" exec "$CLIENT_POD" -- env PGPASSWORD="$(_dsn_part pass)" \
      psql -h "$(_dsn_part host)" -p "$(_dsn_part port)" -U "$(_dsn_part user)" \
      -d "$(_dsn_part name)" -tAc "$1"
  elif db_bundled; then
    kubectl -n "$NAMESPACE" exec "${RELEASE}-postgresql-0" -- env \
      PGPASSWORD="$(db_password)" psql -U "$(db_user)" -d "$(db_name)" -tAc "$1"
  else
    kubectl -n "$NAMESPACE" run velaris-psql-$$ --rm -i --restart=Never --quiet \
      --image="${VELARIS_PSQL_IMAGE:-postgres:18-alpine}" --command -- \
      env PGPASSWORD="$(db_password)" psql -h "$(db_host)" -U "$(db_user)" -d "$(db_name)" -tAc "$1"
  fi
}

db_user()     { db_is_external && { _dsn_part user; return; }; echo "${VELARIS_K8S_DB_USER:-helix}"; }
db_name()     { db_is_external && { _dsn_part name; return; }; echo "${VELARIS_K8S_DB_NAME:-helix}"; }
db_host()     { echo "${VELARIS_K8S_DB_HOST:-${RELEASE}-postgresql}"; }
db_password() {
  if db_is_external; then _dsn_part pass; return; fi
  if [ -n "${VELARIS_K8S_DB_PASSWORD:-}" ]; then
    echo "$VELARIS_K8S_DB_PASSWORD"; return
  fi
  # Bitnami stores it in a Secret the chart created; never echo it anywhere else.
  kubectl -n "$NAMESPACE" get secret "${RELEASE}-postgresql" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null
}

wait_ready() {
  # Track the RELEASE's own deployments, not "every pod in the namespace":
  # a leftover pod from an earlier failed revision, or an unrelated pod
  # sharing the namespace, would otherwise never go ready and make a healthy
  # upgrade look failed — which then rolls a good release back.
  local timeout="${1:-600}" rc=0
  for svc in "${WORKLOADS[@]}"; do
    local d; d="$(deploy_name "$svc")"
    kubectl -n "$NAMESPACE" get deploy "$d" >/dev/null 2>&1 || continue
    if ! kubectl -n "$NAMESPACE" rollout status deploy "$d" --timeout="${timeout}s" >/dev/null 2>&1; then
      warn "$svc did not become ready"
      kubectl -n "$NAMESPACE" get pods -l "app=$svc" --no-headers 2>/dev/null \
        | awk '$2 !~ /^([0-9]+)\/\1$/ {print "      " $1 "  " $2 "  " $3}'
      rc=1
    fi
  done
  [ "$rc" = "0" ] && ok "all release deployments ready"
  return $rc
}


# Refuse a database that is populated but is not a Velaris one. Pointing an
# install at the wrong DSN and running 162 migrations over somebody else's
# schema is the single most destructive mistake available here, and it is a
# plausible typo. A Velaris database has velaris_users; an empty one is fine.
assert_velaris_database() {
  local tables velaris_marker
  tables=$(psql_q "SELECT count(*) FROM information_schema.tables
                   WHERE table_schema = current_schema()" | tr -d '[:space:]')
  [ "${tables:-0}" = "0" ] && { ok "target database is empty — a fresh schema will be created"; return 0; }
  velaris_marker=$(psql_q "SELECT count(*) FROM information_schema.tables
                           WHERE table_schema = current_schema()
                             AND table_name IN ('velaris_users','schema_migrations','case_instances')" | tr -d '[:space:]')
  if [ "${velaris_marker:-0}" = "0" ]; then
    err "The target database holds $tables table(s) but none of them are Velaris tables."
    err "Refusing to migrate: this looks like another application's database."
    err "Check the connection URL, or point at an empty database."
    return 1
  fi
  ok "existing Velaris database recognised ($tables tables)"
}

# ── Container images ───────────────────────────────────────────────
# VELARIS_IMAGE_REGISTRY / VELARIS_IMAGE_TAG are documented as the way to point
# an install at your own registry or a pinned tag. Building and loading under
# that name is only half the job: without these --set values helm still deploys
# the chart's own helix/*:latest and every pod sits in ImagePullBackOff. The
# defaults below are exactly the chart's, so an operator who sets neither
# variable gets the same rendering as before.
IMAGE_SETS=()
build_image_sets() {  # build_image_sets <registry> <tag>
  local reg="$1" tag="$2"
  IMAGE_SETS=(
    --set image.caseService.repository="$reg/case-service"
    --set image.caseService.tag="$tag"
    --set image.worker.repository="$reg/case-service"
    --set image.worker.tag="$tag"
    --set image.engine.repository="$reg/engine"
    --set image.engine.tag="$tag"
    --set image.analyticsService.repository="$reg/analytics-service"
    --set image.analyticsService.tag="$tag"
    --set image.hxgraphService.repository="$reg/hxgraph-service"
    --set image.hxgraphService.tag="$tag"
    --set image.hxmigrateService.repository="$reg/hxmigrate-service"
    --set image.hxmigrateService.tag="$tag"
    --set image.commerceService.repository="$reg/commerce-service"
    --set image.commerceService.tag="$tag"
    --set image.portalService.repository="$reg/portal-service"
    --set image.portalService.tag="$tag"
    --set image.userService.repository="$reg/user-service"
    --set image.userService.tag="$tag"
  )
}

# ── Migrations ─────────────────────────────────────────────────────
#
# Applied files are recorded in schema_migrations, exactly as the host
# installer does, so an update only ever applies what is new.
#
# Unlike the host installer this runner uses ON_ERROR_STOP=1. A migration
# that fails half way must STOP the run and be reported — recording it as
# applied while its statements were rolled back leaves a schema that no
# later migration can repair, and nothing says so.
run_migrations() {
  local dir="$MIGRATIONS_DIR" applied=0 skipped=0 failed=""
  [ -d "$dir" ] || die "migrations directory not found: $dir"

  assert_velaris_database || return 1

  psql_q "CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
          );" >/dev/null || die "cannot reach the database"

  # A database that already carries the schema but has an EMPTY ledger is
  # almost always an existing install being adopted (migrated by the host
  # installer, restored from a dump, or migrated by hand). These migrations
  # are NOT re-runnable: replaying 001 onward against a live schema fails
  # part way and leaves it half-changed. Refuse, and say exactly how to adopt.
  local ledger_rows table_count
  ledger_rows=$(psql_q "SELECT count(*) FROM schema_migrations" | tr -d '[:space:]')
  table_count=$(psql_q "SELECT count(*) FROM information_schema.tables
                        WHERE table_schema = current_schema()" | tr -d '[:space:]')
  local total_files half
  total_files=$(ls "$dir"/*.sql 2>/dev/null | wc -l)
  half=$((total_files / 2))
  # An empty ledger is the obvious case; a PARTIALLY recorded one is the
  # dangerous one, because it looks like an ordinary upgrade and is not.
  # If the schema is populated while the ledger accounts for less than half
  # the migrations, this database was not built by this runner.
  if [ "${table_count:-0}" -gt 20 ] && [ "${ledger_rows:-0}" -lt "$half" ]; then
    if [ "${VELARIS_K8S_ADOPT:-0}" = "1" ]; then
      warn "adopting an existing schema: recording all migrations as applied"
      for f in $(ls "$dir"/*.sql | sort -V); do
        psql_q "INSERT INTO schema_migrations(filename) VALUES('$(basename "$f")')
                ON CONFLICT DO NOTHING;" >/dev/null
      done
      ok "ledger backfilled with $(ls "$dir"/*.sql | wc -l) file(s)"
    else
      err "This database has $table_count tables but only ${ledger_rows:-0} of $total_files migrations recorded."
      err "Replaying every migration against it would fail part way through."
      err "If this is an existing install, adopt it instead (records the"
      err "migrations as applied without running them):"
      err "    VELARIS_K8S_ADOPT=1 $0"
      return 1
    fi
  fi

  local target_pod
  if db_is_external; then
    ensure_client_pod || die "could not start the database client pod"
    target_pod="$CLIENT_POD"
  else
    db_bundled || die "no database reachable: neither a bundled pod nor VELARIS_K8S_EXTERNAL_URL"
    target_pod="${RELEASE}-postgresql-0"
  fi

  kubectl -n "$NAMESPACE" exec "$target_pod" -- rm -rf /tmp/velaris-mig >/dev/null 2>&1
  kubectl -n "$NAMESPACE" cp "$dir" "$target_pod:/tmp/velaris-mig" >/dev/null 2>&1 \
    || die "could not copy migrations into the database pod"

  for f in $(ls "$dir"/*.sql | sort -V); do
    local name; name="$(basename "$f")"
    if [ "$(psql_q "SELECT 1 FROM schema_migrations WHERE filename='$name'")" = "1" ]; then
      skipped=$((skipped + 1)); continue
    fi
    echo -n "    apply $name ... "
    if kubectl -n "$NAMESPACE" exec "$target_pod" -- env PGPASSWORD="$(db_password)" \
         psql $(db_is_external && echo "-h $(_dsn_part host) -p $(_dsn_part port)") \
         -U "$(db_user)" -d "$(db_name)" -v ON_ERROR_STOP=1 -q \
         -f "/tmp/velaris-mig/$name" >/tmp/velaris-mig-err.log 2>&1; then
      psql_q "INSERT INTO schema_migrations(filename) VALUES('$name')
              ON CONFLICT DO NOTHING;" >/dev/null
      echo "${GREEN}✓${NC}"; applied=$((applied + 1))
    else
      echo "${RED}✗${NC}"; failed="$name"
      echo "${RED}$(tail -5 /tmp/velaris-mig-err.log)${NC}" >&2
      break
    fi
  done

  kubectl -n "$NAMESPACE" exec "$target_pod" -- rm -rf /tmp/velaris-mig >/dev/null 2>&1

  if [ -n "$failed" ]; then
    err "migration $failed FAILED — it was NOT recorded as applied."
    err "The database is part-migrated. Fix the migration, then re-run; every"
    err "file before it is already recorded and will be skipped."
    return 1
  fi
  ok "migrations: $applied applied, $skipped already present"
}

superadmin_count() {
  psql_q "SELECT count(*) FROM velaris_users WHERE is_superadmin AND is_active" 2>/dev/null | tr -d '[:space:]'
}
