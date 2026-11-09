#!/usr/bin/env bash
# One-time check and preparation BEFORE pulling the November 2026 tenant
# isolation changes (CLAUDE.md, the 2026-11-04 records).
#
# Run it on the server while the CURRENT version is still running. It is
# self-contained — the server's tree does not have this file yet, so copy it
# over (scp) and run it from the install directory:
#
#   ./pre-update-2026-11.sh            # dry run: report only, changes nothing
#   ./pre-update-2026-11.sh --apply    # back up, then make the changes below
#
# What --apply does (each step is additive and safe for the running version):
#   1. pg_dump backup into .backups/ — first, before anything else.
#   2. Migration 230 (security_events.tenant_id + index) applied and recorded,
#      so the new code's first security event finds its column. The update
#      would apply it too; doing it now removes the one ordering risk.
#   3. Cases with NO tenant whose creator is in the default tenant are stamped
#      `default` (their ids saved first). Without a tenant a case is read by
#      EVERY tenant; strict case reads can only go on once none are left.
#      A case whose creator is in another tenant is never guessed — listed.
#
# What it only REPORTS (decisions that stay yours):
#   * every account in the default tenant — each one is a PLATFORM OPERATOR
#     after the update (sees the whole server). Only your own staff belong there.
#   * tenants with max_users / max_cases set — enforced after the update.
#   * rows with no tenant that the default tenant alone sees after the update
#     (connectors, saved reports, logs, incidents…).
#   * whether strict case reads can be switched on, and how.
#   * a Cloudflare tunnel pointing at the gateway (VELARIS_GW_TRUST_CF_CONNECTING_IP).
#
# Never: enables a setting, deletes a row, touches the website. PostgreSQL
# only (bundled container or VELARIS_CASE_DATABASE_URL); a MySQL/MariaDB
# install gets the report and is told what to run by hand.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

APPLY=false
case "${1:-}" in
  --apply) APPLY=true ;;
  ""|--dry-run) ;;
  -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
  *) echo "unknown option: $1 (use --apply, or nothing for a dry run)" >&2; exit 2 ;;
esac

VELARIS_DIR="${VELARIS_DIR:-$(pwd)}"
ENV_FILE="$VELARIS_DIR/.env"
[ -f "$ENV_FILE" ] || { echo "✗ No .env in $VELARIS_DIR — run this from the install directory." >&2; exit 1; }
get_env() { grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^"//; s/"$//'; }

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="$VELARIS_DIR/.backups"
mkdir -p "$OUT_DIR" && chmod 700 "$OUT_DIR"
REPORT="$OUT_DIR/pre-update-2026-11-$STAMP.txt"
: > "$REPORT"; chmod 600 "$REPORT"
say()  { printf '%s\n' "$*" | tee -a "$REPORT"; }
head_() { say ""; say "── $* ──"; }

# ── Database access: the same three shapes update-velaris.sh uses ─────────
DB_URL="$(get_env VELARIS_CASE_DATABASE_URL)"
case "$DB_URL" in
  mysql*|mariadb*)
    echo "✗ This install runs on MySQL/MariaDB. Apply migrations/mysql/230 with the update," >&2
    echo "  and review tenant-less cases by hand (see CLAUDE.md, 2026-11-04)." >&2
    exit 1 ;;
esac
DB_CONTAINER="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^(docker-compose-velaris-db-1|velaris-db)$' | head -1 || true)"
DB_CONTAINER="${DB_CONTAINER:-docker-compose-velaris-db-1}"
DB_USER="${DB_USER:-velaris}"; DB_NAME="${DB_NAME:-velaris}"   # as update-velaris.sh sets them
if [ -f "$VELARIS_DIR/deploy/external-services.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/external-services.sh" 2>/dev/null || true
fi
if ! type velaris_psql >/dev/null 2>&1; then
  if [ -n "$DB_CONTAINER" ]; then
    velaris_psql()    { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }
    velaris_pg_dump() { docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" "$@"; }
  elif [ -n "$DB_URL" ] && command -v psql >/dev/null 2>&1; then
    PGURL="${DB_URL/+asyncpg/}"
    velaris_psql()    { psql "$PGURL" "$@"; }
    velaris_pg_dump() { pg_dump "$PGURL" "$@"; }
  else
    echo "✗ Cannot reach the database: no bundled velaris-db container and no psql + URL." >&2
    exit 1
  fi
fi
q()  { velaris_psql -v ON_ERROR_STOP=1 -tA -F $'\t' -c "$1" </dev/null; }
qv() { velaris_psql -v ON_ERROR_STOP=1 -q "$@"; }
q "SELECT 1" >/dev/null || { echo "✗ The database did not answer." >&2; exit 1; }
has_table() { [ "$(q "SELECT to_regclass('public.$1') IS NOT NULL")" = "t" ]; }
has_col()   { [ "$(q "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='$1' AND column_name='$2'")" = "1" ]; }

say "Velaris pre-update check — $(date -Is)   mode: $([ "$APPLY" = true ] && echo APPLY || echo 'dry run (nothing changed)')"
say "Install: $VELARIS_DIR"

DEFAULT_ID="$(q "SELECT id FROM tenants WHERE slug='default' LIMIT 1" || true)"
[ -n "$DEFAULT_ID" ] || say "⚠ No 'default' tenant row — steps that need it are skipped."

# ── 1. Backup ─────────────────────────────────────────────────────────────
head_ "1. Backup"
if [ "$APPLY" = true ]; then
  BK="$OUT_DIR/pre-update-2026-11-$STAMP.sql"
  velaris_pg_dump > "$BK"
  chmod 600 "$BK"
  [ -s "$BK" ] || { say "✗ The backup is empty — stopping before any change."; exit 1; }
  say "✓ $BK ($(du -h "$BK" | cut -f1))"
else
  say "(dry run) --apply first writes a pg_dump to $OUT_DIR/"
fi

# ── 2. Migration 230 ──────────────────────────────────────────────────────
head_ "2. Migration 230 — security_events.tenant_id"
if has_col security_events tenant_id; then
  say "✓ Already present."
elif [ "$APPLY" = true ]; then
  # Additive and safe for the running version (it neither reads nor writes the
  # column). Separate statements: CONCURRENTLY refuses a transaction.
  qv -c "ALTER TABLE security_events ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255);" </dev/null
  qv -c "DROP INDEX CONCURRENTLY IF EXISTS idx_security_events_tenant;" </dev/null
  qv -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_security_events_tenant ON security_events (tenant_id, timestamp);" </dev/null
  if has_table schema_migrations; then
    qv -c "INSERT INTO schema_migrations(filename) VALUES('230_security_events_tenant.sql') ON CONFLICT DO NOTHING;" </dev/null
  fi
  say "✓ Applied and recorded (the update will see it as done)."
else
  say "(dry run) would add the column and index, and record 230 as applied."
fi

# ── 3. Cases with no tenant ───────────────────────────────────────────────
head_ "3. Cases with no tenant (readable by EVERY tenant until stamped)"
NULL_CASES="$(q "SELECT COUNT(*) FROM case_instances WHERE tenant_id IS NULL")"
say "Cases with no tenant: $NULL_CASES"
# A creator with no directory row, or one in the default tenant, is the default
# tenant's — the platform's own reading of a caller with no tenant.
OWNED_SQL="
  SELECT c.id FROM case_instances c
   WHERE c.tenant_id IS NULL AND NOT EXISTS (
     SELECT 1 FROM velaris_users u JOIN user_directory d ON d.user_id = u.id::text
      WHERE (u.id::text = c.created_by OR u.username = c.created_by)
        AND d.tenant_id IS NOT NULL AND d.tenant_id NOT IN ('default', '${DEFAULT_ID}'))"
if [ "$NULL_CASES" != "0" ] && [ -n "$DEFAULT_ID" ]; then
  TO_STAMP="$(q "SELECT COUNT(*) FROM ($OWNED_SQL) x")"
  OTHER=$((NULL_CASES - TO_STAMP))
  say "  made by default-tenant (or no-tenant) accounts: $TO_STAMP — stamped 'default' by --apply"
  say "  made by an account in ANOTHER tenant:            $OTHER — never guessed; review:"
  [ "$OTHER" -gt 0 ] && q "SELECT c.id, c.created_by FROM case_instances c WHERE c.tenant_id IS NULL
                           AND c.id NOT IN ($OWNED_SQL) LIMIT 50" | sed 's/^/      /' | tee -a "$REPORT"
  if [ "$APPLY" = true ] && [ "$TO_STAMP" -gt 0 ]; then
    IDS="$OUT_DIR/pre-update-2026-11-$STAMP-cases-stamped.txt"
    q "$OWNED_SQL" > "$IDS"; chmod 600 "$IDS"
    qv -c "UPDATE case_instances SET tenant_id='${DEFAULT_ID}' WHERE id IN ($OWNED_SQL);" </dev/null
    say "✓ Stamped $TO_STAMP case(s) 'default' — ids in $IDS (undo: set tenant_id NULL for those ids)."
  fi
fi
LEFT="$(q "SELECT COUNT(*) FROM case_instances WHERE tenant_id IS NULL")"
STRICT="$(get_env VELARIS_CASE_TENANT_STRICT_READS)"
if [ "$LEFT" = "0" ]; then
  say "→ No case is left without a tenant. Strict case reads can go on (your decision):"
  say "    add VELARIS_CASE_TENANT_STRICT_READS=true to .env, then restart. Now: '${STRICT:-unset}'."
else
  say "→ $LEFT case(s) still have no tenant: leave strict case reads OFF until that is 0."
fi

# ── 4. Platform operators ─────────────────────────────────────────────────
head_ "4. Accounts in the DEFAULT tenant — each is a platform operator after the update"
q "SELECT u.username, u.email, u.roles::text, CASE WHEN u.is_superadmin THEN 'superadmin' ELSE '' END
     FROM velaris_users u LEFT JOIN user_directory d ON d.user_id = u.id::text
    WHERE (d.tenant_id IS NULL OR d.tenant_id IN ('default', '${DEFAULT_ID}'))
      AND u.is_active $(has_col velaris_users deleted_at && echo 'AND u.deleted_at IS NULL')
    ORDER BY u.username" | sed 's/^/    /' | tee -a "$REPORT"
say "→ Only your own staff belong here. Website demo accounts are moved after the pull with"
say "  website/backend/registration-service/move_demo_accounts.py (dry run first, then --apply --keep …)."

# ── 5. Tenant limits ──────────────────────────────────────────────────────
head_ "5. Tenant limits (stored before, ENFORCED after the update)"
LIM="$(q "SELECT t.slug, COALESCE(t.max_users::text,'-'), COALESCE(t.max_cases::text,'-'),
                 (SELECT COUNT(*) FROM user_directory d WHERE d.tenant_id IN (t.slug, t.id::text)),
                 (SELECT COUNT(*) FROM case_instances c WHERE c.tenant_id = t.id)
            FROM tenants t WHERE t.max_users IS NOT NULL OR t.max_cases IS NOT NULL")"
if [ -z "$LIM" ]; then say "✓ No tenant has a limit — nothing changes."
else
  say "    slug  max_users  max_cases  users_now  cases_now"
  printf '%s\n' "$LIM" | sed 's/^/    /' | tee -a "$REPORT"
  say "→ A tenant at its limit cannot add users / cases after the update."
fi

# ── 6. Tenant-less rows ───────────────────────────────────────────────────
head_ "6. Rows with no tenant — the default tenant alone sees them after the update"
for t in connector_registry saved_reports identity_provision_log security_incidents shield_responses \
         audit_stream_targets email_accounts sync_destinations hxwork_boards process_definitions \
         customer_profiles legal_holds gdpr_requests test_runs; do
  has_table "$t" && has_col "$t" tenant_id || continue
  n="$(q "SELECT COUNT(*) FROM $t WHERE tenant_id IS NULL")"
  [ "$n" != "0" ] && say "    $t: $n"
done
say "→ Expected: the operator's own items. Other tenants that used a tenant-less connector"
say "  lose it — give them their own copy if they need it."

# ── 7. Front of the gateway ───────────────────────────────────────────────
head_ "7. What is in front of the gateway"
CF=""
for f in "$HOME/.cloudflared/config.yml" /etc/cloudflared/config.yml; do
  [ -f "$f" ] && grep -Eq "localhost:8200|127\.0\.0\.1:8200" "$f" && CF="$f"
done
if [ -n "$CF" ]; then
  say "A Cloudflare tunnel ($CF) sends traffic straight to the gateway."
  say "→ After the update, set VELARIS_GW_TRUST_CF_CONNECTING_IP=true in .env so each visitor"
  say "  gets its own rate-limit bucket. Now: '$(get_env VELARIS_GW_TRUST_CF_CONNECTING_IP)'."
else
  say "No Cloudflare tunnel to the gateway found — leave VELARIS_GW_TRUST_CF_CONNECTING_IP unset."
fi
say "Note: /metrics now answers 404 to anything that came through a front; a Prometheus that"
say "scrapes it locally (the bundled one) is unaffected."

head_ "Done"
if [ "$APPLY" = true ]; then
  say "Prepared. Next: git pull, then ./update-velaris.sh as usual."
else
  say "Dry run — nothing changed. Read the above, then run with --apply."
fi
say "Report: $REPORT"
