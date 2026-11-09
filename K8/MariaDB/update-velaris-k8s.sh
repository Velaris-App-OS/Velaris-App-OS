#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes (MariaDB) — update an existing install
#
#  Someone is already running this platform. An update that changes
#  nothing for them must break nothing for them:
#
#    * values already in the release are REUSED, never re-derived, so a
#      setting an operator applied by hand survives the update;
#    * nothing is enabled that was not already enabled;
#    * only migrations not yet recorded are applied, and a failing one
#      stops the run instead of being recorded as done;
#    * the previous revision is rolled back to if the rollout fails.
#
#  Usage:  ./update-velaris-k8s.sh
#          ./update-velaris-k8s.sh --build          # rebuild images first
#          ./update-velaris-k8s.sh --kind <cluster>
#          ./update-velaris-k8s.sh --skip-migrations
#          ./update-velaris-k8s.sh --dry-run
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

BUILD=0; KIND_CLUSTER=""; DRY_RUN=0; SKIP_MIG=0; EXTRA_VALUES=()
TIMEOUT="${VELARIS_K8S_TIMEOUT:-10m}"
REGISTRY="${VELARIS_IMAGE_REGISTRY:-helix}"
TAG="${VELARIS_IMAGE_TAG:-latest}"

while [ $# -gt 0 ]; do
  case "$1" in
    --build)           BUILD=1; shift ;;
    --kind)            KIND_CLUSTER="$2"; shift 2 ;;
    --dry-run)         DRY_RUN=1; shift ;;
    --skip-migrations) SKIP_MIG=1; shift ;;
    -f|--values)       EXTRA_VALUES+=(-f "$2"); shift 2 ;;
    --timeout)         TIMEOUT="$2"; shift 2 ;;
    -h|--help)         sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

echo "${BOLD}VELARIS — Kubernetes update${NC}"
step "Step 1/6 — Preflight"
require_cluster
release_exists || die "no release '$RELEASE' in namespace '$NAMESPACE'. Use $K8_DIR/setup-velaris-k8s.sh first."
# The revision to return to is the last one helm actually has DEPLOYED. A
# failed upgrade often leaves that same revision live, and "rollback to the
# revision you are already on" is an error, not a recovery.
PREV_REV="$(helm -n "$NAMESPACE" history "$RELEASE" -o json 2>/dev/null \
            | tr '}' '\n' | grep '"status":"deployed"' \
            | grep -o '"revision":[0-9]*' | tail -1 | tr -dc '0-9')"
ok "last deployed revision: ${PREV_REV:-unknown}"

rollback_if_needed() {
  local now
  now="$(helm -n "$NAMESPACE" history "$RELEASE" -o json 2>/dev/null \
         | tr '}' '\n' | grep '"status":"deployed"' \
         | grep -o '"revision":[0-9]*' | tail -1 | tr -dc '0-9')"
  if [ -n "$PREV_REV" ] && [ "$now" = "$PREV_REV" ]; then
    warn "the install is still on revision $PREV_REV — nothing to roll back"
    return 0
  fi
  warn "rolling back to revision ${PREV_REV:-previous}"
  if helm -n "$NAMESPACE" rollback "$RELEASE" "${PREV_REV:-0}" --timeout "$TIMEOUT" >/dev/null 2>&1; then
    warn "rolled back to revision $PREV_REV"
  else
    err "rollback FAILED — inspect 'helm -n $NAMESPACE history $RELEASE' now"
  fi
}

# Record the live replica counts. The chart writes .spec.replicas AND ships an
# HPA for the same deployments, so once the HPA has scaled anything, a plain
# helm upgrade fails with a field-manager conflict on .spec.replicas. Pinning
# the current counts keeps the upgrade from fighting the autoscaler — and from
# silently resetting a deployment an operator scaled by hand.
step "Step 2/6 — Preserving current scale"
REPLICA_SETS=()
declare -A VALUE_KEY=(
  [case-service]=caseService [engine]=engine [worker]=worker
  [analytics-service]=analyticsService [hxgraph-service]=hxgraphService
  [hxmigrate-service]=hxmigrateService
  [commerce-service]=commerceService
  [portal-service]=portalService
  [user-service]=userService
  [jobs]=jobs
)
for svc in "${WORKLOADS[@]}"; do
  d="$(deploy_name "$svc")"
  n="$(kubectl -n "$NAMESPACE" get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  if [ -n "$n" ]; then
    REPLICA_SETS+=(--set "${VALUE_KEY[$svc]}.replicaCount=$n")
    ok "$svc: $n replica(s)"
  fi
done

step "Step 3/6 — Container images"
if [ "$BUILD" = "1" ]; then
  need_tool docker
  for svc in case-service analytics-service hxgraph-service hxmigrate-service commerce-service portal-service user-service engine; do
    df="$VELARIS_DIR/services/$svc/Dockerfile"
    [ "$svc" = "engine" ] && df="$VELARIS_DIR/engine/Dockerfile"
    echo -n "    build $REGISTRY/$svc:$TAG ... "
    docker build -q --build-arg DB_EXTRAS="[mysql]" -f "$df" -t "$REGISTRY/$svc:$TAG" "$VELARIS_DIR" >/dev/null 2>&1 \
      && echo "${GREEN}✓${NC}" || { echo "${RED}✗${NC}"; die "build failed for $svc"; }
    [ -n "$KIND_CLUSTER" ] && kind load docker-image "$REGISTRY/$svc:$TAG" --name "$KIND_CLUSTER" >/dev/null 2>&1
  done
else
  ok "skipping build (pass --build to rebuild from source)"
fi

step "Step 4/6 — Database migrations"
if [ "$DRY_RUN" = "1" ]; then
  warn "dry run: migrations are NOT applied"
  pending=0
  for f in $(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort -V); do
    name="$(basename "$f")"
    [ "$(sql_q "SELECT 1 FROM schema_migrations WHERE filename='$name'" 2>/dev/null | tr -d '[:space:]')" = "1" ] \
      || { echo "    would apply $name"; pending=$((pending + 1)); }
  done
  ok "$pending migration(s) pending"
elif [ "$SKIP_MIG" = "1" ]; then
  warn "skipped by request"
else
  # Migrations run BEFORE the new code, against the version still serving
  # traffic. They must therefore be additive and safe for the old code.
  run_migrations || die "migrations failed — nothing was upgraded."
fi

# Images are re-pinned ONLY when the operator asked for it. Deriving them on
# every update would overwrite an image an operator set by hand, which is
# exactly what --reuse-values exists to prevent.
IMAGE_SETS=()
if [ -n "${VELARIS_IMAGE_REGISTRY:-}" ] || [ -n "${VELARIS_IMAGE_TAG:-}" ]; then
  build_image_sets "$REGISTRY" "$TAG"
  ok "pinning images to $REGISTRY/*:$TAG"
fi

step "Step 5/6 — Upgrading the release"
helm dependency build "$CHART_DIR" >/dev/null 2>&1 || helm dependency update "$CHART_DIR" >/dev/null 2>&1

if [ "$DRY_RUN" = "1" ]; then
  helm upgrade "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" --reuse-values \
    "${REPLICA_SETS[@]}" "${IMAGE_SETS[@]}" "${EXTRA_VALUES[@]}" --dry-run=client >/tmp/velaris-upgrade.yaml \
    && ok "rendered to /tmp/velaris-upgrade.yaml (nothing changed)"
  exit 0
fi

if helm upgrade "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" --reuse-values \
     "${REPLICA_SETS[@]}" "${IMAGE_SETS[@]}" "${EXTRA_VALUES[@]}" --timeout "$TIMEOUT" >/tmp/velaris-upgrade.log 2>&1; then
  # Read the revision the same way PREV_REV is read. `helm status -o json`
  # does not carry a top-level "revision" key in helm 4, so the old expression
  # printed "upgraded to revision " with nothing after it.
  ok "upgraded to revision $(helm -n "$NAMESPACE" history "$RELEASE" -o json 2>/dev/null \
        | tr '}' '\n' | grep '"status":"deployed"' \
        | grep -o '"revision":[0-9]*' | tail -1 | tr -dc '0-9')"
else
  err "$(tail -4 /tmp/velaris-upgrade.log)"
  # A field-manager conflict means something outside helm owns a field the
  # chart also writes — almost always a `kubectl set image` / `edit` / `scale`
  # run by hand. Helm refuses rather than silently overwrite it, and nothing
  # was changed. Helm's raw apply error does not say what to do next.
  if grep -qi "conflict" /tmp/velaris-upgrade.log; then
    echo
    err "Something other than helm owns a field on one of these objects"
    err "(typically a hand-run kubectl set/edit/scale). NOTHING was changed."
    err "Give the field back to helm, then re-run this script:"
    err "    helm -n $NAMESPACE get manifest $RELEASE \\"
    err "      | kubectl -n $NAMESPACE apply --server-side --force-conflicts \\"
    err "          --field-manager=helm -f -"
    err "(--field-manager=helm matters: applying under any other name moves the"
    err " ownership sideways instead of back, and the next upgrade conflicts again.)"
  fi
  rollback_if_needed
  exit 1
fi

step "Step 6/6 — Verification"
if ! wait_ready 600; then
  err "pods did not become ready after the upgrade."
  rollback_if_needed
  exit 1
fi
[ "$(superadmin_count)" = "0" ] && warn "no active superadmin: the API answers 503 until one exists"

echo
echo "${GREEN}${BOLD}Update complete.${NC}"
echo "  helm -n $NAMESPACE history $RELEASE"
