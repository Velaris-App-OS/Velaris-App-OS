-- 198 — case statuses become data; case actions get one dispatcher.
--
-- Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md
--
-- WHY. `case_instances.status` is VARCHAR(30) with a default of 'new' and NO
-- validation anywhere: `POST /cases/{id}/status` accepts any string a caller
-- sends. Six statuses exist on this platform because six were used, not because
-- six were designed. Worse, "which of them ends a case" is a hardcoded tuple in
-- TWO places — `temporal/workflows/case_lifecycle_workflow.py` and
-- `temporal/activities/interpreter_activities.py` — so a new terminal status is
-- correct in the database and wrong in the workflow engine, with nothing saying
-- so.
--
-- THE SPLIT THIS MIGRATION ENCODES. A status is a LABEL: the customer's process
-- attaches the meaning, and there is no reason the platform should own the word
-- `awaiting_customer`. An action is BEHAVIOUR: `escalate` fires a tree and
-- notifies, and you cannot infer that from the word. So statuses live in a
-- table an operator may edit, and actions stay a closed registry in code with
-- only their PRESENTATION and REACH configurable here.
--
-- NOT A FOREIGN KEY. `case_instances.status` deliberately stays a plain
-- VARCHAR(30) rather than referencing `case_statuses(name)`. A per-tenant status
-- set has to remain editable after it has been used; an FK would make renaming
-- or removing a status impossible on exactly the installs that need it most.
-- Validation is at the write path, where it can produce a readable error naming
-- the legal transitions, instead of a constraint violation.
--
-- SAFE ON A LIVE DATABASE. Purely additive: two new tables and no change to any
-- existing column. Re-runnable — every CREATE is IF NOT EXISTS and every seed
-- row is ON CONFLICT DO NOTHING. The previous release's code does not read
-- either table, so it keeps behaving exactly as it does today while this
-- applies.
--
-- THE SEED IS THE CURRENT BEHAVIOUR. The platform-default rows (tenant_id NULL,
-- case_type_id NULL) mark `resolved`, `closed` and `cancelled` terminal — the
-- same three the two hardcoded tuples name today — so replacing those tuples
-- with a lookup changes no answer. The five new statuses ship enabled but
-- unused: nothing sets them until an operator or an action does.


-- ── statuses ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS case_statuses (
    id            UUID         PRIMARY KEY,

    -- NULL tenant = the platform default set, which is what a single-tenant
    -- install runs on and what every tenant falls back to. Resolution is most
    -- specific first: (tenant, case_type) > (tenant, NULL) > (NULL, NULL) —
    -- the same precedence retention policies and portal display config use.
    tenant_id     UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_type_id  UUID         NULL REFERENCES case_types(id) ON DELETE CASCADE,

    -- The value actually stored in `case_instances.status`. Kept to 30 chars
    -- because that column is VARCHAR(30) and a name that cannot be stored is
    -- not a status.
    name          VARCHAR(30)  NOT NULL,
    -- What an operator sees. Separate from `name` so a label can be renamed —
    -- or translated (A2 half 2) — without rewriting every stored row.
    label         VARCHAR(100) NOT NULL,

    -- open | working | waiting | terminal.
    --
    -- Code needs to reason about KINDS of status without knowing a customer's
    -- names: "is this being worked", "is it waiting on someone else". A rule or
    -- a report written against `category` keeps working when a tenant renames
    -- `pending` to `awaiting_customer`.
    category      VARCHAR(16)  NOT NULL DEFAULT 'open',

    -- Whether reaching this status ends the case. This stays a first-class
    -- boolean rather than being derived from `category = 'terminal'`, because
    -- deriving it is precisely the indirection that let the two hardcoded
    -- tuples drift apart from the data in the first place.
    is_terminal   BOOLEAN      NOT NULL DEFAULT FALSE,
    -- The status a newly created case takes. At most one per resolved set;
    -- enforced in code, not by a constraint, because the constraint would have
    -- to span the NULL-tenant fallback.
    is_default    BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Status names a case may arrive here FROM. An EMPTY list means "from any
    -- status" — that is the permissive default, and it is what every seeded row
    -- ships with, so this migration introduces no transition that used to work
    -- and now fails. Tightening the graph is an operator's decision, made later,
    -- per case type.
    allowed_from  JSONB        NOT NULL DEFAULT '[]'::jsonb,

    sort_order    INTEGER      NOT NULL DEFAULT 0,
    -- A disabled status is not offered for NEW transitions but is still
    -- recognised on rows that already hold it. Deleting a status a case is
    -- sitting on would make that case unreadable; disabling is the safe verb.
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,

    -- The scope this row belongs to, written by the application as
    -- `<tenant_id|platform>:<case_type_id|any>`. It exists because NULL <> NULL:
    -- a plain UNIQUE (tenant_id, case_type_id, name) would permit unlimited
    -- duplicate PLATFORM rows for `closed`, and then "is closed terminal"
    -- depends on which row was read first.
    --
    -- Written rather than GENERATED, and identical on all three engines:
    -- MariaDB 11.8 refuses COALESCE in a GENERATED ALWAYS AS clause (error
    -- 1901) where MySQL 8.4 accepts it — migration 177 met this. A CHECK keeps
    -- the column honest without calling a function anywhere.
    scope_key     VARCHAR(80)  NOT NULL,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_case_statuses_scope_key CHECK (
        scope_key = CONCAT(
            COALESCE(CAST(tenant_id    AS VARCHAR(36)), 'platform'), ':',
            COALESCE(CAST(case_type_id AS VARCHAR(36)), 'any'))
    ),
    CONSTRAINT ux_case_statuses_scope UNIQUE (scope_key, name)
);

CREATE INDEX IF NOT EXISTS idx_case_statuses_lookup
    ON case_statuses (tenant_id, case_type_id, enabled);


-- ── action configuration ────────────────────────────────────────────────────
--
-- What a customer may change about an action, and nothing more. The BEHAVIOUR
-- of each action is a closed registry in code: `escalate` fires an escalation
-- tree because the handler does, not because a row says so. A row naming an
-- action outside that registry is refused at WRITE time — ignoring it at read
-- time would leave an operator staring at a configuration that does nothing.

CREATE TABLE IF NOT EXISTS case_action_config (
    id            UUID         PRIMARY KEY,

    tenant_id     UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_type_id  UUID         NULL REFERENCES case_types(id) ON DELETE CASCADE,

    -- Must be a name in the code registry. Validated on write.
    action        VARCHAR(40)  NOT NULL,

    -- NULL = use the registry's own label.
    label         VARCHAR(100) NULL,
    -- FALSE hides the action from this scope entirely — the dispatcher refuses
    -- it, not just the UI. An action a customer has switched off must be
    -- unreachable by anyone who reads the API docs, or the switch is decoration.
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order    INTEGER      NOT NULL DEFAULT 0,

    -- Overrides the registry's default privilege. Must be a known HxGuard
    -- action; validated on write. This is how a tenant makes `close` require
    -- something stricter than `case.update` without a code change.
    privilege     VARCHAR(80)  NULL,

    -- Status names this action is offered FROM. Empty = from any status. This
    -- is the customer's half of the state machine: the platform says what
    -- `close` does, the customer says when it may be done.
    allowed_from  JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- Same shape and same reason as `case_statuses.scope_key` above.
    scope_key     VARCHAR(80)  NOT NULL,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_case_action_config_scope_key CHECK (
        scope_key = CONCAT(
            COALESCE(CAST(tenant_id    AS VARCHAR(36)), 'platform'), ':',
            COALESCE(CAST(case_type_id AS VARCHAR(36)), 'any'))
    ),
    CONSTRAINT ux_case_action_config_scope UNIQUE (scope_key, action)
);

CREATE INDEX IF NOT EXISTS idx_case_action_config_lookup
    ON case_action_config (tenant_id, case_type_id, enabled);


-- ── seed: the platform default status set ───────────────────────────────────
--
-- Six of these are in use on this platform today; five are new and unused until
-- something sets them. `allowed_from` is '[]' on every row — from any status —
-- so no transition that works today stops working. `is_terminal` matches the
-- hardcoded tuples exactly, which is what makes replacing them a no-op.

INSERT INTO case_statuses (id, tenant_id, case_type_id, scope_key, name, label, category, is_terminal, is_default, sort_order)
VALUES
    ('a1000000-0000-4000-8000-000000000001', NULL, NULL, 'platform:any', 'new',         'New',         'open',     FALSE, TRUE,  10),
    -- open vs in_progress: `open` is accepted but not yet being worked;
    -- `in_progress` is someone working it now. Two words for one state would be
    -- worse than one, so if that distinction is not wanted, disable
    -- `in_progress` rather than blurring `open`.
    ('a1000000-0000-4000-8000-000000000002', NULL, NULL, 'platform:any', 'open',        'Open',        'open',     FALSE, FALSE, 20),
    ('a1000000-0000-4000-8000-000000000003', NULL, NULL, 'platform:any', 'in_progress', 'In Progress', 'working',  FALSE, FALSE, 30),
    -- pending = waiting on someone OUTSIDE the team (a customer, a third party).
    ('a1000000-0000-4000-8000-000000000004', NULL, NULL, 'platform:any', 'pending',     'Pending',     'waiting',  FALSE, FALSE, 40),
    -- on_hold = deliberately paused BY US. Pairs with the SLA pause, which is
    -- why the two are different statuses and not one.
    ('a1000000-0000-4000-8000-000000000005', NULL, NULL, 'platform:any', 'on_hold',     'On Hold',     'waiting',  FALSE, FALSE, 50),
    -- A status, and also an event: `escalation.py` already escalates an SLA
    -- without touching case status, and that keeps working untouched.
    ('a1000000-0000-4000-8000-000000000006', NULL, NULL, 'platform:any', 'escalated',   'Escalated',   'working',  FALSE, FALSE, 60),
    ('a1000000-0000-4000-8000-000000000007', NULL, NULL, 'platform:any', 'resolved',    'Resolved',    'terminal', TRUE,  FALSE, 70),
    ('a1000000-0000-4000-8000-000000000008', NULL, NULL, 'platform:any', 'closed',      'Closed',      'terminal', TRUE,  FALSE, 80),
    ('a1000000-0000-4000-8000-000000000009', NULL, NULL, 'platform:any', 'reopened',    'Reopened',    'open',     FALSE, FALSE, 90),
    ('a1000000-0000-4000-8000-00000000000a', NULL, NULL, 'platform:any', 'cancelled',   'Cancelled',   'terminal', TRUE,  FALSE, 100),
    ('a1000000-0000-4000-8000-00000000000b', NULL, NULL, 'platform:any', 'archived',    'Archived',    'terminal', TRUE,  FALSE, 110),
    -- `pending_external` and `pending_subcase` are NOT new. They are live
    -- platform statuses: `core/case_lifecycle.py` PAUSES a case's SLAs on
    -- reaching either and resumes them on leaving, `relationship_manager.py`
    -- moves a parent off `pending_subcase` when its child resolves, and
    -- analytics reports on both. No case on this box happens to be in either
    -- state, so neither showed up in the six statuses actually in use — and
    -- omitting them would have made the sub-case feature refuse its own
    -- transition the first time a parent waited on a child.
    ('a1000000-0000-4000-8000-00000000000c', NULL, NULL, 'platform:any', 'pending_external', 'Pending External', 'waiting', FALSE, FALSE, 42),
    ('a1000000-0000-4000-8000-00000000000d', NULL, NULL, 'platform:any', 'pending_subcase',  'Pending Subcase',  'waiting', FALSE, FALSE, 44)
ON CONFLICT DO NOTHING;


-- ── report ──────────────────────────────────────────────────────────────────
--
-- Any status a case is ALREADY sitting on that the seed does not define. On
-- this platform that should be zero. A non-empty result is not an error and
-- must not be treated as one: those rows keep working, because validation
-- applies to WRITES, not to what is already stored. It is a list of statuses an
-- operator should add to the set so the UI can label them.

SELECT ci.status AS undefined_status, COUNT(*) AS cases
  FROM case_instances ci
 WHERE NOT EXISTS (
       SELECT 1 FROM case_statuses cs
        WHERE cs.tenant_id IS NULL AND cs.case_type_id IS NULL
          AND cs.name = ci.status)
 GROUP BY ci.status;
