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

---

## D4 (REVISED) — LLM provider
**Supersedes the original D4 (Anthropic).**
**Question:** Anthropic requires paid credits. Which provider now?
**Options:** A) add Anthropic credits · B) Gemini free tier · C) Groq · D) local Ollama
**Chosen:** **B — Google Gemini free tier.**
**Reason:** Human declined to add credits. Gemini's free tier handles this
workload with no card. Groq and Ollama were advised against: the Investigation
Agent chains 4-6 tool calls and reasons over real figures, which is exactly
where smaller models degrade, and a hallucinated failure rate in a financial
demo is the worst available failure mode.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-08-27

**Quality mitigations**, since the human explicitly did not want to compromise
on quality — free-tier models are less consistent at schema-valid structured
output, so this is handled rather than hoped away (`backend/agents/llm.py`):
* Structured output validation is MANDATORY; malformed responses raise instead
  of propagating a half-parsed object into recovery logic.
* Up to 3 retries on schema failure, feeding the validation error back to the
  model. Most schema misses self-correct when shown what was wrong.
* Exponential backoff on 429 / quota errors, so a free-tier throttle costs
  seconds rather than the demo run.
* Fails closed. An incident with no investigation is a visible gap; one with a
  hallucinated investigation is a silent one.

**Provider remains swappable** via `LLM_PROVIDER`. Adding Anthropic credits
later is a one-line env change, not a refactor. The tools, capability-boundary
tests, structured output contract, and the "policy overrides the agent"
guarantee are all provider-independent.

---

## D13 — Policy is re-evaluated at EXECUTION time, not only at planning time
**Found by:** auditing, then reproducing. 22 actions approved across a day
totalled ₹38,893 against a ₹25,000 daily cap, and **all 22 executed**. Nothing
re-checked the cap between planning and execution.
**Chosen:** the executor re-runs the same deterministic Policy Engine against
CURRENT state immediately before money moves.
**Reason:** a stored policy decision records that an action was AUTHORISED. It
does not prove it is still PERMITTED — the daily total, the payment's status,
the customer's opt-out and the retry count all change between the two moments.
Policy has to be the gate at the point money moves, not only when the plan was
drawn up.
**Verified:** same batch now executes ₹24,822 with 17 refused as
`daily_limit_exceeded`.
**Also:** the executor's hand-rolled already-paid / opt-out checks were deleted.
The Policy Engine owns those rules; a second copy in the executor is a second
thing to keep in sync and a second thing to get wrong.
**Status:** AGENT DECISION — pending human review · **Date:** 2026-08-28

---

## D14 — The daily cap follows the merchant's timezone, not UTC
**Question:** which 24 hours does "₹25,000 per day" mean?
**Chosen:** `Asia/Kolkata`, configurable per merchant.
**Reason:** a UTC day rolls over at 05:30 IST. Under a UTC window an Indian
merchant's daily cap resets mid-morning, and spend between midnight and 05:30
IST is attributed to the previous day — so the effective cap for that window is
higher than stated. Demonstrated: ₹50 spent at 02:00 IST was invisible to a UTC
window at 11:00 IST the same day.
**The LIMIT is unchanged.** Only the window it applies to is corrected.
**Status:** AGENT DECISION — pending human review · **Date:** 2026-08-28

---

## D15 — Policy version provenance across authorization and execution
**Chosen:** every recovery action records BOTH the policy that authorized it
and the policy evaluated immediately before execution. Neither overwrites the
other.

**Principle:** *authorization history and execution authority are two separate
facts.* One says why the action was allowed to be planned; the other says why
money did or did not move. Collapsing them into a single mutable field destroys
the ability to answer either question afterwards.

**Stored as two rows** in `policy_decisions`, distinguished by `phase`
(`authorization` | `execution`). A unique partial index allows exactly one
authorization per action; execution-phase rows are unconstrained, because an
action may legitimately be evaluated for execution more than once.

**Snapshot hash, not just a version string.** Versions are reused, migrated and
renamed — "v3" next year may not describe the rules it describes today. Each
evaluation stores a SHA-256 fingerprint of the complete policy snapshot it ran
against, so the execution-time evaluation is reproducible even if the
representation of policy changes underneath.

**Explicit field names**, because a bare `policy_version` is ambiguous about
which evaluation it describes:
```
authorized_policy_version   / authorized_policy_hash   / authorized_at
execution_policy_version    / execution_policy_hash
                            / execution_policy_evaluated_at / executed_at
```

**An execution-phase decision is written even when the action is REFUSED.** A
refusal creates no execution record, so without that row the reason money did
NOT move would exist only in a log line.

**Answers the three auditor questions:**
1. Why was it originally allowed? → the authorization evaluation
2. Why wasn't it executed? → the execution evaluation
3. Was the newer policy correctly applied? → both snapshots pinned by hash

**Status:** APPROVED — required architectural correction, not a nice-to-have.
Human confirmed. Marked *required before production, not demo-blocking*.
**Date:** 2026-08-28

---

## D16 — Monetary field names must be currency-neutral
**Status: TECHNICAL DEBT — deliberately deferred, not overlooked.**

**Problem:** fields such as `max_daily_recovery_paise` hold generic minor
currency units, not Indian paise specifically. For a USD merchant,
`max_daily_recovery_paise = 500_000` with `currency = "USD"` means **$5,000.00**
— the field name actively implies the wrong currency.

**Target:**
```
*_paise  ->  *_amount_minor  (or *_minor)
```
with an explicit ISO-4217 `currency` alongside. The currency supplies the
semantic context; the `_minor` suffix supplies the unit.

**Rule regardless of naming:** the application must NEVER infer currency from a
field name. Currency is data, not nomenclature.

**Preferred end state — a Money value object**, so amounts in different
currencies cannot be compared or summed by accident:
```
Money
├── amount_minor
└── currency
```
The Policy Engine would operate on `Money(500_000, "USD")` rather than naked
integers. This matters as soon as one agent operates across INR/USD/EUR/GBP.

**Why NOT renamed now:** this is a cross-system contract change, not a
find-and-replace. Scope: DB schema, models/types, Policy Engine, queries, API
contracts, planner, executor, frontend, fixtures, tests, audit/event schemas.

A PARTIAL rename is strictly worse than the current debt, because
`*_paise` and `*_minor` would coexist in different layers of the same system —
and the resulting ambiguity is exactly the bug this rename exists to prevent.

**Risk:** medium — broad schema contract change.
**Priority:** high before multi-currency production.
**Demo blocker:** no. **Production blocker:** yes, for non-INR currencies.
**Mitigation until then:** `currency` is stored per merchant AND enforced at the
Razorpay client boundary — a merchant configured for a currency this
integration cannot settle is refused rather than silently charged in rupees.
**Date:** 2026-08-28

---

## D17 — Demo merchant's daily cap raised to ₹75,000
**Question:** approvals were being refused with `daily_limit_exceeded` because
the day's recoveries had reached the ₹25,000 cap.
**Chosen:** raise the DEMO MERCHANT's `max_daily_recovery_minor` to ₹75,000.
The ₹5,000 autonomous limit is unchanged.
**Status:** APPROVED · **Approved by:** Human · **Date:** 2026-09-02

**Reason — a sizing mismatch, not a safety change.** The ₹25,000 cap was set
when the synthetic dataset held ₹53,737 at risk. The dataset now holds ₹76,827
(a second, structural failure cluster was added), so the cap was no longer
proportionate to the data it governs and blocked ordinary recoveries rather
than excessive ones.

**What did NOT change:** the enforcement. D13's execution-time re-evaluation
still runs against whatever number is configured, and the cap still blocks when
genuinely exceeded — verified in the Failure Lab, where ₹3,000 against a spent
budget is still refused.

**Scope:** this merchant's configuration row only (D14 — limits are
merchant-owned). No code default changed; `PolicyConfig` still ships ₹25,000.

---

## D18 — Recovery actions live 24 hours, not 60 minutes
**Status:** AGENT DECISION — pending human confirmation · **Date:** 2026-09-02

**Problem:** `action_ttl_minutes` was 60. Any approval not granted within the
hour expired, and the executor correctly refused it. Observed repeatedly: a
queue of pending approvals emptied itself before anyone could act on it.

**Why 60 minutes is wrong on product grounds, not just demo grounds.** This TTL
governs a queue a HUMAN works through. A merchant who steps away for lunch
returns to an expired queue and no recovered revenue — the system would refuse
work it was explicitly asked to do, for no safety benefit.

**Chosen:** 24 hours for the demo merchant. Merchant-configurable (D14); the
code default is unchanged.

**Why this does not weaken safety.** Freshness was never what made a stale
approval safe — D13 is. Policy is re-evaluated against CURRENT state
immediately before money moves, so an action approved yesterday is still
refused today if the cap is now spent, the customer has opted out, or the
payment has settled. The TTL is a coarse backstop; the real guard is the
re-check, and it is unaffected.

**Still enforced:** an action older than the window is refused, and the refusal
is recorded with `action_expired` in the audit trail.
