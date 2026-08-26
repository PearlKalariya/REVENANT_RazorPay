# REVENANT — Decision Log

Every consequential decision is recorded here. Agents must not silently change an
APPROVED decision. If circumstances change, reopen the decision with the human.

---

## D1 — Technology stack
**Question:** Which stack for backend, DB, agent framework, frontend?
**Options:** A) FastAPI + PostgreSQL + LangGraph + Next.js · B) same minus LangGraph (plain Anthropic SDK) · C) SQLite variant
**Chosen:** **A**
**Reason:** Human selected. LangGraph provides an explicit agent-graph story and
first-class state/checkpointing for the Investigation → Recovery flow.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** `agents/` built on LangGraph. Postgres is the system of record.
Frontend is Next.js. Adds LangGraph as a core dependency.

---

## D2 — Razorpay test credentials
**Question:** How to obtain test-mode credentials?
**Chosen:** Deferred. Human supplies `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET` on 2026-08-28.
**Status:** APPROVED (deferred) · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** Phases touching live Razorpay calls are sequenced after credentials
arrive. All prior phases proceed against synthetic data and a signed replay
harness. No credentials are invented or hardcoded. `.env` is gitignored.

---

## D3 — Webhook ingress
**Question:** Razorpay cannot reach localhost. How are webhooks verified?
**Options:** A) tunnel only · B) simulated replay only · C) both
**Chosen:** **C**
**Reason:** Human selected. Real tunnel run proves genuine integration; signed
replay harness makes tests and the demo deterministic and offline-safe.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** Both paths enter the SAME handler and BOTH verify signatures. The
replay harness signs payloads with the real webhook secret — it does not bypass
verification. Replay endpoints are gated behind `ENABLE_DEV_ENDPOINTS`.

---

## D4 — LLM provider
**Chosen:** **A — Anthropic (Claude)**
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** `ANTHROPIC_API_KEY` required for Investigation/Recovery/Explanation
agents. Structured output is parsed by the deterministic Policy Engine, which
fails closed on malformed agent proposals.

---

## D5 — Redis
**Question:** Include Redis for queue/dedup, or Postgres only?
**Chosen:** **B — Postgres only.** Add Redis only if measured load demands it.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** Deliberate deviation from the spec architecture diagram.
Idempotency = Postgres unique constraint on idempotency key.
Job dispatch = `SELECT ... FOR UPDATE SKIP LOCKED` over a jobs table.
`/health/deep` will NOT report a `redis` key. One less container to fail
during the demo.

---

## D6 — Project name
**Chosen:** **B — product renamed to REVENANT.**
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27
**Impact:** Matches existing remote `REVENANT_RazorPay`. No GitHub rename needed.
All docs, UI, and pitch material use REVENANT.

---

## D7 — MVP scope
**Status:** **PENDING HUMAN CONFIRMATION**
Proposed scope is in `docs/MVP_SCOPE.md`. No P2 work begins until confirmed.

---

## D8 — Money representation
**Question:** Store amounts in rupees or paise?
**Chosen:** **Integer paise everywhere internally.** Never floats.
**Reason:** Matches the Razorpay wire format exactly, so no conversion happens at
the execution boundary — the highest-risk place for a money bug. Integers
eliminate floating-point rounding error. Rupees appear only in UI formatting.
**Status:** APPROVED (agent decision — Category B, reversible, isolated)
**Impact:** Policy config uses `_paise` suffixes. `max_auto_amount_paise =
500_000` means ₹5,000. Deviation from the spec YAML, which wrote rupees.
**Flagged for human review.**

---

## D7 — MVP scope
**Chosen:** Scope in `docs/MVP_SCOPE.md` as written. Tier 3 stays excluded.
Human declined to promote checkout abandonment or the experiment.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27

---

## D9 — Authentication
**Chosen:** Single hardcoded demo merchant + static API key header on mutating
endpoints. No user accounts, no multi-tenancy.
**Reason:** Real auth costs days and earns no judging credit for Track 03.
Security effort goes where the actual attack surface is: webhook signatures,
prompt injection, tool permissions, policy bypass.
**Status:** AGENT DECISION — pending human override · **Date:** 2026-08-27

---

## D10 — Execution primitive
**Chosen:** Payment Links are the only money-moving action.
`SEND_RECOVERY_NOTIFICATION` is non-financial. `retry_payment` is NOT built.
**Reason:** Razorpay test mode cannot genuinely re-charge a failed payment
without a saved mandate. A "retry" would be theatre and a judge may probe it.
One real, verifiable action beats two where one is fake.
**Status:** AGENT DECISION — pending human override · **Date:** 2026-08-27

---

## D11 — Deployment target
**Chosen:** Local Docker Compose + tunnel. Cloud deploy only if time remains.
**Status:** AGENT DECISION — pending human override · **Date:** 2026-08-27

---

## D12 — Database access and migrations
**Chosen:** `asyncpg` with plain SQL. Schema applied by Postgres natively via
`docker-entrypoint-initdb.d`. No ORM, no Alembic.
**Reason:** ~15 queries total in the MVP. An ORM plus a migration tool is more
machinery than the problem needs, and both are things that can break at 2am
before a deadline. Postgres applies init SQL by itself — that is the native
feature doing the job.
**Trade-off, stated plainly:** schema changes require dropping the volume and
recreating. Acceptable ONLY because all data is synthetic and regenerable.
This would be the wrong call with real data.
**Status:** AGENT DECISION — pending human override · **Date:** 2026-08-27
