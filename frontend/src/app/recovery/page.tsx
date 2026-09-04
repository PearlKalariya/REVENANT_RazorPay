import { Header, DataNote, SectionRule } from "@/components/chrome";
import { AuditTicker } from "@/components/ticker";
import { getActions, label, type RecoveryAction, moneyShort
} from "@/lib/api";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<string, string> = {
  executed: "bg-machine text-paper",
  awaiting_approval: "bg-decide",
  approved: "bg-recovered",
  denied: "bg-risk text-paper",
  failed: "bg-risk text-paper",
  expired: "bg-paper-deep",
};

export default async function Recovery() {
  let actions: RecoveryAction[] = [];
  try {
    actions = (await getActions()).actions;
  } catch {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/recovery" />
        <p className="p-10 text-[15px]">Backend unreachable. Start the API and reload.</p>
      </div>
    );
  }

  const recovered = actions.filter((a) => a.recovered);
  const totalRecovered = recovered.reduce((s, a) => s + a.recovered_minor, 0);
  const attempted = actions.filter((a) => a.payment_link).reduce((s, a) => s + a.amount_minor, 0);

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/recovery" />

      <section className="grid shrink-0 grid-cols-4 border-b-4 border-ink">
        <Cell k="CANDIDATES" v={String(actions.length)} />
        <Cell k="LINKS SENT" v={String(actions.filter((a) => a.payment_link).length)} />
        <Cell k="ATTEMPTED" v={moneyShort(attempted)} />
        <Cell k="RECOVERED" v={moneyShort(totalRecovered)} cls="bg-recovered" last />
      </section>

      <section className="grow px-8 py-7">
        <SectionRule left="RECOVERY CANDIDATES" right="RANKED BY SCORE" />
        <div className="grid grid-cols-[150px_110px_70px_1fr_150px_110px] items-center gap-x-4 border-b-[3px] border-ink pb-2">
          {["PAYMENT", "AMOUNT", "SCORE", "POLICY", "STATUS", "OUTCOME"].map((h) => (
            <span key={h} className="mono text-[10px] font-bold tracking-[.12em]">{h}</span>
          ))}
        </div>
        <div className="flex flex-col">
          {actions.map((a) => (
            <div key={a.id} className="grid grid-cols-[150px_110px_70px_1fr_150px_110px] items-center gap-x-4 border-b-2 border-dotted border-ink/35 py-2.5">
              <span className="mono text-[12px]">{a.payment_id}</span>
              <span className="mono tnum text-[13px] font-bold">{a.amount}</span>
              <span className="mono tnum text-[12px]">{a.recovery_score?.toFixed(2) ?? "—"}</span>
              <span className="text-[12.5px]">{label(a.policy.rule)}</span>
              <span className={`mono w-fit px-2 py-0.5 text-[10.5px] font-bold tracking-[.06em] ${a.recovered ? "bg-recovered" : STATUS_TONE[a.status] ?? "bg-paper-deep"}`}>
                {a.recovered ? "RECOVERED" : label(a.status).toUpperCase()}
              </span>
              {a.recovered ? (
                <span className="flex items-baseline gap-2">
                  <span className="mono tnum text-[12.5px] font-bold text-recovered">
                    {moneyShort(a.recovered_minor)}
                  </span>
                  {a.payment_link && (
                    <a href={a.payment_link} target="_blank" rel="noreferrer" className="mono text-[10.5px] underline text-ink/60">
                      link
                    </a>
                  )}
                </span>
              ) : a.payment_link ? (
                <a href={a.payment_link} target="_blank" rel="noreferrer" className="mono text-[11px] underline">
                  open link
                </a>
              ) : (
                <span className="mono text-[11px] text-ink/40">—</span>
              )}
            </div>
          ))}
        </div>
      </section>

      <DataNote source="SYNTHETIC TEST DATA" />
      <AuditTicker />
    </div>
  );
}

function Cell({ k, v, cls, last }: { k: string; v: string; cls?: string; last?: boolean }) {
  return (
    <div className={`px-7 py-5 ${last ? "" : "border-r-4 border-ink"} ${cls ?? ""}`}>
      <span className="mono text-[11px] font-bold tracking-[.14em]">{k}</span>
      <div className="disp tnum mt-1.5 text-[clamp(22px,2.6vw,38px)] leading-none">{v}</div>
    </div>
  );
}
