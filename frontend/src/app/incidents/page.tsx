import Link from "next/link";
import { Header, DataNote, SectionRule } from "@/components/chrome";
import { AuditTicker } from "@/components/ticker";
import {
  getIncidents, getIncidentDetail, getActions, label, moneyShort,
  type Incident, type IncidentDetail, type RecoveryAction,
} from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Incidents({
  searchParams,
}: {
  searchParams: Promise<{ id?: string }>;
}) {
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

  if (!incidents.length) {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/incidents" />
        <p className="grow p-10 text-[15px]">No open incidents.</p>
      </div>
    );
  }

  const { id } = await searchParams;
  const selected = (id ? incidents.find((inc) => String(inc.id) === id) : undefined)
    ?? incidents[0];

  let detail: IncidentDetail | null = null;
  try {
    detail = await getIncidentDetail(selected.id);
  } catch {
    // fall through — the summary row still has enough to render a page
  }

  const inv = detail?.investigation;
  // Recovered actions aren't attributed to a specific incident on the action
  // row today, so this is the batch total, labelled honestly as such rather
  // than implied as this incident's own figure.
  const recovered = actions.filter((a) => a.recovered);
  const recoveredMinor = recovered.reduce((s, a) => s + a.recovered_minor, 0);

  const declined = inv?.is_transient === false;

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/incidents" />

      {/* incident switcher — click, never type a URL */}
      {incidents.length > 1 && (
        <div className="flex shrink-0 border-b-4 border-ink bg-ink">
          {incidents.map((inc) => {
            const isActive = inc.id === selected.id;
            return (
              <Link
                key={inc.id}
                href={`/incidents?id=${inc.id}`}
                className={`flex-1 border-r-2 border-ink/40 px-5 py-3 text-left transition-colors last:border-r-0 ${
                  isActive ? "bg-decide text-ink" : "text-paper/70 hover:bg-paper/10"
                }`}
              >
                <div className="mono text-[10px] font-bold tracking-[.1em]">
                  INCIDENT {String(inc.id).padStart(2, "0")} · {label(inc.status).toUpperCase()}
                </div>
                <div className="disp mt-0.5 truncate text-[15px] uppercase">{inc.title}</div>
              </Link>
            );
          })}
        </div>
      )}

      <section
        className={`relative overflow-hidden border-b-4 border-ink px-8 py-7 ${
          declined ? "bg-machine text-paper" : "bg-risk"
        }`}
      >
        <div className="stripe pointer-events-none absolute inset-0 opacity-40" style={{ animationDuration: "31s" }} />
        <div className="relative">
          <span className="mono inline-block -rotate-1 bg-ink px-2.5 py-[5px] text-[11px] font-bold tracking-[.12em] text-paper">
            {label(selected.status).toUpperCase()}
          </span>
          <h1 className="disp mt-3 text-[clamp(28px,3.8vw,50px)] uppercase">{selected.title}</h1>
          <div className="mt-4 flex flex-wrap gap-9">
            <Fig v={moneyShort(selected.revenue_at_risk_minor)} k="AT RISK" />
            <Fig v={String(selected.affected_count)} k="AFFECTED" />
            <Fig v={inv ? `${inv.confidence.toFixed(2)}` : "—"} k="CONFIDENCE" />
            <Fig v={declined ? "NO" : inv ? "YES" : "—"} k="WORTH RECOVERING" />
          </div>
        </div>
      </section>

      <section className="grid grow grid-cols-[1.4fr_1fr] overflow-hidden">
        <div className="border-r-4 border-ink px-8 py-7">
          <SectionRule
            left="WHAT THE INVESTIGATION FOUND"
            right={inv ? `${inv.tool_calls ?? "?"} TOOL CALLS` : undefined}
          />
          <p className="max-w-[70ch] text-[16px] leading-[1.6]">
            {inv?.root_cause ?? "Investigation pending."}
          </p>

          {declined && (
            <div className="stripe mt-5 max-w-[70ch] border-[3px] border-ink bg-decide px-4 py-3">
              <p className="text-[13.5px] font-bold leading-[1.5]">
                The agent judged this NOT worth recovering. Retrying these
                payments would fail again for the same reason and would only
                irritate customers who already know they can&rsquo;t pay.
              </p>
            </div>
          )}

          {inv?.evidence && inv.evidence.length > 0 && (
            <div className="mt-8">
              <SectionRule left="EVIDENCE" />
              <div className="flex flex-col gap-2">
                {inv.evidence.map((line, i) => (
                  <div key={i} className="flex gap-3 border-b-2 border-dotted border-ink/30 pb-2">
                    <span className="mono shrink-0 text-[11px] text-ink/45">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="text-[13.5px] leading-[1.5]">{line}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {inv?.recommended_focus && (
            <div className="mt-6">
              <SectionRule left={declined ? "WHY NOT" : "RECOMMENDED FOCUS"} />
              <p className="max-w-[70ch] text-[13.5px] leading-[1.6] text-ink/80">
                {inv.recommended_focus}
              </p>
            </div>
          )}

          <p className="mono mt-8 max-w-[70ch] text-[11.5px] leading-[1.6] text-ink/60">
            The agent held six read-only tools. It has no method that can move
            money, approve, or reach Razorpay — that capability does not exist
            in its graph.
          </p>
        </div>

        <div className="relative overflow-hidden bg-paper-deep px-7 py-7">
          <div className="dots pointer-events-none absolute inset-0 opacity-[.055]" style={{ animationDuration: "44s" }} />
          <div className="relative">
            <SectionRule left="RECOVERED THIS BATCH" right={`${recovered.length}`} />
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
            {recovered.length > 0 && (
              <p className="mono mt-4 text-[11px] text-ink/50">
                {moneyShort(recoveredMinor)} recovered across the whole batch —
                not broken out per incident yet.
              </p>
            )}
          </div>
        </div>
      </section>

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
