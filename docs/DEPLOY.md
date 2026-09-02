# Deploying REVENANT

Three pieces, three hosts, all free tiers. ~45 minutes.

```
Neon        Postgres     free, no expiry, scales to zero
Render      FastAPI      free web service, Docker
Vercel      Next.js      free hobby, git push deploys
```

**Everything is prepared and verified locally:** the Docker image builds and
serves on an injected `$PORT`, the frontend production build passes, and
`render.yaml` declares the service. What follows is only the parts that need
your accounts.

---

## Before you start

The one thing worth knowing up front: **free backend tiers sleep after ~15
minutes idle**, and the first request afterwards takes 30–60 seconds. Mitigate
by hitting the URL a minute before you present, or set an uptime pinger on
`/health`.

---

## 1 · Database — Neon (~5 min)

1. **neon.tech** → sign up → **Create project**, region closest to you
2. Copy the **connection string** (`postgresql://…?sslmode=require`)
3. Load the schema and seed:

```bash
export DATABASE_URL='<your neon connection string>'
psql "$DATABASE_URL" -f db/schema.sql
.venv/bin/python -m backend.seed
```

If `psql` is missing: `docker run --rm -i -v "$PWD/db:/db" postgres:16-alpine \
psql "$DATABASE_URL" -f /db/schema.sql`

**Why Neon over Supabase:** Supabase pauses a free project after a week idle.
Neon scales to zero and wakes on demand.

---

## 2 · Backend — Render (~15 min)

1. Push to GitHub (the repo already has `Dockerfile` and `render.yaml`)
2. **render.com** → **New** → **Blueprint** → select the repo
3. Render reads `render.yaml` and prompts for each secret:

| variable | value |
|---|---|
| `DATABASE_URL` | the Neon string |
| `RAZORPAY_KEY_ID` | your `rzp_test_…` |
| `RAZORPAY_KEY_SECRET` | from `.env` |
| `RAZORPAY_WEBHOOK_SECRET` | from `.env` |
| `GOOGLE_API_KEY` | from `.env` |
| `API_KEY` | from `.env` — guards approve/deny |
| `CORS_ORIGINS` | **fill in after step 3** |

4. Deploy. When it's live, check `https://<your-app>.onrender.com/health/deep` —
   `database` must read `ok`.

**Never set `ENABLE_DEV_ENDPOINTS=true` in production.** The replay endpoint
signs payloads with your own webhook secret; reaching it is equivalent to
knowing that secret. It is loopback-only and API-key guarded, but the flag
should stay off regardless.

---

## 3 · Frontend — Vercel (~10 min)

1. **vercel.com** → **Add New Project** → same repo
2. **Root Directory: `frontend`** (this is the step people miss)
3. Environment variables:

| variable | value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://<your-app>.onrender.com` |
| `NEXT_PUBLIC_API_KEY` | same `API_KEY` as the backend |

4. Deploy, then go back to Render and set `CORS_ORIGINS` to the exact Vercel
   URL (`https://revenant.vercel.app` — no trailing slash). Redeploy the backend.

**`NEXT_PUBLIC_*` is visible in the browser bundle.** That is inherent to a
browser calling an API directly, and it is why the demo key only guards
approve/deny on one synthetic merchant. A real deployment moves this behind a
session — see D9.

---

## 4 · Point Razorpay at the deployment (~5 min)

This is the part that gets genuinely better: **the URL stops changing.** No more
cloudflared tunnel dying between sessions.

Razorpay Dashboard → **Test Mode** → **Settings → Webhooks** → edit:

```
https://<your-app>.onrender.com/webhooks/razorpay
```

Same secret. Same four events: `payment_link.paid`, `payment.captured`,
`payment.failed`, `payment_link.expired`.

---

## 5 · Populate and verify

```bash
# against the deployed database
export DATABASE_URL='<neon string>'
.venv/bin/python -m backend.pipeline
```

Then check:

- `/health/deep` — database `ok`, razorpay `test`, llm configured
- `/lab` — works with no data at all; the best page to hand someone
- `/metrics` — figures present
- Approve one action and confirm a payment link comes back

---

## If something breaks

| symptom | cause |
|---|---|
| Frontend loads, all figures blank | `CORS_ORIGINS` doesn't match the Vercel URL exactly |
| Approve returns 401 | `NEXT_PUBLIC_API_KEY` ≠ backend `API_KEY` |
| `/health/deep` says database unreachable | Neon string missing `?sslmode=require` |
| First request takes 40s | free tier cold start — expected |
| Deploy "succeeds" but nothing responds | usually a port problem; this image honours `$PORT`, verified locally |
| Webhooks stop arriving | reconciliation still keeps the figures right — that is the point of it |

---

## Cost

Zero. Neon free tier holds ~9 MB against a 0.5 GB limit; Render's free web
service and Vercel's hobby plan both cover this comfortably. No card required
for any of the three.
