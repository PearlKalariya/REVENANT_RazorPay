-- REVENANT schema.
--
-- Applied automatically by Postgres via docker-entrypoint-initdb.d (D12).
--
-- Money is INTEGER PAISE everywhere (D8). Never NUMERIC, never float.
-- Constraints here are a safety layer, not decoration: the idempotency and
-- duplicate-execution guarantees are enforced by the database, so they hold
-- even if application code is wrong.

SET timezone = 'UTC';

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE payment_status AS ENUM (
    'created', 'authorized', 'captured', 'failed', 'refunded'
);

CREATE TYPE incident_status AS ENUM (
    'open', 'investigating', 'recovering', 'resolved', 'closed'
);

CREATE TYPE action_type AS ENUM (
    'CREATE_PAYMENT_LINK', 'SEND_RECOVERY_NOTIFICATION'
);

CREATE TYPE action_status AS ENUM (
    'proposed', 'policy_checked', 'awaiting_approval', 'approved',
    'denied', 'executing', 'executed', 'failed', 'expired'
);

CREATE TYPE policy_result AS ENUM (
    'AUTO_APPROVED', 'REQUIRES_APPROVAL', 'BLOCKED'
);

CREATE TYPE execution_status AS ENUM (
    'pending', 'succeeded', 'failed', 'timeout'
);

CREATE TYPE job_status AS ENUM (
    'queued', 'running', 'done', 'failed'
);

-- ---------------------------------------------------------------------------
-- Core entities
-- ---------------------------------------------------------------------------

CREATE TABLE merchants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    -- Policy config is stored per merchant so the Policy Simulator and
    -- production read the exact same values. No forked policy logic.
    policy_config   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE customers (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    email           TEXT,
    phone           TEXT,
    -- Enforced by the Policy Engine on every action, financial or not.
    opted_out       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_customers_merchant ON customers(merchant_id);

CREATE TABLE payments (
    id              TEXT PRIMARY KEY,              -- Razorpay payment id
    merchant_id     TEXT NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    amount_paise    BIGINT NOT NULL CHECK (amount_paise > 0),
    currency        TEXT NOT NULL DEFAULT 'INR',
    status          payment_status NOT NULL,
    method          TEXT,                          -- upi | card | netbanking
    failure_reason  TEXT,
    failure_code    TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Every synthetic row is marked. Metrics must never silently mix
    -- synthetic and real data (metrics-integrity rule).
    is_synthetic    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_payments_status  ON payments(merchant_id, status);
CREATE INDEX idx_payments_customer ON payments(customer_id);
CREATE INDEX idx_payments_created  ON payments(created_at DESC);

-- ---------------------------------------------------------------------------
-- Event ingest
-- ---------------------------------------------------------------------------

CREATE TABLE payment_events (
    id              BIGSERIAL PRIMARY KEY,
    -- Razorpay's event id. The UNIQUE constraint IS the duplicate-webhook
    -- defence (mandated failure scenario 4). Enforced by the database, so a
    -- duplicate is impossible even if the dedupe code is wrong.
    event_id        TEXT NOT NULL UNIQUE,
    payment_id      TEXT REFERENCES payments(id) ON DELETE SET NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    signature_valid BOOLEAN NOT NULL,
    source          TEXT NOT NULL DEFAULT 'razorpay',  -- razorpay | replay
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX idx_events_unprocessed ON payment_events(received_at)
    WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Detection → investigation → recovery
-- ---------------------------------------------------------------------------

CREATE TABLE revenue_incidents (
    id                  BIGSERIAL PRIMARY KEY,
    merchant_id         TEXT NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    status              incident_status NOT NULL DEFAULT 'open',
    revenue_at_risk_paise BIGINT NOT NULL DEFAULT 0 CHECK (revenue_at_risk_paise >= 0),
    affected_count      INTEGER NOT NULL DEFAULT 0,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX idx_incidents_open ON revenue_incidents(merchant_id, status);

CREATE TABLE investigations (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     BIGINT NOT NULL REFERENCES revenue_incidents(id) ON DELETE CASCADE,
    root_cause      TEXT NOT NULL,
    confidence      REAL CHECK (confidence >= 0 AND confidence <= 1),
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Merchant-facing text from the Explanation Agent. Never chain-of-thought.
    explanation     TEXT,
    model           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_investigations_incident ON investigations(incident_id);

CREATE TABLE recovery_actions (
    id                  BIGSERIAL PRIMARY KEY,
    incident_id         BIGINT NOT NULL REFERENCES revenue_incidents(id) ON DELETE CASCADE,
    payment_id          TEXT NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    customer_id         TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    action              action_type NOT NULL,
    amount_paise        BIGINT NOT NULL CHECK (amount_paise >= 0),
    status              action_status NOT NULL DEFAULT 'proposed',
    -- Advisory only. Must never override the Policy Engine.
    recovery_score      REAL CHECK (recovery_score >= 0 AND recovery_score <= 1),
    rationale           TEXT,
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,
    -- One live recovery action per payment. Stops the agent proposing the same
    -- recovery twice while the first is still in flight.
    CONSTRAINT one_live_action_per_payment EXCLUDE USING btree (
        payment_id WITH =
    ) WHERE (status IN ('proposed','policy_checked','awaiting_approval','approved','executing'))
);

CREATE INDEX idx_actions_incident ON recovery_actions(incident_id);
CREATE INDEX idx_actions_status   ON recovery_actions(status);

-- ---------------------------------------------------------------------------
-- Policy and approval
-- ---------------------------------------------------------------------------

CREATE TABLE policy_decisions (
    id              BIGSERIAL PRIMARY KEY,
    action_id       BIGINT NOT NULL REFERENCES recovery_actions(id) ON DELETE CASCADE,
    result          policy_result NOT NULL,
    rule            TEXT NOT NULL,
    reason          TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_policy_action ON policy_decisions(action_id);

CREATE TABLE approvals (
    id              BIGSERIAL PRIMARY KEY,
    action_id       BIGINT NOT NULL REFERENCES recovery_actions(id) ON DELETE CASCADE,
    approved        BOOLEAN NOT NULL,
    approver        TEXT NOT NULL,
    note            TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- An action can only be decided once.
    UNIQUE (action_id)
);

-- ---------------------------------------------------------------------------
-- Execution
-- ---------------------------------------------------------------------------

CREATE TABLE execution_records (
    id                  BIGSERIAL PRIMARY KEY,
    action_id           BIGINT NOT NULL REFERENCES recovery_actions(id) ON DELETE CASCADE,
    -- THE duplicate-execution defence (mandated failure scenario 5).
    -- A Razorpay timeout that gets retried collides here and returns the
    -- existing record instead of charging twice.
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              execution_status NOT NULL DEFAULT 'pending',
    -- Amount actually sent to Razorpay. Compared against the approved amount
    -- so a post-approval amount change is detectable.
    amount_paise        BIGINT NOT NULL CHECK (amount_paise >= 0),
    razorpay_ref        TEXT,               -- payment link id
    razorpay_short_url  TEXT,
    error               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_exec_action ON execution_records(action_id);
CREATE INDEX idx_exec_ref    ON execution_records(razorpay_ref);

-- Outcome: money actually recovered, proven by a verified webhook.
CREATE TABLE recovery_outcomes (
    id                  BIGSERIAL PRIMARY KEY,
    execution_id        BIGINT NOT NULL REFERENCES execution_records(id) ON DELETE CASCADE,
    recovered_paise     BIGINT NOT NULL DEFAULT 0 CHECK (recovered_paise >= 0),
    succeeded           BOOLEAN NOT NULL,
    -- The event that proves it. Outcomes without a verified event are not
    -- counted in metrics.
    verified_by_event   TEXT REFERENCES payment_events(event_id),
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (execution_id)
);

-- ---------------------------------------------------------------------------
-- Audit — append only
-- ---------------------------------------------------------------------------

CREATE TABLE audit_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,          -- SYSTEM | INVESTIGATION_AGENT | ...
    event_type      TEXT NOT NULL,
    merchant_id     TEXT,
    customer_id     TEXT,
    payment_id      TEXT,
    incident_id     BIGINT,
    action_id       BIGINT,
    execution_id    BIGINT,
    amount_paise    BIGINT,
    policy_version  TEXT,
    policy_result   policy_result,
    approval_id     BIGINT,
    reason          TEXT,
    result          TEXT,
    error           TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_audit_ts       ON audit_events(ts DESC);
CREATE INDEX idx_audit_action   ON audit_events(action_id);
CREATE INDEX idx_audit_incident ON audit_events(incident_id);

REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Jobs — Postgres replaces Redis here (D5)
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          job_status NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Workers claim with: SELECT ... FOR UPDATE SKIP LOCKED
CREATE INDEX idx_jobs_claimable ON jobs(run_after)
    WHERE status = 'queued';
