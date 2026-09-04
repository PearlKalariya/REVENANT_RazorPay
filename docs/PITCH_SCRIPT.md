# REVENANT — 5-Minute Pitch Script

**How to read this file:** every line of plain text is what you SAY, in order,
continuously, like a script — read top to bottom, no jumping around.

> 🎬 **Blockquoted lines starting with 🎬 are stage directions — SCREEN
> ACTIONS ONLY. Never speak these out loud.** They tell you what to click,
> where to be, and how long to hold before you talk again.

Total runtime target: **5:00**. Timestamps are guides, not a stopwatch you
narrate — just keep pace.

Print this or keep it open on a second screen. Never on the recording screen.

---

## 0:00 — 0:25 · Cold open

> 🎬 Dashboard (`/`) is already loaded, full screen. Start recording, wait 2
> seconds in silence — don't speak yet, don't move the mouse. Then begin.

This is REVENANT. One lakh, five thousand, four hundred and eighty-seven
rupees in failed payments, detected automatically. Three thousand, four
hundred and forty-eight of it recovered — verified by real Razorpay
webhooks, not a claim I'm making. Here's how, and here's what broke while I
built it.

> 🎬 As you say "verified," point the cursor at the RECOVERED figure and hold
> it there for one second. Don't click anything yet.
>
> 🎬 **"Here's how, and here's what broke while I built it" — this is a
> hook line, not an instruction to show anything new.** "How" = the entire
> rest of the video. "What broke" = the reconciliation story you'll tell at
> 2:50. Nothing to click here. Keep the cursor still on the dashboard, let
> the sentence land, THEN move to the next section.

---

## 0:25 — 1:10 · The problem

Revenue loss isn't one clean failure. A payment times out, and nobody notices
until the merchant checks their dashboard days later. By then the customer's
moved on. REVENANT closes that loop: detect the failure while it's happening,
figure out why, decide what to do about it, and only act inside limits a human
set.

> 🎬 Click **INCIDENTS** in the nav. Let the page load fully before speaking
> the next line. Point at the "83.3% · 2.0% · 41.3×" stat blocks as you say
> the next sentence.

Eighty-three percent of UPI payments failing against a two percent baseline.
Forty-one times normal. That's not noise, that's a real incident, and the
system found it on its own.

---

## 1:10 — 2:00 · The AI, and its leash

An agent investigates — reads the failure reasons, the customer history, the
merchant baseline — and proposes a recovery strategy. But it can't execute
anything. It has six read-only tools. No method that touches money exists in
its code anywhere. Here's the part I'm proudest of.

> 🎬 Navigate to the SECOND incident on the page — the card-decline cluster,
> not the UPI one. Scroll or click until "worth_recovering: false" or the
> declined status is visible on screen. Point at it.

This incident looks identical to the first one on any normal dashboard — a
payment method failing way above baseline. But the agent read the actual
failure reasons underneath: expired cards, insufficient funds. Structural, not
transient. It declined to act. "Do not recover this" is a harder answer than
"recover everything," and it's the one that proves the AI is reasoning, not
just pattern-matching a spike.

---

## 2:00 — 2:50 · The policy gate

Every proposed action — whether the AI thinks it's a good idea or not — goes
through one deterministic gate before anything happens.

> 🎬 Click **TRY IT** in the nav. Wait for the page to fully render. Grab the
> AMOUNT slider with the mouse.

> 🎬 **Slowly** drag the slider up past ₹5,000. Pause the instant the verdict
> card flips from green (AUTO-APPROVED) to yellow (NEEDS APPROVAL). Let it
> sit on screen for a full second before you speak again.

Under five thousand rupees, auto-approved. One rupee over, it stops and waits
for a human. This isn't a mock of the policy engine — it's calling the exact
same function the real executor calls right before money moves. I can't fake
this being safe. It either is, or the button breaks in front of you.

> 🎬 Click the **"Too much to decide alone"** preset chip. Let the verdict
> card update on screen before moving on.

---

## 2:50 — 3:40 · The breakthrough — two real failures, caught and fixed

The strongest thing I can show you isn't a feature — it's a bug that actually
happened, twice. Webhooks are how the system learns a customer paid. Twice
during this build, my tunnel died, and Razorpay had nowhere to send the
notification. Real money moved. My system would have reported zero.

> 🎬 Click **AUDIT** in the nav. Scroll until you find an entry reading
> "Confirmed paid by Razorpay" — hold the cursor on that row for a beat.

So the outcome engine doesn't just wait for a push — it also pulls, asking
Razorpay directly for anything unconfirmed. It found both incidents on its
own, no manual fix. And it refuses to count a replayed event as revenue, even
one signed by the system itself — because a product that can inflate its own
headline number isn't trustworthy, and I built the boundary so it structurally
can't.

---

## 3:40 — 4:20 · The second bug — money math itself

I also found a bug where the daily spending cap summed every merchant's
recoveries together — one merchant could exhaust another's budget without
either knowing. And a race condition where two approvals fired at the same
instant could both pass the cap check before either one recorded. Fixed the
second one with a database-level lock — the same lock two concurrent requests
actually contend for, not a comment claiming it's fine.

> 🎬 Click **APPROVALS**. Point at one of the four pending cards — read its
> amount and policy reason out loud as you gesture at it, don't click yet.

Four decisions sitting here right now, all above the five-thousand-rupee
limit, all waiting on a human. This is the queue, live.

> 🎬 Type your name in "Approving as." Click **APPROVE** on one card. Whatever
> the response says, keep talking over it — don't stop to read it on camera.

Approving doesn't move money by itself. It authorizes. Policy runs one more
time, right now, against current state, before anything touches Razorpay —
because a decision made an hour ago isn't automatically still valid.

> 🎬 Click **RECOVERY** in the nav. Find a row with status "Recovered" and a
> real `rzp.io` link. Click the link once to show it opens a genuine Razorpay
> checkout page, then click back.

This one already went all the way through — link sent, customer paid, webhook
confirmed. That's a real Razorpay checkout page, not a screenshot.

---

## 4:20 — 4:50 · Close on the audit

Every one of those decisions — approved, blocked, declined, refused — is on
the record with the policy version and a hash of the exact rules that were
live when it fired. That's not a feature I bolted on for the demo. It's the
reason I trust the number I opened with.

> 🎬 Click **AUDIT** again. Let the ticker scroll along the bottom. Stay
> completely silent for 3 full seconds — just let it play.

---

## 4:50 — 5:00 · Out

REVENANT. AI decides, policy controls, Razorpay executes, and every step
proves itself. Thanks.

> 🎬 Hold on the dashboard for one silent second, then stop the recording.

---

## If you're running short on time

Cut the **3:40–4:20** section (the second bug) down to one sentence folded
into the close, or drop it entirely. Protect these three beats no matter what
— they're doing the most work:

1. **1:10–2:00** — the AI declining an incident
2. **2:00–2:50** — the policy gate flipping live on the slider
3. **2:50–3:40** — the reconciliation story

## Before you record — checklist

- [ ] Browser zoomed to 100%
- [ ] Only the browser window visible — no desktop clutter, no notifications
- [ ] Two tabs pre-loaded: dashboard, and Incidents scrolled to the second (card) incident
- [ ] Confirmed at least one action is sitting in Approvals right now
- [ ] This script open on a second screen or phone — never on the recording screen
- [ ] Do one full silent run-through of the clicks before recording audio, so nothing is a surprise live
