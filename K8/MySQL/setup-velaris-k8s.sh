#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes (MySQL) — first install
#
#  Installs the Helm chart into a cluster, applies the database
#  migrations and verifies the platform answers. Safe to re-run: an
#  existing release is left alone unless --upgrade is given (use
#  update-velaris-k8s.sh for that).
#
#  Usage:  ./setup-velaris-k8s.sh
#          ./setup-velaris-k8s.sh --build          # build images first
#          ./setup-velaris-k8s.sh --kind <cluster> # load images into kind
#          ./setup-velaris-k8s.sh --dry-run        # render, change nothing
#          ./setup-velaris-k8s.sh -f my-values.yaml
#
#  Reusing an existing Velaris database (an earlier install being moved into
#  Kubernetes, or a restored dump) — the bundled database is then not
#  installed at all:
#
#    ./setup-velaris-k8s.sh --external-url \
#      "mysql+aiomysql://user:pass@host:3306/velaris"
#
#  Prefer VELARIS_K8S_EXTERNAL_URL over the flag so the password does not
#  reach your shell history.
#
#  Environment:
#    VELARIS_K8S_NAMESPACE   default: velaris
#    VELARIS_K8S_RELEASE     default: velaris
#    VELARIS_IMAGE_REGISTRY  prefix for pushed images (default: helix, local)
#    VELARIS_IMAGE_TAG       default: latest
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

BUILD=0; KIND_CLUSTER=""; DRY_RUN=0; EXTRA_VALUES=(); TIMEOUT="${VELARIS_K8S_TIMEOUT:-10m}"
REGISTRY="${VELARIS_IMAGE_REGISTRY:-helix}"
TAG="${VELARIS_IMAGE_TAG:-latest}"

while [ $# -gt 0 ]; do
  case "$1" in
    --build)   BUILD=1; shift ;;
    --kind)    KIND_CLUSTER="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -f|--values) EXTRA_VALUES+=(-f "$2"); shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --external-url) EXTERNAL_URL="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

build_image_sets "$REGISTRY" "$TAG"

echo "${BOLD}VELARIS — Kubernetes install${NC}"
echo "  namespace: $NAMESPACE    release: $RELEASE"

# ── Step 1: preflight ──────────────────────────────────────────────
step "Step 1/7 — Preflight"
require_cluster
[ -d "$CHART_DIR" ] || die "chart not found at $CHART_DIR"
ok "chart: $CHART_DIR"

if release_exists; then
  warn "release '$RELEASE' already exists in namespace '$NAMESPACE'."
  warn "This script installs; to move an existing install forward use:"
  warn "    $K8_DIR/update-velaris-k8s.sh"
  exit 0
fi

# The chart's HPAs are inert without metrics-server. That is not fatal — the
# platform runs at its fixed replica counts — but say so rather than leaving
# an operator to wonder why nothing ever scales.
if ! kubectl get deploy metrics-server -n kube-system >/dev/null 2>&1; then
  warn "metrics-server is not installed: the chart's HPAs cannot scale (replicas stay fixed)."
fi

# ── Step 2: images ─────────────────────────────────────────────────
step "Step 2/7 — Container images"
IMAGES=(case-service analytics-service hxgraph-service hxmigrate-service commerce-service portal-service user-service engine)
# The DB SDK is BYO: without this build arg the images carry asyncpg only and
# cannot open a MySQL connection at all.
BUILD_ARGS=(--build-arg DB_EXTRAS="[mysql]")
if [ "$BUILD" = "1" ]; then
  need_tool docker
  for svc in "${IMAGES[@]}"; do
    echo -n "    build $REGISTRY/$svc:$TAG ... "
    local_df="$VELARIS_DIR/services/$svc/Dockerfile"
    [ "$svc" = "engine" ] && local_df="$VELARIS_DIR/engine/Dockerfile"
    if docker build -q "${BUILD_ARGS[@]}" -f "$local_df" -t "$REGISTRY/$svc:$TAG" "$VELARIS_DIR" >/dev/null 2>&1; then
      echo "${GREEN}✓${NC}"
    else
      echo "${RED}✗${NC}"
      die "image build failed for $svc — run the docker build by hand to see why."
    fi
  done
else
  ok "skipping build (pass --build to build from source)"
fi

if [ -n "$KIND_CLUSTER" ]; then
  need_tool kind
  for svc in "${IMAGES[@]}"; do
    kind load docker-image "$REGISTRY/$svc:$TAG" --name "$KIND_CLUSTER" >/dev/null 2>&1 \
      && ok "loaded $svc into kind/$KIND_CLUSTER" \
      || warn "could not load $svc into kind/$KIND_CLUSTER"
  done
fi

# ── Step 3: chart dependencies ─────────────────────────────────────
step "Step 3/7 — Chart dependencies"
if ! helm dependency build "$CHART_DIR" >/tmp/velaris-helm-dep.log 2>&1; then
  if ! helm dependency update "$CHART_DIR" >>/tmp/velaris-helm-dep.log 2>&1; then
    err "$(tail -3 /tmp/velaris-helm-dep.log)"
    die "chart dependencies could not be resolved — see Chart.yaml pins."
  fi
fi
ok "subcharts resolved"

# ── Step 4: install ────────────────────────────────────────────────
step "Step 4/7 — Installing the chart"

# Resource floors for the bundled database, and the engine selection. The
# subchart's default preset is far too small for a real workload, and MySQL's
# default max_connections of 151 is below what a handful of pods open (each
# service pod holds up to 21). Set here, not in the chart, so an operator who
# supplies their own -f values keeps the last word.
DB_DEFAULTS=(
  --set postgresql.enabled=false
  --set mysql.enabled=true
  # Bitnami moved its versioned free images to the bitnamilegacy/ namespace in
  # 2025; the tag this subchart pins no longer exists under bitnami/, so the
  # pod cannot start without this override.
  --set mysql.image.repository=bitnamilegacy/mysql
  --set mysql.primary.resourcesPreset=none
  --set mysql.primary.resources.requests.cpu=500m
  --set mysql.primary.resources.requests.memory=1Gi
  --set mysql.primary.resources.limits.cpu=2
  --set mysql.primary.resources.limits.memory=3Gi
  # max_connections via extraFlags, NOT primary.configuration: that key
  # replaces Bitnami's whole my.cnf, and dropping its [client] section sends
  # mysqladmin to the wrong socket, so the health probe fails and the pod
  # never becomes ready even though the server is up.
  # utf8mb4 must be the SERVER default before the database is created: the
  # chart creates it with whatever the server says, and Bitnami ships
  # utf8mb3. A utf8mb3 database rejects any 4-byte character — an emoji in
  # a case field or a search box comes back as a 503, not a validation error.
  --set mysql.primary.extraFlags="--max_connections=500 --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci"
)

# An external database replaces the bundled subchart entirely.
if db_is_external; then
  DB_DEFAULTS=(
    --set postgresql.enabled=false
    --set mysql.enabled=false
    --set externalDatabaseUrl="$EXTERNAL_URL"
    --set externalDatabaseBackend=mysql
  )
  warn "using an EXTERNAL database — the bundled mysql will not be installed"
  ok "host: $(_dsn_part host):$(_dsn_part port)  database: $(_dsn_part name)"
fi

if [ "$DRY_RUN" = "1" ]; then
  helm install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
    "${DB_DEFAULTS[@]}" "${IMAGE_SETS[@]}" "${EXTRA_VALUES[@]}" --dry-run=client >/tmp/velaris-render.yaml \
    && ok "rendered $(grep -c '^kind:' /tmp/velaris-render.yaml) objects to /tmp/velaris-render.yaml"
  exit 0
fi

helm install "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" --create-namespace \
  "${DB_DEFAULTS[@]}" "${IMAGE_SETS[@]}" "${EXTRA_VALUES[@]}" --timeout "$TIMEOUT" >/dev/null \
  || die "helm install failed"
ok "release installed"

step "Step 5/7 — Waiting for the database"
db_is_external && ok "external database: $(_dsn_part host)"
for i in $(seq 1 60); do
  [ "$(sql_q 'SELECT 1' 2>/dev/null | tr -d '[:space:]')" = "1" ] && break
  sleep 5
done
[ "$(sql_q 'SELECT 1' 2>/dev/null | tr -d '[:space:]')" = "1" ] || die "database never became reachable"
ok "database reachable"

# ── Charset check ──────────────────────────────────────────────────
# A utf8mb3 database rejects every 4-byte character (emoji, extended CJK)
# with a 503 rather than a validation error. Two very different cases:
#
#   empty schema  -> ALTER DATABASE is free, nothing exists to rewrite, and
#                    the migrations about to run would otherwise create
#                    utf8mb3 tables. Fixed here, and said out loud.
#   tables present -> converting rebuilds every table. That is an operator's
#                    decision in a maintenance window, never a side effect of
#                    running setup. Warn and point at the tool.
CS="$(sql_q "SELECT @@character_set_database" | tr -d '[:space:]')"
if [ "$CS" != "utf8mb4" ]; then
  EXISTING="$(sql_q "SELECT count(*) FROM information_schema.tables
                     WHERE table_schema = DATABASE()" | tr -d '[:space:]')"
  if [ "${EXISTING:-0}" = "0" ]; then
    warn "database charset is ${CS:-unknown}; setting utf8mb4 before any table exists"
    sql_q "ALTER DATABASE \`$(db_name)\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" >/dev/null \
      && ok "database charset is now utf8mb4" \
      || die "could not set the database charset to utf8mb4"
  else
    warn "database charset is ${CS:-unknown} and it already holds ${EXISTING} table(s)."
    warn "4-byte characters (emoji, extended CJK) will fail with VEL-DB-UNAVAILABLE-503."
    warn "Converting rebuilds every table, so it is NOT done here. Check it with:"
    warn "    $K8_DIR/convert-utf8mb4.sh            # dry run"
    warn "    $K8_DIR/convert-utf8mb4.sh --apply    # in a maintenance window"
  fi
else
  ok "database charset: utf8mb4"
fi

# ── Step 6: migrations ─────────────────────────────────────────────
step "Step 6/7 — Database migrations"
run_migrations || die "migrations failed — the platform was NOT started."

# ── Step 7: superadmin + verification ──────────────────────────────
step "Step 7/7 — Verification"
if [ "$(superadmin_count)" = "0" ]; then
  warn "No superadmin account exists yet."
  warn "Every API endpoint answers 503 until one does (health probes excepted)."
  warn "Create one with the interactive helper on a machine that can reach this database:"
  warn "    python3 $VELARIS_DIR/scripts/setup_superadmin.py"
else
  ok "superadmin present"
fi

wait_ready 600 || warn "some pods are not ready — see 'kubectl -n $NAMESPACE get pods'"

echo
echo "${GREEN}${BOLD}Install complete.${NC}"
echo "  kubectl -n $NAMESPACE get pods"
echo "  kubectl -n $NAMESPACE port-forward svc/$(deploy_name case-service) 8200:8200"
echo
echo "  The chart does NOT expose the platform: ingress.enabled is false by"
echo "  default. Enable it with your own host, and set ingress.tls to a secret"
echo "  holding the certificate — without that entry it is served over plain HTTP."
