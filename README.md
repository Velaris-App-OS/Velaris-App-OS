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
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-06B6D4?logo=tailwindcss&logoColor=white)
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
![MinIO](https://img.shields.io/badge/MinIO-C72E49?logo=minio&logoColor=white)

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

## What's new in 2.1.0

- **HxMeet** — real-time case sessions: embedded video/screen-share (self-hosted LiveKit) or Teams/Zoom/Meet via connectors; single-use guest invites; consent-gated **sealed recordings and live transcripts** (tenant-key encrypted, audit-chain anchored, verifiable); GPU live captions (Whisper, auto-detected CUDA/Vulkan); post-session AI summaries; document-first KYC verification.
- **HxNexus case Q&A** — ask anything about a case (variables, timeline, messages, documents, transcripts) with cited answers. Sovereignty-first: local models by default, external providers only by explicit tenant consent (pseudonymized + audited).
- **HxNexus Operator (MCP)** — a governed MCP tool surface so external AI agents can read (and, opt-in, write) platform state with scoped tokens and human-confirm proposals.
- **Marketplace** — install apps and connector packages (official / verified / community tiers, out-of-process execution, capability grants).
- **Portal v2** — customer portal rework: offline-capable PWA with queued submissions, worker↔customer messaging, CSAT, customer workflow steps.
- **HxReplay** — counterfactual case replay with cohort what-ifs and case costing (rate cards, timers, billing export).
- **HxEvolve** — process-drift detection that mines deviations and proposes replay-proven, human-approved process changes.
- **HxDBMigrate** — migrate legacy databases: schema analysis, case-type generation, polling sync, one-click cutover with rollback window, signed compliance certificate.
- **HxDraft** — draft-first configuration changes with diff cards and human merge.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Studio (Vite + React)                          :5173           │
│  Case designer · HxGraph visualiser · HxNexus AI · Marketplace  │
│  Test Suite · portals · analytics · HxDeploy · HxWork · HxMeet  │
└───────────────────────────┬─────────────────────────────────────┘
                            │  HTTPS
┌───────────────────────────▼─────────────────────────────────────┐
│  API Gateway (nginx)                            :8200           │
│  The single public door — routes /api/v1/* to case-service      │
└───────────────────────────┬─────────────────────────────────────┘
                            │  loopback
┌───────────────────────────▼─────────────────────────────────────┐
│  Case Service (FastAPI + SQLAlchemy)            :8201           │
│  ├── Cases, stages, steps, assignments, SLA, queues             │
│  ├── Auth (JWT RS256, bcrypt, MFA, passkeys, RBAC + ABAC, SSO)  │
│  ├── HxNexus (multi-provider AI, case Q&A, HxDraft, MCP)        │
│  ├── HxMeet (embedded video, live captions, sealed              │
│  │           recordings & transcripts, document-first KYC)      │
│  ├── HxGraph (in-process knowledge graph)                       │
│  ├── HxVault (per-tenant encryption, crypto-shredding)          │
│  ├── HxDbManager (guarded DB operations)                        │
│  ├── Marketplace + Test Suite / HxTest                          │
│  ├── Customer Accounts + Portal v2 (offline PWA, messaging)     │
│  ├── HxDeploy · HxWork · HxStream · HxShield                    │
│  ├── HxReplay · HxEvolve · HxDBMigrate                          │
│  ├── HxFusion / HxConnect (integrations)                        │
│  ├── HxAnalytics / HxSync / HxGlobal                            │
│  └── Customer portal + multi-tenant + compliance                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Flow Engine (FastAPI + Temporal)               :8100          │
│  └── BPMN 2.0 process orchestration + durable workers          │
└───────────────────────────┬────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Infrastructure (Docker Compose)                               │
│  ├── PostgreSQL 16 (+ pgvector)  :5432                         │
│  ├── Temporal                    :7233  (UI :8233)             │
│  ├── Valkey                      :6379                         │
│  ├── MinIO                       :9000  (console :9001)        │
│  ├── OpenBao (secrets manager)                                 │
│  ├── Ollama (local AI backend)   :11434                        │
│  ├── LiveKit SFU + egress (HxMeet video/recording)  :7880      │
│  └── Mailpit (SMTP dev)          :1025  (UI :8025)             │
└────────────────────────────────────────────────────────────────┘
```

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Shadcn/ui, ReactFlow, D3.js |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2, Alembic |
| API gateway | nginx — single `/api/v1` entrypoint; case-service runs loopback-only behind it |
| Process engine | Temporal, BPMN 2.0 |
| Database | PostgreSQL 16 (+ pgvector) |
| Cache / realtime | Valkey 8 (Redis-compatible) |
| Object storage | MinIO (S3-compatible) |
| AI (HxNexus) | Ollama (local default), Anthropic Claude, OpenAI, Google Gemini, Groq, Mistral, DeepSeek, Azure — or any OpenAI-compatible endpoint. Sovereignty-first: per-tenant `local_only` (any open-weight model) vs consented external, pseudonymized + audited |
| Real-time sessions (HxMeet) | Self-hosted LiveKit SFU + egress; Whisper live captions (auto-detected GPU: CUDA / Vulkan); recordings & transcripts sealed to the case (tenant DEK + audit-chain anchor) |
| Auth | JWT RS256 (RSA-2048), bcrypt, TOTP MFA, WebAuthn passkeys, RBAC + ABAC, SSO (SAML / OIDC) |
| Secrets | OpenBao — envelope-encrypted, rendered into the app at startup |
| Encryption at rest | HxVault per-tenant DEK (AES-256-GCM, crypto-shredding) |
| Package management | uv (Python), npm workspaces (Node) |
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
| MinIO console | http://localhost:9001 |
| Mailpit (dev email) | http://localhost:8025 |

### 4. Update

```bash
./update-velaris.sh
```

Pulls the channel-pinned release, applies any new migrations, and restarts the stack.

### 5. Stop services

```bash
./stop-velaris.sh          # stops Python services + Studio
./stop-velaris.sh --all    # also tears down Docker (DB, Temporal, etc.)
```

> Stop scripts require your system password. This prevents accidental or unauthorised shutdowns.

### 6. Uninstall

```bash
./uninstall-velaris.sh              # interactive — removes containers, volumes, venv, secrets
./uninstall-velaris.sh --purge      # also remove Docker images and uv
./uninstall-velaris.sh --delete-dir # also remove the project directory (irreversible)
```

---

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
├── case-service/          # Active monolith — cases, auth, AI, and all modules
│   ├── case_service/
│   │   ├── api/routers/   # 40+ FastAPI routers
│   │   ├── auth/          # JWT (RS256), RBAC, ABAC, MFA, passkeys
│   │   ├── db/            # SQLAlchemy models + repository
│   │   ├── hxnexus/       # AI assistant (multi-provider)
│   │   ├── hxgraph/       # Knowledge graph
│   │   ├── hxvault/       # Per-tenant encryption (envelope / crypto-shred)
│   │   ├── marketplace/   # App store + trust model + conformance gate
│   │   ├── testsuite/     # Core deterministic test engine
│   │   ├── hxtest/        # AI test generation (marketplace add-on)
│   │   ├── hxdeploy/      # Promotion governance
│   │   ├── middleware/    # Auth, rate limiting, audit, CORS
│   │   └── temporal/      # Workflow activities + workers
│   └── tests/             # Extensive phase_* test suite
├── ai-service/  analytics-service/  audit-service/  connect-service/
├── form-service/  notification-service/  rules-service/
├── user-service/  vcs-service/        # modular-split targets the gateway is pre-wired for
└── …

engine/                    # BPMN process engine (Temporal-backed)
studio/                    # React frontend (Vite)
migrations/                # ordered SQL migrations (000–124, postgresql/ + mysql/)
libs/                      # helix-ir (schema) + helix-sdk
plugins/                   # auth, cache, db, events, git, integrations, search
load-tests/                # Locust load test suite (4 user classes)
deploy/
├── docker-compose/        # Local dev compose stack (incl. nginx gateway + OpenBao)
├── helm/                  # Kubernetes Helm charts
└── openbao/               # Secrets-manager config
```

> The **API gateway** (nginx) is the only externally exposed door on `:8200`; it routes `/api/v1/*` to the case-service monolith on loopback `:8201`. The gateway's upstreams are pre-wired so analytics, HxGraph, and HxMigrate can split into their own services later with no client change.

---

## Database migrations

Migrations run automatically on `setup-velaris.sh` / `start-velaris.sh`. To inspect manually:

```bash
# List migrations
find migrations/ -name "*.sql" | sort

# Apply a specific migration
PGPASSWORD=<pw> psql -h localhost -U helix -d helix -f migrations/postgresql/124_marketplace_containers.sql
```

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
