# REVENANT — Security

Audited 2026-08-27. Findings, fixes, and remaining accepted risk.

---

## P0 — FIXED — Unauthenticated forged-event injection

**Found by:** exploiting it. Not by reading code.

`POST /dev/replay-webhook` self-signs payloads with the server's webhook
secret, so reaching it is equivalent to knowing that secret. It was gated only
by `ENABLE_DEV_ENDPOINTS`, which **defaulted to `true`**, had no
authentication, and the app was running behind a public Cloudflare tunnel.

**Proven exploit** — from the public internet, with no credentials:

```
POST https://<tunnel>/dev/replay-webhook
{"event_id":"evt_ATTACKER_FORGED_001", ... "amount":50000000, "status":"paid"}
-> HTTP 200 {"result":"accepted","signature_verified":true}
```

The forged ₹500,000 "payment succeeded" event was stored with
`signature_valid = TRUE`. Once the Outcome Engine exists, this would let any
anonymous caller fabricate recovered revenue — the exact failure the whole
architecture is built to prevent.

**Fix — two INDEPENDENT controls**, because one control with a plausible
failure mode is not a control:

1. **Loopback-only.** Dev endpoints refuse any request not originating from
   127.0.0.1 / ::1, regardless of flags. `X-Forwarded-For` is deliberately NOT
   trusted — it is attacker-controlled, and trusting it would restore the exact
   bypass being closed.
2. **API key**, constant-time compared. An unset server key fails closed.

**Verified with the flag deliberately ON (worst case):**

| attempt | result |
|---|---|
| remote, no key | 404 |
| remote, **valid key** | 404 |
| remote, spoofed `X-Forwarded-For: 127.0.0.1` | 404 |
| localhost, no key | 401 |
| localhost, wrong key | 401 |
| localhost, valid key | 200 |

404 rather than 403: do not confirm the endpoint exists.

**Defaults changed:** `ENABLE_DEV_ENDPOINTS` and `ENABLE_API_DOCS` now default
to `false`. `API_KEY` has no default — a shipped default key is the same as no
key.

---

## P1 — FIXED — Live-mode guard was bypassable

The refusal to run against live Razorpay credentials lived in
`get_settings()`, so any code constructing `Settings()` directly slipped past
it. A financial guard that depends on callers choosing the right factory is not
a guard.

**Fix:** moved onto the model as a pydantic validator. Refused at construction,
case-insensitively, however Settings is built.

---

## P1 — FIXED — 11 dependency CVEs

`pip-audit` found 11 known vulnerabilities across 3 packages, 8 of them in
Starlette (FastAPI's HTTP layer).

Upgraded: fastapi 0.115.6 → 0.141.1, starlette 0.41.3 → 1.6.0,
python-dotenv 1.0.1 → 1.2.3, pytest 8.3.4 → 9.1.1.

**Re-audit: no known vulnerabilities.** All 111 tests pass on the new versions.

---

## P2 — FIXED — Unbounded webhook body

`POST /webhooks/razorpay` is necessarily public and unauthenticated, and
`await request.body()` buffers the entire request in memory. An unbounded POST
was a trivial memory-exhaustion vector.

**Fix:** 256KB cap, checked on `content-length` before buffering and again on
actual length to cover chunked transfers. Verified: 5MB → 413, normal → 200.

---

## P2 — FIXED — Health endpoint reported the wrong LLM provider

`/health/deep` hardcoded `"provider": "anthropic"` and was never updated after
D4 was revised to Gemini. It reported a provider the system was not using —
precisely the false-green a health check exists to prevent. Now reports the
active provider and model.

---

## Verified controls

| Control | Evidence |
|---|---|
| Webhook signature (HMAC-SHA256, constant-time) | 14 tests + real Razorpay delivery |
| Tampered payload rejected | amount forgery → 401, through the public tunnel |
| Duplicate webhook processed once | DB unique constraint + tests |
| Duplicate execution impossible | DB idempotency key **and** Razorpay `reference_id` rejection |
| Webhook cannot invent a payment row | unknown `payment_id` stores NULL — verified on real data |
| SQL injection | every query parameterized; no f-string interpolation anywhere |
| Secrets in git | none; `.env` gitignored and verified |
| Secrets in responses | `/health/deep` asserted free of every configured secret |
| Agent cannot move money | no such tool exists; toolset ∩ forbidden = ∅ |
| Agent cannot write | source scanned for INSERT/UPDATE/DELETE/DROP/TRUNCATE/ALTER |
| Agent cannot reach Razorpay | no import, no reference in executable code |
| Agent cross-merchant read | `merchant_id` bound at construction, not model-controlled |
| Agent output cannot express an action | `InvestigationResult` has no action or amount field |
| Live mode | refused at construction |

---

## Accepted risk — stated, not hidden

**1. PII in stored webhook payloads.** Real Razorpay events contain customer
email and phone. One such payload is already stored from live testing. This is
inherent to keeping raw payloads as verification evidence.
*Constraint:* any merchant-facing event view must redact contact details, and
no raw payload goes on screen during the demo. To be enforced when the audit
UI is built.

**2. No rate limiting on `/webhooks/razorpay`.** It must stay public and
unauthenticated to receive Razorpay traffic. Signature verification means a
flood cannot inject data, but it can consume CPU. Accepted for a demo;
production would need per-IP limits.

**3. Replayed events are tagged, not blocked.** `source='replay'` distinguishes
them. **The Outcome Engine MUST exclude `source != 'razorpay'` from any figure
presented as real recovered revenue.** Recorded here because that component is
not yet built and the rule must not be lost.

**4. Single shared API key, no rotation.** Adequate for a single-merchant demo
(decision D9). Real multi-tenant auth was explicitly out of scope.
