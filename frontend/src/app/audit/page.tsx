import { Header, DataNote, SectionRule } from "@/components/chrome";
import { getAudit, label, money, clockIST, type AuditEvent } from "@/lib/api";

export const dynamic = "force-dynamic";

const TONE: Record<string, string> = {
  REVENUE_RECOVERED: "bg-recovered",
  PAYMENT_LINK_CREATED: "bg-decide",
  APPROVAL_GRANTED: "bg-recovered",
  APPROVAL_DENIED: "bg-risk text-paper",
  EXECUTION_BLOCKED_AT_RUNTIME: "bg-risk text-paper",
  EXECUTION_FAILED: "bg-risk text-paper",
  OUTCOME_IGNORED_UNTRUSTED_SOURCE: "bg-risk text-paper",
  POLICY_EVALUATED: "bg-machine text-paper",
  ROOT_CAUSE_IDENTIFIED: "bg-judgement text-paper",
  RECOVERY_STRATEGY_PROPOSED: "bg-judgement text-paper",
  REVENUE_INCIDENT_DETECTED: "bg-paper-deep",
};

export default async function Audit() {
  let events: AuditEvent[] = [];
  try {
    events = (await getAudit(120)).events;
  } catch {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/audit" />
        <p className="p-10 text-[15px]">Backend unreachable. Start the API and reload.</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/audit" />

      <section className="relative overflow-hidden border-b-4 border-ink bg-ink px-8 py-7 text-paper">
        <div className="dots pointer-events-none absolute inset-0 opacity-[.14] invert" />
        <div className="relative">
          <h1 className="disp text-[clamp(26px,3.2vw,42px)] uppercase">Every decision, on the record</h1>
          <p className="mt-3 max-w-[76ch] text-[14.5px] leading-[1.55] text-paper/75">
            Detection, investigation, policy, execution, outcome — including the
            refusals. Money that did <em>not</em> move is as traceable as money
            that did.
          </p>
        </div>
      </section>

      <section className="grow px-8 py-7">
        <SectionRule left="AUDIT TRAIL" right={`${events.length} EVENTS · NEWEST FIRST`} />
        <div className="flex flex-col">
          {events.map((e, i) => (
            <div
              key={i}
              className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b-2 border-dotted border-ink/35 py-2.5"
            >
              <span className="mono w-[74px] shrink-0 text-[12px] text-ink/55">{clockIST(e.ts)}</span>
              <span className="mono w-[150px] shrink-0 text-[11.5px] font-bold tracking-[.05em]">
                {label(e.actor)}
              </span>
              <span className={`mono shrink-0 px-2 py-0.5 text-[11px] font-bold ${TONE[e.event_type] ?? "bg-paper-deep"}`}>
                {label(e.event_type)}
              </span>
              {e.amount_minor !== null && (
                <span className="mono tnum shrink-0 text-[13px] font-bold">{money(e.amount_minor)}</span>
              )}
              {e.payment_id && <span className="mono text-[11.5px] text-ink/55">{e.payment_id}</span>}
              {e.reason && <span className="text-[13px] text-ink/80">{e.reason}</span>}
              {e.error && <span className="text-[13px] text-risk">{e.error}</span>}
            </div>
          ))}
        </div>
      </section>

      <DataNote source="SYNTHETIC TEST DATA" />
    </div>
  );
}
