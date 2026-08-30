"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { decide, label, type RecoveryAction } from "@/lib/api";

/**
 * The only screen that writes.
 *
 * Approving does NOT execute — it authorises. Policy is evaluated again against
 * current state immediately before money moves and can still refuse: a
 * tightened daily cap, a customer who opted out since, a payment already
 * settled. The UI says that rather than implying the money has moved.
 */
export function ApprovalQueue({ initial }: { initial: RecoveryAction[] }) {
  const router = useRouter();
  const [pending, setPending] = useState(initial);
  const [approver, setApprover] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ approved: boolean } | null>(null);
  const [, startTransition] = useTransition();

  if (!pending.length) {
    return (
      <div className="hard border-4 border-ink bg-paper p-8">
        <p className="max-w-[60ch] text-[15px] leading-[1.55]">
          Nothing needs your decision. Every recovery in this batch was inside the
          merchant&rsquo;s policy limits and executed autonomously.
        </p>
      </div>
    );
  }

  async function submit(action: RecoveryAction, approved: boolean) {
    if (!approver.trim()) {
      setError("Enter your name first — an approval has to be attributable.");
      return;
    }
    setError(null);
    setBusy(action.id);
    try {
      await decide(action.id, approved, approver.trim());
      setPending((q) => q.filter((a) => a.id !== action.id));
      setDone({ approved });
      startTransition(() => router.refresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decision failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <label className="mono text-[11px] font-bold tracking-[.12em]">APPROVING AS</label>
        <input
          value={approver}
          onChange={(e) => setApprover(e.target.value)}
          placeholder="your name"
          className="mono border-[3px] border-ink bg-paper px-3 py-2 text-[13px] outline-none focus:bg-decide"
        />
        <span className="max-w-[54ch] text-[12px] text-ink/60">
          Recorded against every decision — an audit trail that cannot name who
          approved a payment is not an audit trail.
        </span>
      </div>

      {error && (
        <div className="hard-sm mb-5 border-[3px] border-ink bg-risk px-4 py-3 text-[13.5px] font-bold">
          {error}
        </div>
      )}
      {done && (
        <div className="hard-sm mb-5 border-[3px] border-ink bg-recovered px-4 py-3 text-[13.5px]">
          <strong>{done.approved ? "Approved." : "Denied."}</strong>{" "}
          {done.approved
            ? "Authorised — policy runs again before money moves."
            : "No money will move for this payment."}
        </div>
      )}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-5">
        {pending.map((a) => (
          <article key={a.id} className="hard flex flex-col gap-3.5 border-4 border-ink bg-paper p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="disp tnum leading-[.95] text-[clamp(30px,3vw,44px)]">
                  ₹{Math.round(a.amount_paise / 100).toLocaleString("en-IN")}
                </div>
                <div className="mono mt-1.5 text-[11px]">
                  {a.payment_id} · {label(a.customer_id)}
                </div>
              </div>
              {a.recovery_score !== null && (
                <span className="mono inline-block rotate-2 whitespace-nowrap bg-judgement px-2 py-1 text-[10px] font-bold text-paper">
                  {a.recovery_score.toFixed(3)} SCORE
                </span>
              )}
            </div>

            <div className="stripe border-[3px] border-ink bg-decide px-3 py-2.5">
              <p className="text-[13px] font-bold leading-[1.4]">{a.rationale}</p>
            </div>

            <div className="flex flex-col gap-1.5">
              <Row k="POLICY" v={label(a.policy.result)} />
              <Row k="RULE" v={label(a.policy.rule)} />
              <Row k="ACTION" v={label(a.action)} last />
            </div>

            <div className="flex gap-2.5">
              <button
                onClick={() => submit(a, true)}
                disabled={busy === a.id}
                className="hard-sm grow border-[3px] border-ink bg-recovered py-3 text-[15px] font-bold transition-transform active:translate-x-[3px] active:translate-y-[3px] active:shadow-none disabled:opacity-50"
              >
                {busy === a.id ? "…" : "APPROVE"}
              </button>
              <button
                onClick={() => submit(a, false)}
                disabled={busy === a.id}
                className="hard-sm grow border-[3px] border-ink bg-paper py-3 text-[15px] font-bold transition-transform active:translate-x-[3px] active:translate-y-[3px] active:shadow-none disabled:opacity-50"
              >
                DENY
              </button>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}

function Row({ k, v, last }: { k: string; v: string; last?: boolean }) {
  return (
    <div className={`flex justify-between ${last ? "" : "border-b-2 border-dotted border-ink pb-1"}`}>
      <span className="mono text-[11px]">{k}</span>
      <span className="text-[12.5px] font-medium">{v}</span>
    </div>
  );
}
