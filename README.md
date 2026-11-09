<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d9488,100:4ecdc4&height=120&section=header" width="100%" alt="" />

<img src="studio/public/velaris.png" width="140" alt="Velaris" />

<h1>Velaris</h1>

<h3><i>Your infra. Your choice. Zero SaaS lock-in.</i></h3>

<a href="https://github.com/Velaris-App-OS/Velaris-App-OS">
  <img src="https://readme-typing-svg.demolab.com?font=DM+Sans&weight=600&size=22&pause=900&color=0D9488&center=true&vCenter=true&width=780&lines=Structured+workflows+%26+real-time+case+tracking;AI-assisted+triage+with+HxNexus;A+live+semantic+knowledge+graph+%E2%80%94+HxGraph;Per-tenant+encryption+%26+secrets+via+OpenBao" alt="Velaris" />
</a>

<p>
  <img src="https://img.shields.io/badge/License-BSL%201.1%20%E2%86%92%20Apache%202.0-0d9488?style=for-the-badge" alt="License" />
  <img src="https://img.shields.io/badge/Deploy-Self--hosted-4ecdc4?style=for-the-badge" alt="Self-hosted" />
  <img src="https://img.shields.io/badge/Use-Free-26de81?style=for-the-badge" alt="Free to use" />
</p>

</div>

<h3 align="center">Tech Stack</h3>

<div align="center">

![Ollama](https://img.shields.io/badge/Ollama-000000?logo=ollama&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-D97757?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?logo=openai&logoColor=white)
![Gemini](https://img.shields.io/badge/Google_Gemini-8E75B2?logo=googlegemini&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-F55036?logoColor=white)
![Mistral](https://img.shields.io/badge/Mistral-FA520F?logo=mistralai&logoColor=white)
![DeepSeek](https://img.shields.io/badge/DeepSeek-4D6BFE?logo=deepseek&logoColor=white)

![React](https://img.shields.io/badge/React-20232A?logo=react&logoColor=61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-646CFF?logo=vite&logoColor=white)
![D3.js](https://img.shields.io/badge/D3.js-F9A03C?logo=d3dotjs&logoColor=white)

![Python](https://img.shields.io/badge/Python_3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-D71F00?logo=sqlalchemy&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-E92063?logo=pydantic&logoColor=white)

![Temporal](https://img.shields.io/badge/Temporal-000000?logo=temporal&logoColor=white)
![BPMN 2.0](https://img.shields.io/badge/BPMN-2.0-FF6F00)

![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-4169E1?logo=postgresql&logoColor=white)
![Valkey](https://img.shields.io/badge/Valkey-2A6DB2?logo=valkey&logoColor=white)
![PgBouncer](https://img.shields.io/badge/PgBouncer-336791?logo=postgresql&logoColor=white)
![Apache Kafka](https://img.shields.io/badge/Apache_Kafka-231F20?logo=apachekafka&logoColor=white)

![OpenBao](https://img.shields.io/badge/OpenBao-secrets-FFCF25?logo=openbao&logoColor=black)
![JWT](https://img.shields.io/badge/JWT_RS256-000000?logo=jsonwebtokens&logoColor=white)
![WebAuthn](https://img.shields.io/badge/WebAuthn_Passkeys-3423A6?logo=webauthn&logoColor=white)

![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![nginx](https://img.shields.io/badge/nginx-009639?logo=nginx&logoColor=white)
![uv](https://img.shields.io/badge/uv-DE5FE9?logo=python&logoColor=white)
![npm](https://img.shields.io/badge/npm-CB3837?logo=npm&logoColor=white)
![Locust](https://img.shields.io/badge/Locust-2EAE4E?logo=locust&logoColor=white)

</div>

<p align="center">
  <b>Enterprise case management and process automation platform.</b><br/>
  Velaris is a production-grade BPM and case management platform built for operations teams that need
  structured workflows, real-time case tracking, AI-assisted triage, and deep integration capabilities
  &mdash; without SaaS lock-in.
</p>

---

## What's new in 3.2.0

- **Services of its own** — sign-in and identity (user-service), the customer portal (portal-service), checkout, payments and the marketplace (commerce-service) and the background workers (worker-service) now run outside case-service, beside analytics, HxGraph and HxMigrate. Every client URL is unchanged: the gateway is still the only door.
- **Infrastructure for production** — a PgBouncer connection pooler, a shared Valkey cache, Apache Kafka for the optional event bus (Redpanda is gone), and OpenBao on raft storage, migrated in place on the update with the old data kept as the way back.
- **Backup, restore and recovery** — encrypted backups (each install gets its own age key), a restore that is drilled, and a warm standby.
- **Form Builder, redesigned** — sections, conditions, connector lookups, translations, drafts and field-level security, with every value a form stores checked against the form.
- **Connectors you can name and point at your data** — saved connector operations, and a database query connector: saved, read-only SELECTs on your own PostgreSQL, MySQL or MariaDB database, with bind values only, a row cap, a time limit and pinned, verified TLS.
- **The tenant boundary, tightened** — each website demo sign-up gets its own tenant; creating and completing case work needs an access group; a tenant's connectors and database sources reach the internet only; the install's audit chain and platform screens are the operator's.
- **What you notice on upgrade** — a user in no access group can no longer create or complete cases until an administrator assigns one, and portal customers sign in again once. Nothing is removed or renamed.

Full notes: [`releases/notes/v3.2.0.md`](releases/notes/v3.2.0.md). Earlier releases: [`CHANGELOG.md`](CHANGELOG.md).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Studio (Vite + React)                          :5173           │
│  Case designer · HxGraph visualiser · HxNexus AI · Marketplace  │
│  Form Builder · portals · analytics · HxDeploy · HxWork · HxMeet│
└───────────────────────────┬─────────────────────────────────────┘
                            │  HTTPS
┌───────────────────────────▼─────────────────────────────────────┐
│  API Gateway (nginx)                            :8200           │
│  The single public door — routes each /api/v1 prefix to its     │
│  service; services listen on loopback and refuse other callers  │
└───────────────────────────┬─────────────────────────────────────┘
                            │  loopback (internal mTLS optional)
┌───────────────────────────▼─────────────────────────────────────┐
│  case-service        :8201  cases, stages, steps, SLA, queues,  │
│                             forms, HxNexus, HxMeet, HxBridge    │
│                             connectors, HxVault, compliance,    │
│                             HxDeploy, HxWork, HxStream, HxShield│
│  analytics-service   :8202  analytics + process mining          │
│  hxgraph-service     :8203  knowledge graph                     │
│  hxmigrate-service   :8204  scan, import, plan, HxDBMigrate     │
│  portal-service      :8205  customer portal + accounts          │
│  user-service        :8206  sign-in, MFA, SSO, SAML, SCIM       │
│  commerce-service    :8207  checkout, payments, marketplace     │
│  worker-service      :8208  background jobs (health port only)  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Flow Engine (FastAPI + Temporal)               :8100          │
│  └── BPMN 2.0 process orchestration + durable workers          │
└───────────────────────────┬────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Infrastructure (Docker Compose)                               │
│  ├── PostgreSQL 16 (+ pgvector)  :5432  (MySQL/MariaDB too)    │
│  ├── PgBouncer (connection pooler)                             │
│  ├── Temporal                    :7233  (UI :8233)             │
│  ├── Valkey (shared cache)       :6379                         │
│  ├── Apache Kafka (event bus — optional, off by default)       │
│  ├── OpenBao (secrets manager, raft storage)                   │
│  ├── Documents: local disk (S3 endpoint optional)              │
│  ├── Ollama (local AI backend)   :11434                        │
│  ├── LiveKit SFU + egress (HxMeet video/recording)  :7880      │
│  ├── Prometheus + OpenTelemetry collector (optional)           │
│  ├── App egress + broker gateways (marketplace apps)           │
│  └── Mailpit (SMTP dev)          :1025  (UI :8025)             │
└────────────────────────────────────────────────────────────────┘
```

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, TypeScript, Vite, ReactFlow, D3.js |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2; ordered SQL migrations (Alembic in the flow engine only) |
| API gateway | nginx — single `/api/v1` entrypoint; every service runs loopback-only behind it |
| Process engine | Temporal, BPMN 2.0 |
| Database | PostgreSQL 16 (+ pgvector), MySQL or MariaDB; PgBouncer connection pooler |
| Cache / realtime | Valkey 8 (Redis-compatible), shared across processes |
| Event bus | Apache Kafka 4 (optional, off by default; SASL) |
| Document storage | Local disk by default, encrypted at rest; any S3-compatible endpoint optional |
| AI (HxNexus) | Ollama (local default), Anthropic Claude, OpenAI, Google Gemini, Groq, Mistral, DeepSeek, Azure — or any OpenAI-compatible endpoint. Sovereignty-first: per-tenant `local_only` (any open-weight model) vs consented external, pseudonymized + audited |
| Real-time sessions (HxMeet) | Self-hosted LiveKit SFU + egress; Whisper live captions (auto-detected GPU: CUDA / Vulkan); recordings & transcripts sealed to the case (tenant DEK + audit-chain anchor) |
| Auth | JWT RS256 (RSA-2048), bcrypt, TOTP MFA, WebAuthn passkeys, RBAC + ABAC, SSO (SAML / OIDC) |
| Secrets | OpenBao (raft storage) — envelope-encrypted, rendered into the app at startup |
| Encryption at rest | HxVault per-tenant DEK (AES-256-GCM, crypto-shredding) |
| Package management | uv (Python), npm workspaces (Node) |
| Observability | Prometheus, OpenTelemetry collector, per-tenant log/metric views |
| Load testing | Locust |

---

## Prerequisites

| Requirement | Min version | Notes |
|-------------|-------------|-------|
| Ubuntu / Debian | 22.04+ | Other distros work, untested |
| Docker + Compose | 24+ | `docker compose` v2 plugin |
| uv | 0.4+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20+ | LTS recommended |
| npm | 10+ | Ships with Node |
| Python | 3.12 | Managed by uv — no manual install needed |
| Ollama | any | Optional — enables local HxNexus AI features |

---

## Quick start

### 1. First-time setup (run once)

```bash
chmod +x setup-velaris.sh
./setup-velaris.sh
```

This script:
- Installs all Python dependencies into `.venv/` via `uv sync`
- Installs Studio npm dependencies
- Pulls required Docker images
- Runs all database migrations
- Generates infrastructure secrets (managed by OpenBao)
- Validates your Velaris product key
- Creates the superadmin account interactively

### 2. Start all services

```bash
./start-velaris.sh
```

### 3. Open the platform

| Interface | URL |
|-----------|-----|
| Studio (main UI) | http://localhost:5173 |
| API + docs (via gateway) | http://localhost:8200/docs |
| Engine API + docs | http://localhost:8100/docs |
| Temporal UI | http://localhost:8233 |
| Mailpit (dev email) | http://localhost:8025 |

### 4. Update

```bash
./update-velaris.sh
```

Pulls the channel-pinned release, applies any new migrations, and restarts the stack. It takes a
database backup first and aborts if the dump fails.

On an install run by systemd (`deploy/systemd/`), the update finishes with
`deploy/systemd/refresh-after-update.sh`: it re-applies the release manifest, rebuilds Studio and
restarts the units.

### 5. Back up

```bash
./backup-velaris.sh                    # dump + record it in the HxDR posture
./backup-velaris.sh --keep 30 --quiet  # cron-friendly, prunes dumps older than 30 days
```

Takes the dump **and records it**, in one command. A bare `pg_dump` leaves no record, so the
recovery posture reports *no backup* while backups are in fact being taken — wrong in the direction
that teaches people to ignore the screen. Exits non-zero only if the backup itself failed.

### 6. Stop services

```bash
./stop-velaris.sh          # stops Python services + Studio
./stop-velaris.sh --all    # also tears down Docker (DB, Temporal, etc.)
```

> Stop scripts require your system password. This prevents accidental or unauthorised shutdowns.

### 7. Uninstall

```bash
./uninstall-velaris.sh              # interactive — removes containers, volumes, venv, secrets
./uninstall-velaris.sh --purge      # also remove Docker images and uv
./uninstall-velaris.sh --delete-dir # also remove the project directory (irreversible)
```

---

## Kubernetes (optional)

The quick start above is the standard installation and is unaffected by this
section. If you already run a cluster, Velaris installs on Kubernetes with the
Helm chart in `deploy/helm/velaris` and the scripts in `K8/` — one directory per
database engine:

```bash
./K8/Postgres/setup-velaris-k8s.sh --build   # or K8/MySQL, or K8/MariaDB
./K8/Postgres/update-velaris-k8s.sh
./K8/Postgres/stop-velaris-k8s.sh            # --start | --uninstall | --purge
```

Already running Velaris and moving into a cluster? Point the install at the
database you have and the bundled one is not deployed:

```bash
VELARIS_K8S_EXTERNAL_URL="postgresql+asyncpg://user:pass@host:5432/velaris" \
  ./K8/Postgres/setup-velaris-k8s.sh
```

The target is checked before anything is written: an empty database is
accepted, an existing Velaris database is adopted and only missing migrations
applied, and another application's database is refused.

Each directory has a README covering requirements, resource sizing, the
security posture, and measured load and failure behaviour. Read
`K8/Postgres/README.md` before exposing an install — the chart ships with no
ingress and no TLS by default, on purpose.

## Marketplace — build &amp; use apps

The Velaris Marketplace is an in-app store for extending the platform — connectors, case-type templates, dashboards, prompt packs, portal themes, and full Studio modules — packaged as content-addressed `.hxapp` bundles.

**Using apps.** Browse the catalogue in Studio, review a package's README, screenshots, and declared outbound domains, then install it per tenant in one click. Every install verifies the bundle checksum and validates the manifest first, and you can exercise any app against disposable data in an isolated **sandbox** before it touches production. Uninstall either revokes access (keeping data) or revokes and deletes the app's data.

**Creating apps.** Package your artifacts as an `.hxapp` with a manifest (id, semver `version`, `type`, and the exact `outbound_domains` it is allowed to call). There are two publishing lanes:

- **Official** — published by the Velaris team; a package earns the Official badge only when the source org, the `official/` folder, **and** the built-in registry all agree, so a package can never self-declare its own tier.
- **Community** — open to anyone, via fork + pull request to the marketplace repository.

Either way, a package must pass the **Test Suite conformance gate** — deterministic structural checks (valid manifest, resolvable stage transitions, typed form fields, an acyclic rules graph, no hardcoded tenant/user ids) — before it can be submitted. Third-party code runs **out-of-process only**: installed packages extend Velaris through Studio artifacts and out-of-process connectors, never in-process platform code.

> Marketplace repo: `Velaris-App-OS/Marketplace` (`official/` + `community/` folders). Manage everything from the in-app **Marketplace**; see the docs for publishing details.

---

## Service map

```
services/
├── _shared/velaris_shared/  # code every service shares — config, DB models, auth, HxGuard, HxBridge rules
├── case-service/            # cases, forms, AI, connectors, compliance, and the worker entry point
│   ├── case_service/
│   │   ├── api/routers/     # FastAPI routers
│   │   ├── compliance/      # HxConsent, HxPrivacy, HxLedger, HxDR, retention, audit chain
│   │   ├── hxbridge/        # connectors, executor, dead-letter queue
│   │   ├── hxnexus/         # AI assistant (multi-provider)
│   │   ├── hxvault/         # per-tenant encryption (envelope / crypto-shred)
│   │   ├── hxdbmigrate/     # legacy-database sources
│   │   ├── marketplace/     # app store + trust model + conformance gate
│   │   ├── testsuite/  hxtest/  hxdeploy/
│   │   ├── middleware/      # auth, rate limiting, audit, CORS
│   │   ├── worker/          # worker-service (background jobs)
│   │   └── temporal/        # workflow activities + workers
│   └── tests/
├── analytics-service/  hxgraph-service/  hxmigrate-service/
├── portal-service/  user-service/  commerce-service/
└── ai-, audit-, connect-, form-, notification-, rules-, vcs-service/   # not started; reserved

engine/                    # BPMN process engine (Temporal-backed)
studio/                    # React frontend (Vite)
migrations/                # ordered SQL migrations (postgresql/ 001–231, mysql/ frozen baseline + 155→)
libs/                      # velaris-ir (schema) + velaris-sdk + hxarbiter-core
plugins/                   # ai, auth, cache, channels, db, events, git, integrations, search
load-tests/                # Locust load test suite
K8/                        # Kubernetes scripts, one directory per database engine
deploy/
├── docker-compose/        # compose stack (gateway, pooler, OpenBao, Kafka, observability)
├── helm/                  # Kubernetes Helm chart
├── systemd/               # unit files, one per service
├── tls/                   # internal CA + mTLS scripts
└── openbao/               # secrets-manager config
```

> The **API gateway** (nginx) is the only externally exposed door on `:8200`; it routes each `/api/v1/*` prefix to its service on loopback. A service refuses a caller that did not come through the gateway.

---

## Database migrations

Migrations run automatically on `setup-velaris.sh` / `start-velaris.sh`: each file is applied in
filename order and recorded in a `schema_migrations` ledger, so an already-applied file is skipped.
A failing migration stops startup — the platform never boots on a half-applied schema.

```bash
# List migrations (the runner only sees maxdepth 1)
find migrations/ -maxdepth 2 -name "*.sql" | sort

# Apply a specific migration
PGPASSWORD=<pw> psql -h localhost -U velaris -d velaris \
  -f migrations/postgresql/155_dr_measured_rto.sql
```

**PostgreSQL** carries the full incremental history (`001` → `231`). **MySQL/MariaDB** starts from a
**frozen** baseline — `000_baseline_pg154.sql`, a snapshot of PostgreSQL `001`–`154` — followed by
the seed files and then one file per PostgreSQL migration from `155` on, sharing its sibling's
number. The gap between `003` and `155` is intentional: the baseline already contains it.

> **Never regenerate the MySQL baseline to ship a schema change.** On an existing install the
> baseline is already in `schema_migrations`, so a regenerated one is silently skipped and the
> change reaches nobody. Add a numbered file instead. `migrations/mysql/historical/README.md`
> has the full story.

---

## API reference

The platform API is served through the gateway with interactive Swagger UIs:

- **API (via gateway)** — http://localhost:8200/docs (full platform API)
- **Engine** — http://localhost:8100/docs (BPMN process API)

Key endpoint groups:

| Prefix | Description |
|--------|-------------|
| `/api/v1/cases` | Case CRUD, stage transitions, locking |
| `/api/v1/case-types` | Case type designer |
| `/api/v1/auth` | Login, MFA, passkeys, token refresh, user management |
| `/api/v1/my` | Personal assignments, workload, notifications |
| `/api/v1/queues` | Work queues |
| `/api/v1/analytics` | Dashboard, reporting |
| `/api/v1/graph` | HxGraph knowledge graph queries |
| `/api/v1/nexus` | HxNexus AI assistant |
| `/api/v1/marketplace` | Marketplace — discover and install apps |
| `/api/v1/testsuite` | Test Suite — runs, history, conformance gate |
| `/api/v1/hxtest` | HxTest — AI test generation (install-gated add-on) |
| `/api/v1/hxdbmanager` | HxDB Manager — guarded database operations |
| `/api/v1/deploy` | HxDeploy promotion governance |
| `/api/v1/rollouts` | Staged rollouts of case type versions |
| `/api/v1/legal-holds` | Legal holds — place, release, what is frozen |
| `/api/v1/forms` | Form definitions |
| `/api/v1/portal` | Customer-facing portal + customer accounts |

---

## HxGraph (knowledge graph)

Velaris ships a live semantic knowledge graph at `/api/v1/graph`:

```bash
# Natural-language query
curl -s -X POST http://localhost:8200/api/v1/graph/query \
  -H "Content-Type: application/json" \
  -d '{"question":"which modules depend on auth?"}'

# Path between two concepts
curl -s "http://localhost:8200/api/v1/graph/path?from=case&to=stage"

# Interactive visualiser (D3.js)
open http://localhost:8200/api/v1/graph/visualize
```

---

## Load testing

```bash
# Install Locust (already in .venv)
uv run locust --version

# Headless smoke test — 40 users, 2 min
uv run locust -f load-tests/locustfile.py --host http://localhost:8200 \
    --headless -u 40 -r 2 --run-time 2m

# With HTML report
uv run locust -f load-tests/locustfile.py --host http://localhost:8200 \
    --headless -u 40 -r 2 --run-time 5m --html load-tests/report.html

# Interactive web UI (http://localhost:8089)
uv run locust -f load-tests/locustfile.py --host http://localhost:8200
```

Simulated user classes: `CaseWorkerUser` (60%), `ManagerUser` (20%), `DevOpsUser` (10%), `SpikeUser` (10%).

---

## Secrets & configuration

Velaris secrets are managed by **OpenBao** — `setup-velaris.sh` generates them and `start-velaris.sh` renders them into the app at startup (fail-closed: a problem leaves the last-known-good config untouched). There is no manual environment-variable wrangling; for local development an `.env` is created from `.env.example` automatically, and per-tenant data is encrypted at rest by **HxVault**.

---

## License

| Component | License |
|-----------|---------|
| Core platform (`services/`, `engine/`, `studio/`) | Business Source License 1.1 → Apache 2.0 after 4 years |
| SDK + IR schema (`libs/`) | Apache License 2.0 |
| Plugins (`plugins/`) | Apache License 2.0 |

**Free to use.** Run, customise, and build on Velaris for any internal business purpose, and sell services, plugins, and applications built on it. The single BSL 1.1 limit: you may not resell the platform itself, sublicense it, or offer it as a competing hosted/SaaS product. Each release's BSL grant converts to Apache 2.0 after four years. See `LICENSE` for the full terms.

---

## Star History

<a href="https://www.star-history.com/?repos=Velaris-App-OS%2FVelaris-App-OS&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&legend=top-left" />
 </picture>
</a>

<div align="center">
  <sub>Questions &middot; <a href="mailto:velaris.app.os@gmail.com">velaris.app.os@gmail.com</a></sub>
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:4ecdc4,100:0d9488&height=120&section=footer" width="100%" alt="" />
</div>
