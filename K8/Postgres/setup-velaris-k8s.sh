#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes — first install
#
#  Installs the Helm chart into a cluster, applies the database
#  migrations and verifies the platform answers. Safe to re-run: an
#  existing release is left alone unless --upgrade is given (use
#  update-velaris-k8s.sh for that).
#
#  Usage:  ./setup-velaris-k8s.sh
#          ./setup-velaris-k8s.sh --external-url <dsn>   # reuse an existing database
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
#      "postgresql+asyncpg://user:pass@host:5432/velaris"
#
#  Prefer VELARIS_K8S_EXTERNAL_URL over the flag so the password does not
#  reach your shell history. The DSN is rendered into the deployment
#  environment either way, exactly as the bundled password is today.
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
if [ "$BUILD" = "1" ]; then
  need_tool docker
  for svc in "${IMAGES[@]}"; do
    echo -n "    build $REGISTRY/$svc:$TAG ... "
    local_df="$VELARIS_DIR/services/$svc/Dockerfile"
    [ "$svc" = "engine" ] && local_df="$VELARIS_DIR/engine/Dockerfile"
    if docker build -q -f "$local_df" -t "$REGISTRY/$svc:$TAG" "$VELARIS_DIR" >/dev/null 2>&1; then
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

# Resource floors for the bundled database. The subchart's own default preset
# is 192Mi/150m, which a real workload OOM-kills within minutes; and its
# max_connections of 100 is below what a handful of pods open (each service
# pod holds up to 21). Both are set here, not in the chart, so an operator
# who supplies their own -f values keeps the last word.
DB_DEFAULTS=(
  --set postgresql.primary.resourcesPreset=none
  --set postgresql.primary.resources.requests.cpu=500m
  --set postgresql.primary.resources.requests.memory=1Gi
  --set postgresql.primary.resources.limits.cpu=2
  --set postgresql.primary.resources.limits.memory=3Gi
  --set postgresql.primary.extendedConfiguration="max_connections = 500"
)

# An external database replaces the bundled subchart entirely.
if db_is_external; then
  DB_DEFAULTS=(
    --set postgresql.enabled=false
    --set externalDatabaseUrl="$EXTERNAL_URL"
    --set externalDatabaseBackend=postgresql
  )
  warn "using an EXTERNAL database — the bundled PostgreSQL will not be installed"
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
  [ "$(psql_q 'SELECT 1' 2>/dev/null | tr -d '[:space:]')" = "1" ] && break
  sleep 5
done
[ "$(psql_q 'SELECT 1' 2>/dev/null | tr -d '[:space:]')" = "1" ] || die "database never became reachable"
ok "database reachable"

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
