# Velaris

**Enterprise case management and process automation platform.**

Velaris is a production-grade BPM and case management platform built for operations teams that need structured workflows, real-time case tracking, AI-assisted triage, and deep integration capabilities — without SaaS lock-in.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Studio (Vite + React)           :5173                      │
│  ├── Case Designer / Stage Builder                          │
│  ├── Form Builder                                           │
│  ├── Work Center                                            │
│  ├── HxGraph Visualiser (D3.js force graph)                 │
│  ├── HxNexus AI Assistant                                   │
│  ├── HxWork (tasks / stories / sprints)                     │
│  ├── HxBranch (config version control)                      │
│  ├── HxCanvas (freeform case whiteboard)                    │
│  ├── HxDocs (in-platform documentation)                     │
│  ├── HxMigrate (data migration)                             │
│  ├── HxDeploy (CI/CD governance)                            │
│  ├── HxConnect (integrations hub)                           │
│  ├── HxShield (security)                                    │
│  ├── Portal Admin                                           │
│  └── Analytics, Compliance, Observability                   │
└────────────────────────┬────────────────────────────────────┘
                         │ REST / WebSocket
┌────────────────────────▼────────────────────────────────────┐
│  Case Service (FastAPI + SQLAlchemy)   :8200                │
│  ├── Cases, stages, steps, assignments                      │
│  ├── Auth (JWT, bcrypt, MFA, RBAC, ABAC, SSO)               │
│  ├── HxNexus (Ollama-backed AI)                             │
│  ├── HxGraph (live knowledge graph)                         │
│  ├── HxBridge (connector execution engine)                  │
│  ├── HxShield (security audit + breach detection)           │
│  ├── HxDeploy (promotion governance)                        │
│  ├── HxWork (tasks + sprints)                               │
│  ├── HxStream (event tracing)                               │
│  ├── HxFusion (cross-case automation)                       │
│  ├── HxConnect (payments, KYC, e-sign, CRM, SMS, OCR)       │
│  ├── HxAnalytics / HxSync / HxGlobal                        │
│  ├── HxMigrate / HxBranch / HxDocs / HxCanvas               │
│  ├── Customer Portal + multi-tenant                         │
│  └── SLA engine, queues, notifications, compliance          │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Engine (FastAPI + Temporal)           :8100                │
│  └── BPMN 2.0 process orchestration                         │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Infrastructure (Docker Compose)                            │
│  ├── PostgreSQL 16          :5432                           │
│  ├── Temporal               :7233  (UI :8233)               │
│  ├── Redis                  :6379                           │
│  ├── Redpanda (Kafka)       :9092                           │
│  ├── OpenSearch             :9200                           │
│  ├── MinIO                  :9000  (console :9001)          │
│  └── Mailpit (SMTP dev)     :1025  (UI :8025)               │
└─────────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Shadcn/ui, ReactFlow, D3.js |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2 |
| Process engine | Temporal, BPMN 2.0 |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Event streaming | Redpanda (Kafka-compatible) |
| Full-text search | OpenSearch 2.17 |
| Object storage | MinIO (S3-compatible) |
| AI | Ollama (local LLMs), Anthropic Claude API |
| Auth | JWT (HS256), bcrypt, TOTP MFA, RBAC + ABAC, SSO |
| Package management | uv (Python), npm (Node) |

---

## Prerequisites

| Requirement | Min version | Notes |
|-------------|-------------|-------|
| Ubuntu / Debian | 22.04+ | Other distros work, untested |
| Docker + Compose | 24+ | `docker compose` v2 plugin |
| uv | 0.4+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20+ | LTS recommended |
| Python | 3.12 | Managed by uv — no manual install needed |
| Ollama | any | Optional — enables HxNexus AI features |

---

## Quick Start

### 1. First-time setup (run once)

```bash
chmod +x setup-velaris.sh
./setup-velaris.sh
```

This script:
- Installs all Python dependencies via `uv sync`
- Installs Studio npm dependencies
- Pulls required Docker images
- Runs all 80 database migrations
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
| Case Service API | http://localhost:8200/docs |
| Engine API | http://localhost:8100/docs |
| Temporal UI | http://localhost:8233 |
| MinIO console | http://localhost:9001 |
| Mailpit (dev email) | http://localhost:8025 |

### 4. Stop services

```bash
./stop-velaris.sh          # stops Python services + Studio
./stop-velaris.sh --all    # also tears down Docker (DB, Temporal, etc.)
```

> Stop and start scripts require your system password to prevent accidental or unauthorised shutdowns.

---

## Update

```bash
./update-velaris.sh
```

Automatically: checks for new version → backs up database → pulls latest code → runs new migrations → restarts services.

---

## Service Map

```
services/
├── case-service/          # Core platform — cases, auth, AI, all modules
│   └── case_service/
│       ├── api/routers/   # 40+ FastAPI routers
│       ├── auth/          # JWT, RBAC, ABAC, MFA, SSO
│       ├── db/            # SQLAlchemy models + repository
│       ├── hxnexus/       # AI assistant (Ollama)
│       ├── hxgraph/       # Live knowledge graph
│       ├── hxbridge/      # Connector execution engine
│       ├── hxshield/      # Security audit + breach detection
│       ├── hxdeploy/      # CI/CD promotion governance
│       ├── hxwork/        # Tasks + stories + sprints
│       ├── hxstream/      # Real-time event tracing
│       └── middleware/    # Auth, rate limiting, audit, CORS
├── ai-service/
├── analytics-service/
├── audit-service/
├── connect-service/
├── form-service/
├── notification-service/
├── rules-service/
├── user-service/
└── vcs-service/

engine/                    # BPMN process engine (Temporal-backed)
studio/                    # React frontend (Vite)
migrations/                # 80 ordered SQL migrations
releases/
├── manifest.sql           # Feature flag activation (auto-applied on start)
└── notes/                 # Release notes per version (vX.Y.Z.md)
libs/                      # helix-ir (schema) + helix-sdk
plugins/                   # auth, cache, db, events, git, integrations, search
scripts/                   # Superadmin setup, key verification
deploy/
├── docker-compose/        # Local dev + production compose stack
├── helm/                  # Kubernetes Helm charts
└── cloudflare-tunnel.example.yml
```

---

## Database Migrations

Migrations run automatically on `./start-velaris.sh`. To apply manually:

```bash
# List all migrations
find migrations/ -name "*.sql" | sort

# Apply a specific migration
PGPASSWORD=<pw> psql -h localhost -U helix -d helix -f migrations/080_versioned_flags.sql
```

---

## API Reference

Both services expose interactive Swagger UIs:

- **Case Service** — http://localhost:8200/docs
- **Engine** — http://localhost:8100/docs

Key Case Service endpoint groups:

| Prefix | Description |
|--------|-------------|
| `/api/v1/cases` | Case CRUD, stage transitions, locking |
| `/api/v1/case-types` | Case type designer |
| `/api/v1/auth` | Login, MFA, SSO, token refresh, user management |
| `/api/v1/portal` | Customer-facing portal |
| `/api/v1/analytics` | Dashboards, reporting, process mining |
| `/api/v1/graph` | HxGraph knowledge graph queries |
| `/api/v1/nexus` | HxNexus AI assistant |
| `/api/v1/forms` | Form definitions |
| `/api/v1/deploy` | HxDeploy promotion governance |
| `/api/v1/bridge` | HxBridge connector execution |
| `/api/v1/payments` | Stripe payments (HxConnect) |
| `/api/v1/identity` | KYC / identity verification (HxConnect) |
| `/api/v1/esign` | E-signatures via DocuSign (HxConnect) |
| `/api/v1/work` | HxWork tasks, stories, sprints |
| `/api/v1/migrate` | HxMigrate data migration jobs |
| `/api/v1/shield` | HxShield security events |
| `/api/v1/compliance` | Audit log, compliance reports |
| `/api/v1/my` | Personal assignments, workload, notifications |

---

## HxGraph

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

## Environment Variables

Key variables in `.env`:

| Variable | Description |
|----------|-------------|
| `VELARIS_DB_PASSWORD` | PostgreSQL password |
| `HELIX_CASE_AUTH_SECRET` | JWT signing secret |
| `HELIX_CASE_AI_PROVIDER` | AI provider: `ollama`, `openai`, `anthropic`, `groq` |
| `HELIX_CASE_AI_OLLAMA_URL` | Ollama endpoint (default: `http://localhost:11434`) |
| `VELARIS_REGISTER_URL` | Product key registration server |

Generate secrets:

```bash
openssl rand -hex 32   # for HELIX_CASE_AUTH_SECRET
```

---

## Releases

Release notes live in `releases/notes/`. Each version has its own file:

```
releases/notes/
├── README.md       # versioning guide + release checklist
└── v1.0.0.md       # initial release
```

---

## Licence

| Component | Licence |
|-----------|---------|
| Core platform (`services/`, `engine/`, `studio/`) | Business Source Licence 1.1 → Apache 2.0 after 4 years |
| SDK + IR schema (`libs/`) | Apache Licence 2.0 |
| Plugins (`plugins/`) | Apache Licence 2.0 |

**BSL 1.1 summary:** You can use, run, and customise Velaris for any internal business purpose. You may sell services, plugins, and applications built on the platform. You may not resell the platform itself, sublicense it, or offer it as a competing SaaS product. See `LICENSE` for full terms.

For commercial licensing or enterprise agreements: **velaris.app.os@gmail.com**
