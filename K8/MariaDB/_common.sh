#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes (MariaDB) — shared helpers
#  Sourced by setup-velaris-k8s.sh, update-velaris-k8s.sh and
#  stop-velaris-k8s.sh. Not meant to be run directly.
#
#  Copyright (c) 2024-2026 Velaris Contributors
#  SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail

# ── Engine ─────────────────────────────────────────────────────────
# MySQL and MariaDB share this file; only these three values differ. The
# MySQL copy of these scripts sets them to mysql/mysql/mysql-password.
# MariaDB keeps the mysql+aiomysql SQLAlchemy scheme — the driver is the same,
# only the server and its client binary differ.
DB_ENGINE="${DB_ENGINE:-mariadb}"            # helm values key and service suffix
DB_CLIENT="${DB_CLIENT:-mariadb}"            # client binary inside the database pod
DB_SECRET_KEY="${DB_SECRET_KEY:-mariadb-password}"

K8_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find the repository root by walking up until the things we need are there,
# rather than counting directory levels. These scripts can then be moved or
# nested further without an edit.
VELARIS_DIR="$K8_DIR"
while [ "$VELARIS_DIR" != "/" ]; do
  if [ -d "$VELARIS_DIR/migrations/mysql" ] && [ -d "$VELARIS_DIR/deploy/helm" ]; then
    break
  fi
  VELARIS_DIR="$(dirname "$VELARIS_DIR")"
done
[ "$VELARIS_DIR" = "/" ] && {
  echo "Cannot locate the Velaris repository root above $K8_DIR" >&2
  echo "(looked for a directory holding both migrations/mysql and deploy/helm)" >&2
  exit 1
}

CHART_DIR="${VELARIS_CHART_DIR:-$VELARIS_DIR/deploy/helm/velaris}"

# MySQL and MariaDB share one migration track: a frozen baseline of PostgreSQL
# 001-154, the seeds, then one numbered file per PostgreSQL migration from 155.
MIGRATIONS_DIR="$VELARIS_DIR/migrations/mysql"

NAMESPACE="${VELARIS_K8S_NAMESPACE:-velaris-$DB_ENGINE}"
RELEASE="${VELARIS_K8S_RELEASE:-velaris}"

# ── External database ──────────────────────────────────────────────
# Set to reuse a database that already exists — typically an earlier Velaris
# install being moved into Kubernetes. When set, the bundled subchart is not
# installed and every query below runs from a short-lived client pod instead
# of exec'ing into a database pod that does not exist.
EXTERNAL_URL="${VELARIS_K8S_EXTERNAL_URL:-}"
DB_CLIENT_IMAGE="${VELARIS_DB_CLIENT_IMAGE:-mariadb:11.4}"

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
       "host": u.hostname or "", "port": str(u.port or 3306),
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

need_tool() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."; }

require_cluster() {
  need_tool kubectl; need_tool helm
  kubectl cluster-info >/dev/null 2>&1 \
    || die "No reachable Kubernetes cluster (check KUBECONFIG / current context)."
  ok "cluster: $(kubectl config current-context)"
  ok "database engine: $DB_ENGINE"
}

release_exists() { helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; }

WORKLOADS=(case-service engine worker analytics-service hxgraph-service hxmigrate-service commerce-service portal-service user-service jobs)
deploy_name() { echo "${RELEASE}-helix-$1"; }
db_pod()      { echo "${RELEASE}-${DB_ENGINE}-0"; }

db_user() { db_is_external && { _dsn_part user; return; }; echo "${VELARIS_K8S_DB_USER:-helix}"; }
db_name() { db_is_external && { _dsn_part name; return; }; echo "${VELARIS_K8S_DB_NAME:-helix}"; }
db_password() {
  if db_is_external; then _dsn_part pass; return; fi
  if [ -n "${VELARIS_K8S_DB_PASSWORD:-}" ]; then echo "$VELARIS_K8S_DB_PASSWORD"; return; fi
  kubectl -n "$NAMESPACE" get secret "${RELEASE}-${DB_ENGINE}" \
    -o jsonpath="{.data.${DB_SECRET_KEY}}" 2>/dev/null | base64 -d 2>/dev/null
}

db_bundled() { db_is_external && return 1; kubectl -n "$NAMESPACE" get pod "$(db_pod)" >/dev/null 2>&1; }

#: Helper pod for an external database, kept alive for one run.
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

# Run a client in a pod with the database password, never on a command line:
# `kubectl exec -- env MYSQL_PWD=…` and `sh -c "MYSQL_PWD='…' …"` put it in the
# process list on this machine and inside the pod (and the second broke on a
# password with a quote in it). It goes in as the first line of stdin instead;
# the pod's shell reads it into MYSQL_PWD and runs the client.
_kx() {  # pod command args…
  local pod="$1"; shift
  printf '%s\n' "$(db_password)" | kubectl -n "$NAMESPACE" exec -i "$pod" -- \
    sh -c 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec "$@"' sh "$@"
}

# sql_q <sql> -> one column, no headers
sql_q() {
  if db_is_external; then
    ensure_client_pod || { err "could not start the database client pod"; return 1; }
    _kx "$CLIENT_POD" \
      mariadb -h "$(_dsn_part host)" -P "$(_dsn_part port)" -u "$(db_user)" \
      -N -B -e "$1" "$(db_name)" 2>/dev/null
    return
  fi
  db_bundled || die "no database reachable: neither a bundled pod nor VELARIS_K8S_EXTERNAL_URL"
  _kx "$(db_pod)" \
    "$DB_CLIENT" -u "$(db_user)" -N -B -e "$1" "$(db_name)" 2>/dev/null
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


# Refuse a populated database that is not a Velaris one — pointing an install
# at the wrong DSN and running the baseline over somebody else's schema is the
# most destructive mistake available here, and it is a plausible typo.
assert_velaris_database() {
  local tables marker
  tables=$(sql_q "SELECT count(*) FROM information_schema.tables
                  WHERE table_schema = DATABASE()" | tr -d '[:space:]')
  [ "${tables:-0}" = "0" ] && { ok "target database is empty — a fresh schema will be created"; return 0; }
  marker=$(sql_q "SELECT count(*) FROM information_schema.tables
                  WHERE table_schema = DATABASE()
                    AND table_name IN ('velaris_users','schema_migrations','case_instances')" | tr -d '[:space:]')
  if [ "${marker:-0}" = "0" ]; then
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
# Applied files are recorded in schema_migrations, the same ledger the host
# installer uses, so an update only ever applies what is new. The mysql client
# aborts on the first error by default, which is the behaviour we want: a
# migration that fails must stop the run and be reported, never be recorded as
# applied while its statements did not land.
run_migrations() {
  local dir="$MIGRATIONS_DIR" applied=0 skipped=0 failed=""
  [ -d "$dir" ] || die "migrations directory not found: $dir"
  local target_pod
  if db_is_external; then
    ensure_client_pod || die "could not start the database client pod"
    target_pod="$CLIENT_POD"
  else
    db_bundled || die "no database reachable: neither a bundled pod nor VELARIS_K8S_EXTERNAL_URL"
    target_pod="$(db_pod)"
  fi
  assert_velaris_database || return 1

  _kx "$target_pod" \
    mariadb $(db_is_external && echo "-h $(_dsn_part host) -P $(_dsn_part port)") \
    -u "$(db_user)" -e "CREATE TABLE IF NOT EXISTS schema_migrations (
      filename   VARCHAR(255) PRIMARY KEY,
      applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    );" "$(db_name)" >/dev/null 2>&1 || die "cannot reach the database"

  # A database that already carries the schema but has an EMPTY or barely
  # populated ledger is almost always an existing install being adopted. This
  # track is not re-runnable: replaying the baseline over a live schema fails
  # part way and leaves it half-changed. Refuse, and say how to adopt.
  local ledger_rows table_count total_files half
  ledger_rows=$(sql_q "SELECT count(*) FROM schema_migrations" | tr -d '[:space:]')
  table_count=$(sql_q "SELECT count(*) FROM information_schema.tables
                       WHERE table_schema = DATABASE()" | tr -d '[:space:]')
  total_files=$(ls "$dir"/*.sql 2>/dev/null | wc -l)
  half=$((total_files / 2))
  if [ "${table_count:-0}" -gt 20 ] && [ "${ledger_rows:-0}" -lt "$half" ]; then
    if [ "${VELARIS_K8S_ADOPT:-0}" = "1" ]; then
      warn "adopting an existing schema: recording all migrations as applied"
      for f in $(ls "$dir"/*.sql | sort -V); do
        _kx "$target_pod" \
          $DB_CLIENT $(db_is_external && echo "-h $(_dsn_part host) -P $(_dsn_part port)") -u "$(db_user)" -e \
          "INSERT IGNORE INTO schema_migrations(filename) VALUES('$(basename "$f")');" \
          "$(db_name)" >/dev/null 2>&1
      done
      ok "ledger backfilled with $total_files file(s)"
    else
      err "This database has $table_count tables but only ${ledger_rows:-0} of $total_files migrations recorded."
      err "Replaying the baseline against it would fail part way through."
      err "If this is an existing install, adopt it instead (records the"
      err "migrations as applied without running them):"
      err "    VELARIS_K8S_ADOPT=1 $0"
      return 1
    fi
  fi

  kubectl -n "$NAMESPACE" exec "$target_pod" -- rm -rf /tmp/velaris-mig >/dev/null 2>&1
  kubectl -n "$NAMESPACE" cp "$dir" "$target_pod:/tmp/velaris-mig" >/dev/null 2>&1 \
    || die "could not copy migrations into the database pod"

  for f in $(ls "$dir"/*.sql | sort -V); do
    local name; name="$(basename "$f")"
    if [ "$(sql_q "SELECT 1 FROM schema_migrations WHERE filename='$name'" | tr -d '[:space:]')" = "1" ]; then
      skipped=$((skipped + 1)); continue
    fi
    echo -n "    apply $name ... "
    HOSTFLAGS=""; db_is_external && HOSTFLAGS="-h $(_dsn_part host) -P $(_dsn_part port)"
    if _kx "$target_pod" sh -c \
         "exec $DB_CLIENT $HOSTFLAGS -u '$(db_user)' '$(db_name)' < /tmp/velaris-mig/$name" \
         >/tmp/velaris-mig-err.log 2>&1; then
      _kx "$target_pod" \
        $DB_CLIENT $(db_is_external && echo "-h $(_dsn_part host) -P $(_dsn_part port)") -u "$(db_user)" -e \
        "INSERT IGNORE INTO schema_migrations(filename) VALUES('$name');" "$(db_name)" >/dev/null 2>&1
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
  sql_q "SELECT count(*) FROM velaris_users WHERE is_superadmin = 1 AND is_active = 1" | tr -d '[:space:]'
}
