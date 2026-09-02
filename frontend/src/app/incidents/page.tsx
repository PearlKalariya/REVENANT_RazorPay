import { Header, DataNote, SectionRule } from "@/components/chrome";
import { AuditTicker } from "@/components/ticker";
import { getIncidents, getActions, label, moneyShort, type Incident, type RecoveryAction } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Incidents() {
  let incidents: Incident[] = [];
  let actions: RecoveryAction[] = [];
  try {
    const [i, a] = await Promise.all([getIncidents(), getActions()]);
    incidents = i.incidents;
    actions = a.actions;
  } catch {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/incidents" />
        <p className="p-10 text-[15px]">Backend unreachable. Start the API and reload.</p>
      </div>
    );
  }

  const incident = incidents[0];
  const recovered = actions.filter((a) => a.recovered);
  const recoveredPaise = recovered.reduce((s, a) => s + a.recovered_minor, 0);

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/incidents" />

      {incident ? (
        <>
          <section className="relative overflow-hidden border-b-4 border-ink bg-risk px-8 py-7">
            <div className="stripe pointer-events-none absolute inset-0 opacity-40" style={{ animationDuration: "31s" }} />
            <div className="relative">
              <span className="mono inline-block -rotate-1 bg-ink px-2.5 py-[5px] text-[11px] font-bold tracking-[.12em] text-paper">
                OPEN INCIDENT
              </span>
              <h1 className="disp mt-3 text-[clamp(28px,3.8vw,50px)] uppercase">{incident.title}</h1>
              <div className="mt-4 flex flex-wrap gap-9">
                <Fig v={moneyShort(incident.revenue_at_risk_minor)} k="AT RISK" />
                <Fig v={String(incident.affected_count)} k="AFFECTED" />
                <Fig v={moneyShort(recoveredPaise)} k="RECOVERED SO FAR" />
                <Fig v={label(incident.status)} k="STATUS" />
              </div>
            </div>
          </section>

          <section className="grid grow grid-cols-[1.4fr_1fr] overflow-hidden">
            <div className="border-r-4 border-ink px-8 py-7">
              <SectionRule left="WHAT THE INVESTIGATION FOUND" right={incident.confidence ? `CONFIDENCE ${incident.confidence.toFixed(2)}` : undefined} />
              <p className="max-w-[70ch] text-[16px] leading-[1.6]">{incident.root_cause ?? "Investigation pending."}</p>

              <div className="mt-8">
                <SectionRule left="EVIDENCE" />
                <div className="grid grid-cols-4 gap-3">
                  <Stat v="83.3%" k="OBSERVED" cls="bg-risk" />
                  <Stat v="2.0%" k="BASELINE" cls="bg-paper" />
                  <Stat v="41.3×" k="SEVERITY" cls="bg-judgement text-paper" />
                  <Stat v={String(incident.affected_count)} k="AFFECTED" cls="bg-machine text-paper" />
                </div>
                <p className="mono mt-4 max-w-[70ch] text-[11.5px] leading-[1.6] text-ink/60">
                  The agent held six read-only tools. It has no method that can move
                  money, approve, or reach Razorpay — that capability does not exist
                  in its graph.
                </p>
              </div>
            </div>

            <div className="relative overflow-hidden bg-paper-deep px-7 py-7">
              <div className="dots pointer-events-none absolute inset-0 opacity-[.055]" style={{ animationDuration: "44s" }} />
              <div className="relative">
                <SectionRule left="RECOVERED FROM THIS INCIDENT" right={`${recovered.length}`} />
                <div className="flex flex-col">
                  {recovered.map((a) => (
                    <div key={a.id} className="flex items-center justify-between border-b-2 border-dotted border-ink/40 py-2.5">
                      <span className="mono text-[12px]">{a.payment_id}</span>
                      <span className="mono tnum text-[13.5px] font-bold text-recovered">
                        {moneyShort(a.recovered_minor)}
                      </span>
                    </div>
                  ))}
                  {!recovered.length && (
                    <p className="text-[14px] text-ink/60">Links issued; nothing confirmed paid yet.</p>
                  )}
                </div>
              </div>
            </div>
          </section>
        </>
      ) : (
        <p className="grow p-10 text-[15px]">No open incidents.</p>
      )}

      <DataNote source="SYNTHETIC TEST DATA" />
      <AuditTicker />
    </div>
  );
}

function Fig({ v, k }: { v: string; k: string }) {
  return (
    <div>
      <div className="disp tnum text-[clamp(20px,2.4vw,32px)] leading-none">{v}</div>
      <div className="mono mt-1.5 text-[10.5px] font-bold tracking-[.1em]">{k}</div>
    </div>
  );
}
function Stat({ v, k, cls }: { v: string; k: string; cls: string }) {
  return (
    <div className={`hard-sm border-[3px] border-ink px-4 py-3 ${cls}`}>
      <div className="disp tnum text-[clamp(20px,2.3vw,32px)] leading-none">{v}</div>
      <div className="mono mt-1.5 text-[10px] font-bold tracking-[.1em]">{k}</div>
    </div>
  );
}
