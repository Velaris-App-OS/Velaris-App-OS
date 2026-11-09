# Changelog

What changed in each release, for the people who run Velaris. The full notes,
including upgrade steps and anything an operator must do, are in
`releases/notes/v<version>.md`.

A release that **contains a security fix** says so here. Take it promptly.
Every release keeps what an install already has: nothing is removed or renamed
without a line in its notes saying so and why.

## 3.2.0 — The Split, The Boundary, and The Way Back
Minor release, **contains security fixes**. Upgrade from 3.1.2. Migrations
218–231, all additive; nothing removed or renamed. On upgrade a user in no
access group can no longer create or complete case work, and portal customers
sign in again once. Full notes: `releases/notes/v3.2.0.md`.

**Features**
- Services of their own: user-service, portal-service, commerce-service and
  worker-service, beside analytics, HxGraph and HxMigrate. Client URLs unchanged.
- Form Builder redesigned: sections, conditions, versions, drafts, connector
  lookups, translations and field-level security.
- Connector operations, and the database query connector: saved read-only
  SELECTs on your own PostgreSQL, MySQL or MariaDB database.
- A large connector response is readable at the `ref_url` the call records
  (`GET /api/v1/hxbridge/overflow/{call_id}`); that URL had no route.
- Legal holds can require a second person to release.
- A tenant's administrator sees their own tenant's logs, metrics, AI-call
  records and security events.


**Security**
- The MySQL/MariaDB database password no longer appears on a command line
  (setup, start, update, and the Kubernetes scripts): any local user could read
  it from the process list, and inside the database pod. It is handed over
  through the environment or standard input.
- An update's clean-up of old release files can never remove anything outside
  the install, nor what an install holds (`.env`, keys, certificates, backups,
  data), whatever the file list says.
- An empty or mistyped `VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT` turned case
  permissions advisory-only. It now stays enforced.
- The Helm chart signed every Kubernetes install's tokens with a default secret
  from the source. It now generates one per install and keeps it.
- A tenant's administrator could change built-in roles and system-wide portals,
  and put another tenant's roles or users in their groups. These are now
  refused.
- Archiving a tenant did not close it. An archived tenant's people are now
  refused everywhere, and its portal answers "not found". Restoring the tenant
  re-opens it.
- Rate limits are per person (client address), not one bucket for everyone
  behind Studio. A client cannot fake its address to escape them.
- Website demo sign-ups were administrators of the install's own (default)
  tenant. Each now gets its own demo tenant.
- A tenant's connectors, database sources, mail hosts and webhooks could reach
  the server's own network (`127.0.0.1`, private and metadata addresses). They
  reach the internet only, checked again at connect and pinned to the checked
  address — verified TLS included.
- A tenant's administrator could bind any account, the operator's included, to
  an SSO identity they own, and create, change or delete tenants.
- Creating a case, and completing, claiming, reassigning or deleting a step,
  needed only a sign-in. They now need an access group.
- A form lookup could run any connector with any input. It runs only what a
  saved form says.
- Portal customer tokens were signed with a key that is in the source code.
- The install's audit chain and platform-wide screens were open to every
  tenant's administrator. They are the operator's.
- `/metrics` was readable from the internet through a tunnel front.

**Updates and rollback** (found rehearsing an unattended update of a clean 3.1.2 install)
- MySQL and MariaDB installs can now be updated: backup, migrations and the
  release manifest run against your own database server. Before, the updater
  refused them. **A 3.1.2 MySQL/MariaDB install runs 3.1.2's updater, which
  still refuses:** run this release's `update-velaris.sh` by hand once.
- A rollback no longer leaves sign-in broken: the gateway is recreated on the
  release now on disk, and the health check also tries a request in each part
  of the API, not just `/health`.
- An update from your own update server no longer resets `velaris.yaml`: the
  update source, server, channel, schedule and database settings are kept.
- A tarball update removes the files the previous release shipped and the new
  one does not, instead of leaving them behind. Files you added are untouched.
- An update stopped halfway is finished on the next run, whatever version the
  channel pins.
- With internal TLS on, an update no longer ends as "unhealthy" on a healthy
  platform.
- An update no longer fills in a setting you left empty (it had switched on
  storage encryption where the key was empty). Only new settings get values.
- Turning internal TLS on works on an install that has already started once.
- New secrets made during an update now reach OpenBao.
- A document that cannot be decrypted answers `VEL-DOC-UNREADABLE-409`, saying
  why, instead of an error 500.
- `scripts/package-release.sh` builds the tarball an update server serves.
- Studio: HxDeploy's Rollouts tab works (it answered "Not Found"); HxConnect's
  Webhook Events tab no longer blanks the page for someone not allowed to see it
  (it says why); `/login` while signed in, or an unknown address, no longer
  shows an empty page.
- MySQL and MariaDB installs start again after the update: a startup check
  added this month refused the empty database URL they use (their connection is
  in `VELARIS_CASE_DB_HOST`/`_PORT`/`_NAME`/`_USER`). An empty URL is refused
  now only when no database host is set either.

**Operations**
- No single empty or mistyped line in `.env` stops a service from starting. The
  default is used and the log names the variable. Two settings refuse to start
  when empty and say so.
- Studio → Status lists every dependency as working, degraded or off, with the
  reason. So does the end of every update. Indexing says when no embedding
  model answered.
- `./support-bundle.sh` makes a fault report bundle with secrets and personal
  data removed.
- A default Prometheus alert set. The bundled Prometheus can now reach the
  platform.
- `VELARIS_WORKERS_<SERVICE>` runs a service as several processes (it needs the
  shared cache on). On 8 vCPU / 16 GB this served about 600 active users with
  sub-second pages.
- Encrypted backup sets with a per-install age key, a proven restore, and a
  warm standby.
- PgBouncer connection pooling (new installs), Valkey as a shared cache, Kafka
  in place of Redpanda (the event bus stays off by default), OpenBao on raft
  storage (migrated in place, old volume kept).
- Operator documentation: `docs/operator/` and the Operator Guide PDF.

## 3.1.2 — The Update That Stopped Halfway
Patch release, **contains a security fix**. Fixes four faults that could stop an
update partway (withdrawn MinIO images, a stopped update that looked finished,
a Studio update button with no agent, a disk-reclaim step that ended the
update). Nothing removed.

## 3.1.0 — The Tenant Boundary, The Rep Desk, and Legal Holds
Minor release. Cross-tenant boundaries tightened, the rep desk, and legal holds
(with two-person release).

## 3.0.0 — The Rule, The Evaluator, and The Log
Major release. Authorisation as data (one evaluator, one rule), and its audit
log.

## 2.5.1 — Bring Your Own Calendar
Patch release. *Released 2026-08-27.*

## 2.5.0 — Four Services, Three Databases, and a Platform That Says No
Minor release. *Released 2026-08-26.*

## 2.4.0 — Enterprise Identity, Omnichannel & Authorization You Can Prove
Minor release. *Released 2026-08-19.*

## 2.3.0 — Compliance, Evidence & Determinism
Minor release. *Released 2026-08-04.*

## 2.2.0 — Risk Intelligence & Voice
Minor release. *Released 2026-07-28.*

## 2.1.0 — HxMeet, AI Operator & Process Intelligence
Minor release. *Released 2026-07-19.*

## 2.0.0 — Marketplace, AI Testing & Security Hardening
Major release. *Released 2026-06-17.*

## 1.0.0 — Initial release
*Released 2026-06-04.*
