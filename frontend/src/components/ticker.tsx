import { getAudit, label, clockIST, money } from "@/lib/api";

const TONE: Record<string, string> = {
  REVENUE_RECOVERED: "text-recovered",
  PAYMENT_LINK_CREATED: "text-decide",
  EXECUTION_BLOCKED_AT_RUNTIME: "text-risk",
  EXECUTION_FAILED: "text-risk",
  APPROVAL_DENIED: "text-risk",
  OUTCOME_IGNORED_UNTRUSTED_SOURCE: "text-risk",
};

export async function AuditTicker() {
  let events: Awaited<ReturnType<typeof getAudit>>["events"] = [];
  try {
    events = (await getAudit(14)).events;
  } catch {
    // A dead backend must not blank the page; the strip just stays quiet.
    return null;
  }
  if (!events.length) return null;

  const strip = events.map((e, i) => (
    <span key={i} className="mono px-[26px] text-[12px]">
      {clockIST(e.ts)}{" "}
      <span className={TONE[e.event_type] ?? "text-[#7b9dff]"}>{label(e.event_type)}</span>
      {e.amount_minor ? ` ${money(e.amount_minor)}` : ""}
      {e.reason ? ` · ${e.reason.slice(0, 72)}` : ""}
    </span>
  ));

  return (
    <div className="flex h-[38px] shrink-0 items-center overflow-hidden border-t-4 border-ink bg-ink text-paper">
      <span className="mono shrink-0 bg-recovered px-3.5 py-[11px] text-[10.5px] font-bold tracking-[.14em] text-ink">
        AUDIT
      </span>
      <div className="flex whitespace-nowrap" style={{ animation: "tick 44s linear infinite" }}>
        {strip}
        {strip}
      </div>
    </div>
  );
}
