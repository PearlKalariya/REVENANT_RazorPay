# REVENANT — MVP Scope

Status: **PROPOSED — awaiting human confirmation (D7)**

The MVP is ONE complete, verified, measurable loop:

```
Payment Failure → Detection → Investigation → Recovery Strategy
→ Policy → Approval/Auto → Razorpay Test Execution → Webhook
→ Outcome → Audit → Metrics
```

Nothing ships that does not serve that loop.

---

## TIER 1 — CORE (must work end to end)

### Data (PostgreSQL)
Single demo merchant. Tables:
`merchants, customers, payments, payment_events, revenue_incidents,
investigations, recovery_actions, policy_decisions, approvals,
execution_records, audit_events, jobs`

Synthetic dataset: ~500 payments with realistic failure mix
(UPI timeout, insufficient funds, card decline, bank downtime).
Labelled **Synthetic Test Data** everywhere it surfaces.

### Deterministic components
- Event ingest → normalize → deduplicate
- Revenue detection (failure clustering → incident + revenue-at-risk)
- **Policy Engine** (P0 — deterministic, exhaustively tested)
- Action Executor (narrow surface, idempotent)
- Outcome Engine (webhook → recovered amount)
- Audit Ledger (append-only)
- Metrics Engine

### AI components (LangGraph + Claude)
- Investigation Agent — read-only tools, structured root cause
- Recovery Strategy Agent — propose-only, structured action
- Explanation Agent — merchant-facing text from structured facts

### Razorpay (test mode)
- **Payment Links only** — the single execution primitive
- Webhook receive → signature verify → dedupe → persist → normalize → process
- Idempotency on every financial action
- Signed replay harness (D3)

### API
```
POST /events
GET  /incidents
GET  /incidents/{id}
GET  /recovery-actions
POST /recovery-actions/{id}/approve
POST /recovery-actions/{id}/deny
POST /webhooks/razorpay
GET  /audit
GET  /metrics
GET  /health
GET  /health/deep
```
Dev-only, gated by `ENABLE_DEV_ENDPOINTS`:
```
POST /dev/simulate/{scenario}
POST /dev/replay-webhook
```

### Frontend (Next.js) — 5 screens
1. **Dashboard** — revenue at risk, recovered, recovery rate, active incidents, pending approvals
2. **Incident** — problem, root cause, affected transactions, revenue at risk, explanation
3. **Recovery Center** — proposed action, expected recovery, policy status, execution status
4. **Approval Center** — customer, amount, reason, policy result, approve / deny
5. **Audit Timeline** — Detected → Investigated → Proposed → Policy → Approved → Executed → Verified → Recovered

### Tests
- Policy Engine unit tests (exhaustive)
- Integration: webhook → detection → agent → policy → executor
- E2E: the full loop above
- The 7 mandated failure scenarios

### The 7 failure scenarios (all must pass)
| # | Scenario | Expected |
|---|---|---|
| 1 | Already paid | BLOCKED, no duplicate recovery |
| 2 | Amount too high | REQUIRES_APPROVAL |
| 3 | Daily limit exceeded | BLOCKED |
| 4 | Duplicate webhook | processed exactly once |
| 5 | Razorpay timeout | safe retry, no duplicate action |
| 6 | Customer opted out | BLOCKED |
| 7 | Agent proposes unsafe action | Policy Engine overrides agent |

---

## TIER 2 — STRETCH (build only after Tier 1 is verified green)

- **Policy Simulator** — same engine as production, no forked logic. High demo value, low effort.
- **Failure Lab** — dev-only buttons driving the 7 scenarios. High demo value, mostly reuses test fixtures.
- **Control vs REVENANT experiment** — credibility for the value claim, but only meaningful once the core loop produces real numbers.

These three are ranked in build order. They are the highest demo-value-per-hour
work in the project — but they are worthless on a broken core.

---

## TIER 3 — EXPLICITLY EXCLUDED

Not built. Not started. Not discussed further unless you reopen scope.

- Failed subscription recovery
- Checkout abandonment recovery
- Mandate retry
- Payment degradation detection
- Revenue forecasting
- Recovery probability ML model (a transparent heuristic score is Tier 1; a trained model is not)
- Natural-language merchant commands
- Fraud, accounting, CRM, marketing automation
- Mobile app
- Refunds and payouts — **the executor will never hold these capabilities**

---

## SCOPE CALLS NEEDING YOUR CONFIRMATION

**1. Authentication.** Real multi-tenant auth is days of work with no judging
payoff. Proposal: single hardcoded demo merchant + a static API key header on
mutating endpoints. Security Agent still covers webhook signatures, prompt
injection, tool permissions, and policy bypass — the things that actually matter
here. **Agree?**

**2. Payment Links as the only execution primitive.** Spec also lists
`retry_payment`. Razorpay test mode cannot genuinely re-charge a failed payment
without a saved mandate, so a "retry" would be theatre. Proposal: Payment Link
is the real, verifiable action; notification is non-financial and free.
**Agree?**

**3. Deployment.** Local Docker Compose + tunnel is enough for a recorded demo.
Cloud deploy is a nice-to-have. Proposal: local only, cloud if time.
**Agree?**
