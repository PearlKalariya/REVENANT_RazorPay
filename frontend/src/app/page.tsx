import Link from "next/link";
import { Header, DataNote, SectionRule } from "@/components/chrome";
import { AuditTicker } from "@/components/ticker";
import {
  getMetrics, getIncidents, getActions,
  label, rupees, rupeesShort,
  type Metrics, type Incident, type RecoveryAction,
} from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  let metrics: Metrics | null = null;
  let incident: Incident | null = null;
  let awaiting: RecoveryAction[] = [];
  let incidentCount = 0;

  try {
    const [m, inc, act] = await Promise.all([
      getMetrics(), getIncidents(), getActions("awaiting_approval"),
    ]);
    metrics = m;
    incident = inc.incidents[0] ?? null;
    incidentCount = inc.incidents.length;
    awaiting = act.actions;
  } catch {
    return <Offline />;
  }

  const attempted = metrics.recovery_attempted_paise;
  const recovered = metrics.recovered_paise;
  const remainder = Math.max(0, attempted - recovered);
  const rate = metrics.recovery_rate_of_attempted;

  const byStatus = metrics.actions_by_status;
  const auto = (byStatus.approved?.count ?? 0) + (byStatus.executed?.count ?? 0);
  const needsApproval = byStatus.awaiting_approval?.count ?? 0;
  const blocked = byStatus.denied?.count ?? 0;
  const heldPaise = byStatus.awaiting_approval?.paise ?? 0;

  const next = awaiting[0];

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/" />

      {/* ── hero band ─────────────────────────────────────────── */}
      <section className="grid shrink-0 grid-cols-[1.62fr_1fr] border-b-4 border-ink">
        <div className="relative overflow-hidden border-r-4 border-ink bg-recovered px-[30px] pt-6 pb-[22px]">
          <div className="dots pointer-events-none absolute inset-0 opacity-[.13]" />
          <div
            className="pointer-events-none absolute inset-y-0 w-[26%] bg-gradient-to-r from-transparent via-white/30 to-transparent"
            style={{ animation: "sheen 17s ease-in-out infinite" }}
          />
          <div className="relative">
            <div className="mb-2.5 flex items-center gap-[11px]">
              <span className="mono bg-ink px-[9px] py-1 text-[11px] font-bold tracking-[.16em] text-recovered">
                RECOVERED · VERIFIED
              </span>
              <span className="mono text-[11px] tracking-[.1em]">
                {metrics.recovered_count} OUTCOMES
              </span>
            </div>
            <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
              <span className="disp tnum leading-[.86] text-[clamp(44px,6.6vw,92px)]">{rupeesShort(recovered)}</span>
              {rate !== null && (
                <span className="disp inline-block -rotate-2 whitespace-nowrap bg-ink px-[13px] py-0.5 text-decide text-[clamp(20px,2.9vw,40px)]">
                  {(rate * 100).toFixed(1)}%
                </span>
              )}
            </div>
            <div className="mt-5 flex h-5 border-[3px] border-ink bg-paper">
              <div className="relative overflow-hidden bg-machine" style={{ flexGrow: recovered || 1 }}>
                <div
                  className="absolute inset-0 bg-gradient-to-r from-transparent via-white to-transparent"
                  style={{ animation: "shimmer 9s ease-in-out infinite" }}
                />
              </div>
              <div className="stripe" style={{ flexGrow: remainder || 0.0001, animationDuration: "19s" }} />
            </div>
            <div className="mt-2 flex justify-between">
              <span className="mono text-[11.5px] font-bold">{rupees(recovered)} RECOVERED</span>
              <span className="mono text-[11.5px]">
                {rupees(attempted)} ATTEMPTED · {metrics.payment_links_issued} LINKS
              </span>
            </div>
          </div>
        </div>

        <div className="flex flex-col">
          <div className="relative overflow-hidden border-b-4 border-ink bg-risk px-[26px] py-[18px]">
            <div className="stripe pointer-events-none absolute inset-0 opacity-50" style={{ animationDuration: "31s" }} />
            <div className="relative">
              <span className="mono text-[11px] font-bold tracking-[.16em]">REVENUE AT RISK</span>
              <div className="disp tnum mt-[7px] leading-none text-[clamp(28px,3.4vw,46px)]">
                {rupeesShort(metrics.revenue_at_risk_paise)}
              </div>
              <div className="mono mt-[5px] text-[11px]">
                {incident?.affected_count ?? 0} FAILED PAYMENTS
              </div>
            </div>
          </div>
          <Link
            href="/approvals"
            className="relative flex grow flex-col justify-center overflow-hidden bg-decide px-[26px] py-[18px] transition-[filter] hover:brightness-95"
          >
            <div
              className="dots pointer-events-none absolute inset-0 opacity-[.1]"
              style={{ animation: "drift-dots-rev 34s linear infinite" }}
            />
            <div className="relative">
              <span className="mono text-[11px] font-bold tracking-[.16em]">WAITING ON YOU</span>
              <div className="mt-[7px] flex items-baseline gap-3.5">
                <span className="disp leading-none text-[clamp(28px,3.4vw,46px)]">{needsApproval}</span>
                <span className="disp tnum text-[clamp(15px,1.7vw,22px)]">{rupeesShort(heldPaise)}</span>
              </div>
              <div className="mono mt-[5px] text-[11px]">ABOVE THE ₹5,000 AUTO LIMIT</div>
            </div>
          </Link>
        </div>
      </section>

      {/* ── middle ────────────────────────────────────────────── */}
      <section className="grid grow grid-cols-[1.62fr_1fr] overflow-hidden">
        <div className="flex flex-col gap-5 border-r-4 border-ink px-[30px] py-[22px]">
          {incident ? (
            <>
              <div>
                <div className="mb-[11px] flex flex-wrap items-center gap-3">
                  <span className="mono inline-block -rotate-1 bg-judgement px-2.5 py-[5px] text-[11px] font-bold tracking-[.12em] text-paper">
                    {incidentCount > 1 ? `INCIDENT 01 OF ${incidentCount}` : "OPEN INCIDENT"}
                  </span>
                  <Link href={`/incidents`} className="disp uppercase hover:underline text-[clamp(19px,2.1vw,27px)]">
                    {incident.title}
                  </Link>
                </div>
                <p className="max-w-[66ch] text-[14.5px] leading-[1.5]">
                  {incident.root_cause ?? "Investigation pending."}
                </p>
              </div>

              <div className="grid grid-cols-4 gap-[13px]">
                <Stat value="83.3%" caption="OBSERVED" className="bg-risk" />
                <Stat value="2.0%" caption="BASELINE" className="bg-paper" />
                <Stat value="41.3×" caption="SEVERITY" className="bg-judgement text-paper" />
                <Stat value={String(incident.affected_count)} caption="AFFECTED" className="bg-machine text-paper" />
              </div>
            </>
          ) : (
            <p className="text-[15px] text-ink/60">No open incidents. Nothing at risk right now.</p>
          )}

          <div>
            <SectionRule left="WHAT POLICY DECIDED" right={`${auto + needsApproval + blocked} CANDIDATES`} />
            <div className="flex items-stretch gap-3">
              <Disposition n={auto} caption="AUTO-APPROVED" className="bg-machine text-paper" grow={auto} />
              <Disposition n={needsApproval} caption="NEEDS APPROVAL" className="bg-decide" grow={Math.max(needsApproval, 1)} />
              <Disposition n={blocked} caption="BLOCKED" className="stripe bg-paper" grow={Math.max(blocked, 1)} />
            </div>
            {Object.keys(metrics.policy_blocks).length > 0 && (
              <p className="mono mt-3 text-[11px] text-ink/55">
                Blocked because:{" "}
                {Object.entries(metrics.policy_blocks)
                  .map(([rule, n]) => `${label(rule).toLowerCase()} (${n})`)
                  .join(" · ")}
              </p>
            )}
          </div>
        </div>

        {/* decision column */}
        <div className="relative flex flex-col overflow-hidden bg-paper-deep px-[26px] py-[22px]">
          <div className="dots pointer-events-none absolute inset-0 opacity-[.055]" style={{ animationDuration: "44s" }} />
          <div className="relative">
            <SectionRule left="DECIDE" right={awaiting.length ? `1 / ${awaiting.length}` : undefined} />
          </div>

          {next ? (
            <>
              <div className="hard relative flex flex-col gap-3.5 border-4 border-ink bg-paper p-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="disp tnum leading-[.95] text-[clamp(30px,3.4vw,47px)]">{rupeesShort(next.amount_paise)}</div>
                    <div className="mono mt-[7px] text-[11px]">
                      {next.payment_id} · {label(next.customer_id)}
                    </div>
                  </div>
                  {next.recovery_score !== null && (
                    <span className="mono inline-block rotate-2 whitespace-nowrap bg-judgement px-[9px] py-[5px] text-[10px] font-bold tracking-[.08em] text-paper">
                      {next.recovery_score.toFixed(3)} SCORE
                    </span>
                  )}
                </div>

                <div className="stripe border-[3px] border-ink bg-decide px-[13px] py-[11px]">
                  <p className="text-[13.5px] font-bold leading-[1.4]">
                    {next.rationale ?? "Exceeds the autonomous limit."}
                  </p>
                </div>

                <div className="flex flex-col gap-2">
                  <Row k="POLICY" v={label(next.policy.result)} tone="text-risk" />
                  <Row k="RULE" v={label(next.policy.rule)} />
                  <Row k="ACTION" v={label(next.action)} last />
                </div>

                <Link
                  href="/approvals"
                  className="hard-sm border-[3px] border-ink bg-recovered py-3 text-center text-[16px] font-bold"
                >
                  REVIEW & DECIDE
                </Link>

                <p className="text-[11.5px] leading-[1.45]">
                  Approving <strong>authorises</strong> execution. Policy runs again
                  immediately before money moves — and may still refuse.
                </p>
              </div>

              <div className="relative mt-3.5 flex gap-[7px]">
                {awaiting.slice(0, 5).map((_, i) => (
                  <span
                    key={i}
                    className={`h-1.5 grow ${i === 0 ? "bg-ink" : "border-2 border-ink bg-paper"}`}
                  />
                ))}
              </div>
            </>
          ) : (
            <div className="hard relative border-4 border-ink bg-paper p-5">
              <p className="text-[15px] leading-[1.5]">
                Nothing needs your decision. Every recovery in this batch was inside
                the merchant&rsquo;s policy limits.
              </p>
            </div>
          )}
        </div>
      </section>

      <DataNote source={metrics.data_source} />
      <AuditTicker />
    </div>
  );
}

function Stat({ value, caption, className }: { value: string; caption: string; className: string }) {
  return (
    <div className={`hard-sm border-[3px] border-ink px-[15px] py-[13px] ${className}`}>
      <div className="disp tnum leading-none text-[clamp(21px,2.4vw,33px)]">{value}</div>
      <div className="mono mt-1.5 text-[10px] font-bold tracking-[.1em]">{caption}</div>
    </div>
  );
}

function Disposition({ n, caption, className, grow }: { n: number; caption: string; className: string; grow: number }) {
  return (
    <div className={`hard border-[3px] border-ink px-[18px] py-4 ${className}`} style={{ flexGrow: grow }}>
      <div className="disp tnum leading-none text-[clamp(25px,2.9vw,40px)]">{n}</div>
      <div className="mono mt-2 text-[10.5px] font-bold tracking-[.1em]">{caption}</div>
    </div>
  );
}

function Row({ k, v, tone, last }: { k: string; v: string; tone?: string; last?: boolean }) {
  return (
    <div className={`flex justify-between ${last ? "" : "border-b-2 border-dotted border-ink pb-1.5"}`}>
      <span className="mono text-[11.5px]">{k}</span>
      <span className={`text-[13px] font-medium ${tone ?? ""}`}>{v}</span>
    </div>
  );
}

function Offline() {
  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/" />
      <div className="flex grow items-center justify-center p-10">
        <div className="hard max-w-[52ch] border-4 border-ink bg-paper p-8">
          <div className="disp mb-3 text-[28px]">BACKEND UNREACHABLE</div>
          <p className="text-[15px] leading-[1.55]">
            The dashboard shows nothing rather than showing figures it cannot
            verify. Start the API and reload.
          </p>
          <pre className="mono mt-4 border-2 border-ink bg-paper-deep p-3 text-[12px]">
uvicorn backend.main:app --port 8000</pre>
        </div>
      </div>
    </div>
  );
}
