# REVENANT — Delivery Plan

**Today:** 2026-08-27 · **Target complete:** 2026-09-02 · **Hard deadline:** 2026-09-05
**Working days to target: 6. Buffer after target: 3.**

The buffer is deliberate and is not spare capacity. Demo recording, submission
material, and the things that always break on the last day live there.

---

## Critical path

```
Foundation → Razorpay+Webhook → Detection → Agents → Executor → Outcome → Frontend → E2E → Demo
```

Policy Engine is already DONE (34/34 tests green), which buys back roughly a day.

---

## Day plan

### Day 1 — 27 Aug — Foundation
- Docker Compose (postgres + api)
- Schema, 12 tables, constraints as safety
- FastAPI skeleton, `/health`, `/health/deep`
- Synthetic dataset generator (~500 payments, realistic failure mix)
- **Exit:** `docker compose up` works, DB seeded, health green

### Day 2 — 28 Aug — Razorpay + events  ⚠️ credentials needed
- Razorpay test client, Payment Links
- Webhook receive → signature verify → dedupe → persist → normalize
- Signed replay harness (D3)
- Idempotency via DB unique constraint
- Event ingest, revenue detection
- **Exit:** a real test-mode Payment Link created; a signed webhook verified

### Day 3 — 29 Aug — AI agents
- LangGraph graph, state, checkpointing
- Investigation Agent, read-only tools
- Recovery Strategy Agent, propose-only, structured output
- Agent evals: hallucination, tool misuse, malformed output
- **Exit:** incident → structured root cause → structured proposed action

### Day 4 — 30 Aug — Execution + outcome
- Action Executor, narrow surface, idempotent, policy-gated
- Outcome Engine, webhook → recovered amount
- Audit Ledger, Metrics Engine
- **Exit:** full backend loop runs end to end, headless

### Day 5 — 31 Aug — Frontend
- 5 screens against real APIs
- Live status, loading/error/retry states
- **Exit:** demo walkable in a browser

### Day 6 — 1 Sep — Hardening
- E2E test of the full loop
- Security: policy bypass, prompt injection, tool abuse, replay, secret scan
- Chaos: all 7 mandated failure scenarios
- Tier 2 if green: Policy Simulator, then Failure Lab
- **Exit:** all 7 scenarios pass, no P0 open

### Day 7 — 2 Sep — Demo
- 3-minute demo rehearsed and recorded
- Metrics verified against real synthetic runs, all labelled Synthetic Test Data
- README, architecture diagram, submission material
- **Exit:** submittable

### 3–5 Sep — buffer

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Razorpay credentials slip past 28 Aug | Blocks Day 2, cascades | Replay harness + synthetic data keep Days 3–4 unblocked; only real-link creation waits |
| LangGraph debugging overruns | Squeezes frontend | Agent contract is a fixed dataclass. If the graph fights back, a plain tool-loop drops into the same interface without touching policy or executor |
| Frontend scope creep | Days 5–6 blow out | 5 screens are P0. Trim to 3 (Dashboard, Approval, Audit) before cutting test time |
| Schema churn after seeding | Rework | Data is synthetic and regenerable by design (D12) |

## Stop rule

If Day 5 ends with the backend loop unverified, **the frontend gets cut to
3 screens**, not the tests. A narrow demo that provably works beats a wide one
that does not.
