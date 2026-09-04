# REVENANT

**AI decides. Policy controls. Razorpay executes. Every step proves itself.**

An autonomous revenue-recovery agent for the [Razorpay AI Buildathon](https://razorpay.com/buildathon/) — Track 03, AI Revenue Recovery. It detects payments failing above baseline, has an AI agent investigate the real root cause and propose a fix, gates every proposal behind a deterministic policy engine, and executes the recovery on Razorpay's live test-mode APIs — with a full, hash-pinned audit trail behind every number it shows you.

[**Live demo →**](https://revenant-razor-pay-en9g.vercel.app) · [Architecture](docs/ARCHITECTURE.md) · [Decision log](docs/DECISIONS.md) · [Pitch script](docs/PITCH_SCRIPT.md)

![test-mode](https://img.shields.io/badge/razorpay-test--mode-528FF0?style=flat-square)
![tests](https://img.shields.io/badge/backend%20tests-216%20passing-2ea44f?style=flat-square)
![stack](https://img.shields.io/badge/stack-FastAPI%20%C2%B7%20LangGraph%20%C2%B7%20Next.js%2016-black?style=flat-square)

---

## The problem

Revenue loss from failed payments rarely happens in one clean step. A payment times out, a card gets declined, a UPI collect request expires — and nobody notices until a merchant checks a dashboard days later, by which point the customer has moved on. Most tooling stops at "here's a number that dropped." REVENANT closes the loop: **detect → investigate → decide → gate → execute → verify**, and shows its work at every stage.

## What it actually does — right now, on the live instance

| | |
|---|---|
| Revenue at risk detected | **₹1,05,487** across 24 failed payments |
| Recovered & verified | **₹3,448**, confirmed by real Razorpay webhooks — never self-reported |
| Incidents auto-detected | 3, each against the merchant's own rolling baseline (not a fixed threshold) |
| One incident, declined on purpose | The agent read the failure reasons underneath a spike that *looked* identical to a recoverable one, found expired cards and issuing-bank declines — structural, not transient — and refused to recommend recovery |

Every one of those numbers is read live from the database by the endpoints you'd hit yourself — nothing on the frontend is hardcoded.

## Why this isn't just an LLM with a Razorpay key

**The AI cannot move money. Structurally, not by prompt instruction.** The Investigation and Recovery agents (LangGraph) hold exactly six tools, all read-only — no tool in their graph can call Razorpay, write an execution record, or touch anything financial. It reads payment history, customer context, and merchant baseline, and returns a proposal. That's the entire extent of its power.

**Every proposal passes through one deterministic function — twice.** `policy/engine.py` is not an LLM and never sees a prompt. It runs once when the action is *authorized* (planning time) and again immediately before execution, against whatever is true *right now* — today's actual spend, this payment's live status, the customer's live opt-out flag. A decision made an hour ago isn't automatically still valid; the second gate exists because it once wasn't. (Real bug, real fix — see [D13](docs/DECISIONS.md).)

**Both evaluations are permanent, hash-pinned, and neither overwrites the other.** Every action stores a SHA-256 fingerprint of the exact policy values it ran against, at both phases. An action authorized under yesterday's limits and blocked under today's tightened ones leaves both facts on the record — not a version string that could mean anything by the time someone reads it. ([D15](docs/DECISIONS.md))

**Verification doesn't just trust a push.** Razorpay webhooks are signature-verified (HMAC-SHA256, replay-safe) and treated as the primary signal — but a webhook can fail to arrive, and this system found that out the hard way, twice, mid-build. So a reconciliation pass also polls Razorpay directly for anything left unconfirmed, and a payment only counts as recovered once one of those two *verified* paths confirms it. A forged or replayed event can never inflate the number this product leads with.

Full reasoning, including every bug this caught along the way, is in [docs/DECISIONS.md](docs/DECISIONS.md) — daily-cap leaks across merchants, a UTC-vs-timezone bug, a stale-approval race, an LLM timeout that crashed the wrong way. Nothing here is a claim without the fix that backs it.

## Architecture

```
failed payments  →  DETECT (baseline anomaly)  →  INVESTIGATE (AI, read-only)
                                                          ↓
                                            PROPOSE (AI) — or decline, on purpose
                                                          ↓
                                  AUTHORIZE  (Policy Engine, deterministic)
                                                          ↓
                                    human approval, if above the auto limit
                                                          ↓
                              RE-AUTHORIZE  (same Policy Engine, current state)
                                                          ↓
                                    EXECUTE (Razorpay, idempotency-keyed)
                                                          ↓
                          VERIFY (webhook, signature-checked  +  reconciliation poll)
```

Full diagram with every module named: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Stack:** FastAPI · PostgreSQL (Neon) · LangGraph · Next.js 16 / React 19 · Google Gemini (model-fallback chain, hard per-call timeout) · deployed on Vercel + Render, both free tier.

## Try it yourself

The **Failure Lab** (`/lab` on the live demo) calls the real Policy Engine directly — same function the executor calls before money moves, zero side effects, no auth needed. Drag the amount slider past ₹5,000 and watch the verdict flip from `AUTO_APPROVED` to `REQUIRES_APPROVAL` live. It's not a mock of the safety behavior; it's the safety behavior.

## Running locally

```bash
cp .env.example .env        # fill in RAZORPAY_*, GOOGLE_API_KEY, DATABASE_URL
docker compose up -d db
pip install -r requirements.txt -r requirements-dev.txt
uvicorn backend.main:app --reload

cd frontend && npm install && npm run dev
```

```bash
pytest -q   # 216 passed
```

See [docs/DEPLOY.md](docs/DEPLOY.md) for the free-tier Vercel + Render + Neon deployment this demo runs on.

## Repo map

```
backend/
  detection/     baseline-vs-current anomaly detection
  agents/        Investigation + Recovery agents (LangGraph, read-only tools)
  policy/        the deterministic gate — the only thing that says yes to money
  recovery/      executor, idempotency, reconciliation, provenance
  integrations/  Razorpay client, webhook verification
frontend/        Next.js 16 — dashboard, incidents, approvals, audit, lab
docs/            decisions, architecture, deploy guide, pitch script
```

---

Built for the Razorpay AI Buildathon. Everything above is verifiable against the live instance — nothing here is a claim without a place to check it.
