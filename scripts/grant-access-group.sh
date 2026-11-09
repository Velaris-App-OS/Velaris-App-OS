#!/usr/bin/env bash
# Put accounts that hold a platform role but no access group into one group.
#
# Since the 2026-10-18 release, creating a case is decided by the caller's
# access group (Admin Studio); the platform `admin` role alone no longer
# grants it, and an account in no group is refused. The website's product-key
# sign-up gave every demo account the platform role `admin` and no group, so
# those accounts lose case creation on update until they are in one. This adds
# them — and only them: an account already in any group is left alone.
#
#   ./scripts/grant-access-group.sh                 # dry run: list who would be added
#   ./scripts/grant-access-group.sh --apply         # add them
#
# Options (defaults match the registration service's VELARIS_DEMO_ACCESS_GROUP_*):
#   --role ROLE       platform role to select (default: admin)
#   --group NAME      access group to join (default: "Demo Users")
#   --tenant TENANT   the group's tenant (default: default)
#
# The group is created when missing, with the built-in full-access `admin`
# access role and the Staff Studio portal (both seeded by migration 033) —
# the same group the registration service creates. Superadmins are skipped:
# they pass every gate already. Re-runnable: a second run adds nobody.
# Bundled PostgreSQL only (the velaris-db container).
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

ROLE="admin"; GROUP="Demo Users"; TENANT="default"; APPLY=false
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)  APPLY=true ;;
    --role)   ROLE="${2:?--role needs a value}"; shift ;;
    --group)  GROUP="${2:?--group needs a value}"; shift ;;
    --tenant) TENANT="${2:?--tenant needs a value}"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

DB="$(docker ps -qf 'label=com.docker.compose.service=velaris-db' | head -1)"
if [ -z "$DB" ]; then
  echo "✗ No running velaris-db container — this script works on the bundled PostgreSQL only." >&2
  exit 1
fi
psqlv() { docker exec -i "$DB" psql -U velaris -d velaris -v ON_ERROR_STOP=1 -tA "$@"; }

# Values reach SQL as psql variables (:'name'), never spliced into the text.
VARS=(-v role="$ROLE" -v grp="$GROUP" -v tenant="$TENANT")

CANDIDATES_SQL="
  SELECT u.username FROM velaris_users u
   WHERE u.is_active AND NOT u.is_superadmin
     AND u.roles::jsonb ? :'role'
     AND NOT EXISTS (SELECT 1 FROM operator_access_groups o
                      WHERE o.operator_id IN (u.username, u.email, u.id::text))
   ORDER BY u.username"

mapfile -t WHO < <(psqlv "${VARS[@]}" <<<"$CANDIDATES_SQL")
if [ "${#WHO[@]}" -eq 0 ]; then
  echo "✓ No active account with role '$ROLE' is outside an access group — nothing to do."
  exit 0
fi
echo "Accounts with role '$ROLE' and no access group (${#WHO[@]}):"
printf '    %s\n' "${WHO[@]}"

if [ "$APPLY" != true ]; then
  echo "Dry run. Re-run with --apply to add them to '$GROUP' (tenant '$TENANT')."
  exit 0
fi

psqlv "${VARS[@]}" <<SQL
BEGIN;
INSERT INTO access_groups (id, name, description, tenant_id, portal_id, role_ids,
                           allowed_case_type_ids, allowed_queue_ids, is_default, is_active)
SELECT gen_random_uuid(), :'grp',
       'Demo accounts from the website''s product-key sign-up (full access)',
       :'tenant', '00000000-0000-0000-0000-000000000001',
       '["00000000-0000-0001-0000-000000000001"]', '["*"]', '["*"]', FALSE, TRUE
 WHERE NOT EXISTS (SELECT 1 FROM access_groups WHERE name = :'grp' AND tenant_id = :'tenant');

INSERT INTO operator_access_groups (id, operator_id, access_group_id, is_primary)
SELECT gen_random_uuid(), c.username,
       (SELECT id FROM access_groups WHERE name = :'grp' AND tenant_id = :'tenant' LIMIT 1), TRUE
  FROM ($CANDIDATES_SQL) c
ON CONFLICT DO NOTHING;
COMMIT;
SQL
echo "✓ Added ${#WHO[@]} account(s) to '$GROUP'. They can create cases at their next request"
echo "  (services cache access decisions for up to 30 s)."
