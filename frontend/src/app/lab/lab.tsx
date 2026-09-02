"use client";

import { useEffect, useState } from "react";
import {
  evaluateScenario, label, money,
  type LabLimits, type LabScenario, type LabVerdict,
} from "@/lib/api";

const TONE: Record<string, { bg: string; word: string }> = {
  AUTO_APPROVED: { bg: "bg-recovered", word: "The system acts on its own" },
  REQUIRES_APPROVAL: { bg: "bg-decide", word: "A human has to decide" },
  BLOCKED: { bg: "bg-risk text-paper", word: "Refused — no money moves" },
};

export function Lab({ data }: { data: LabLimits }) {
  const [scenario, setScenario] = useState<LabScenario>({ amount_minor: 93_900 });
  const [verdict, setVerdict] = useState<LabVerdict | null>(null);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState<string | null>(null);

  // Re-evaluate whenever anything changes — the point is watching the verdict
  // flip as you drag the amount past a limit, not pressing a submit button.
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    evaluateScenario(scenario)
      .then((v) => { if (!cancelled) setVerdict(v); })
      .catch(() => { if (!cancelled) setVerdict(null); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [scenario]);

  const cur = data.currency;
  const L = data.limits;
  const amount = scenario.amount_minor ?? 0;

  return (
    <div className="grid grow grid-cols-[minmax(0,1fr)_minmax(0,420px)] overflow-hidden">
      {/* controls */}
      <div className="border-r-4 border-ink px-8 py-7">
        <div className="mb-3 flex items-center gap-2.5">
          <span className="mono text-[11px] font-bold tracking-[.14em]">ONE CLICK</span>
          <span className="h-[3px] grow bg-ink" />
        </div>
        <div className="grid grid-cols-[repeat(auto-fill,minmax(230px,1fr))] gap-3">
          {data.presets.map((p) => (
            <button
              key={p.id}
              onClick={() => { setActive(p.id); setScenario({ amount_minor: 93_900, ...p.scenario }); }}
              className={`hard-sm border-[3px] border-ink px-4 py-3 text-left transition-transform active:translate-x-[3px] active:translate-y-[3px] active:shadow-none ${
                active === p.id ? "bg-judgement text-paper" : "bg-paper hover:bg-paper-deep"
              }`}
            >
              <div className="text-[14px] font-bold leading-tight">{p.label}</div>
              <div className={`mono mt-1.5 text-[10px] tracking-[.06em] ${active === p.id ? "text-paper/70" : "text-ink/55"}`}>
                expects {label(p.expect).toUpperCase()}
              </div>
            </button>
          ))}
        </div>

        <div className="mt-8 mb-3 flex items-center gap-2.5">
          <span className="mono text-[11px] font-bold tracking-[.14em]">OR CHANGE IT YOURSELF</span>
          <span className="h-[3px] grow bg-ink" />
        </div>

        <div className="flex flex-col gap-5">
          <div>
            <div className="flex items-baseline justify-between">
              <label className="mono text-[11.5px] font-bold">AMOUNT</label>
              <span className="disp tnum text-[26px]">{money(amount, cur)}</span>
            </div>
            <input
              type="range" min={0} max={1_500_000} step={5_000}
              value={amount}
              onChange={(e) => { setActive(null); setScenario((s) => ({ ...s, amount_minor: +e.target.value })); }}
              className="mt-2 w-full accent-machine"
            />
            <div className="mono mt-1 flex justify-between text-[10.5px] text-ink/55">
              <span>0</span>
              <span>auto limit {L.auto_limit}</span>
              <span>{money(1_500_000, cur)}</span>
            </div>
          </div>

          <div>
            <div className="flex items-baseline justify-between">
              <label className="mono text-[11.5px] font-bold">ALREADY RECOVERED TODAY</label>
              <span className="mono tnum text-[15px]">
                {money(scenario.already_recovered_today_minor ?? 0, cur)}
              </span>
            </div>
            <input
              type="range" min={0} max={L.daily_cap_minor} step={10_000}
              value={scenario.already_recovered_today_minor ?? 0}
              onChange={(e) => { setActive(null); setScenario((s) => ({ ...s, already_recovered_today_minor: +e.target.value })); }}
              className="mt-2 w-full accent-machine"
            />
            <div className="mono mt-1 text-[10.5px] text-ink/55">daily cap {L.daily_cap}</div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <Toggle
              on={!!scenario.customer_opted_out}
              label="Customer opted out"
              onClick={() => { setActive(null); setScenario((s) => ({ ...s, customer_opted_out: !s.customer_opted_out })); }}
            />
            <Toggle
              on={scenario.payment_status === "captured"}
              label="Already paid"
              onClick={() => { setActive(null); setScenario((s) => ({ ...s, payment_status: s.payment_status === "captured" ? "failed" : "captured" })); }}
            />
            <Toggle
              on={(scenario.prior_attempts ?? 0) >= L.max_retry_attempts}
              label={`${L.max_retry_attempts} attempts already`}
              onClick={() => { setActive(null); setScenario((s) => ({ ...s, prior_attempts: (s.prior_attempts ?? 0) >= L.max_retry_attempts ? 0 : L.max_retry_attempts })); }}
            />
            <Toggle
              on={(scenario.minutes_since_proposed ?? 0) > L.action_ttl_minutes}
              label="Approved hours ago"
              onClick={() => { setActive(null); setScenario((s) => ({ ...s, minutes_since_proposed: (s.minutes_since_proposed ?? 0) > L.action_ttl_minutes ? 0 : L.action_ttl_minutes * 3 })); }}
            />
          </div>
        </div>
      </div>

      {/* verdict */}
      <div className="relative flex flex-col overflow-hidden bg-paper-deep px-7 py-7">
        <div className="dots pointer-events-none absolute inset-0 opacity-[.055]" style={{ animationDuration: "44s" }} />
        <div className="relative mb-3 flex items-center gap-2.5">
          <span className="mono text-[11px] font-bold tracking-[.14em]">THE ENGINE SAYS</span>
          <span className="h-[3px] grow bg-ink" />
          {busy && <span className="mono text-[10px] text-ink/50">…</span>}
        </div>

        {verdict ? (
          <div className="hard relative border-4 border-ink bg-paper">
            <div className={`border-b-4 border-ink px-5 py-4 ${TONE[verdict.decision]?.bg ?? "bg-paper-deep"}`}>
              <div className="disp text-[clamp(22px,2.6vw,34px)] uppercase leading-none">
                {label(verdict.decision)}
              </div>
              <div className="mono mt-2 text-[11px] font-bold tracking-[.08em]">
                {TONE[verdict.decision]?.word}
              </div>
            </div>
            <div className="flex flex-col gap-3 p-5">
              <p className="text-[14px] leading-[1.5]">{verdict.reason}</p>
              <div className="flex flex-col gap-1.5 border-t-2 border-dotted border-ink pt-3">
                <Row k="RULE" v={label(verdict.rule)} />
                <Row k="AMOUNT" v={verdict.amount} />
                <Row k="MOVES MONEY" v={verdict.would_move_money ? "Yes" : "No"} />
                <Row k="POLICY" v={verdict.policy_version} last />
              </div>
              <div className="mono break-all text-[10px] text-ink/45">
                snapshot {verdict.policy_hash}
              </div>
            </div>
          </div>
        ) : (
          <div className="hard relative border-4 border-ink bg-paper p-5 text-[14px]">
            Waiting for the engine…
          </div>
        )}

        <p className="relative mt-4 text-[12px] leading-[1.5] text-ink/65">
          This is the real engine, not a mock — a simulator with its own copy of
          the rules would eventually disagree with production and reassure you
          wrongly. Nothing here writes anything or contacts Razorpay.
        </p>
      </div>
    </div>
  );
}

function Toggle({ on, label: text, onClick }: { on: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`hard-sm border-[3px] border-ink px-3 py-2.5 text-left text-[13px] font-bold transition-transform active:translate-x-[3px] active:translate-y-[3px] active:shadow-none ${
        on ? "bg-risk text-paper" : "bg-paper hover:bg-paper-deep"
      }`}
    >
      {text}
    </button>
  );
}

function Row({ k, v, last }: { k: string; v: string; last?: boolean }) {
  return (
    <div className={`flex justify-between ${last ? "" : "border-b-2 border-dotted border-ink/40 pb-1.5"}`}>
      <span className="mono text-[11px]">{k}</span>
      <span className="text-[12.5px] font-medium">{v}</span>
    </div>
  );
}
