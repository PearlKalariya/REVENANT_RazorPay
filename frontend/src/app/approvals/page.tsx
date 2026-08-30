import { Header, DataNote, SectionRule } from "@/components/chrome";
import { AuditTicker } from "@/components/ticker";
import { getActions, type RecoveryAction } from "@/lib/api";
import { ApprovalQueue } from "./queue";

export const dynamic = "force-dynamic";

export default async function Approvals() {
  let pending: RecoveryAction[] = [];
  let decided: RecoveryAction[] = [];
  try {
    const [a, approved, denied] = await Promise.all([
      getActions("awaiting_approval"), getActions("approved"), getActions("denied"),
    ]);
    pending = a.actions;
    decided = [...approved.actions, ...denied.actions];
  } catch {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/approvals" />
        <p className="p-10 text-[15px]">Backend unreachable. Start the API and reload.</p>
      </div>
    );
  }

  const held = pending.reduce((s, a) => s + a.amount_paise, 0);

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/approvals" />

      <section className="grid shrink-0 grid-cols-3 border-b-4 border-ink">
        <div className="relative overflow-hidden border-r-4 border-ink bg-decide px-8 py-6">
          <div className="dots pointer-events-none absolute inset-0 opacity-[.1]" />
          <div className="relative">
            <span className="mono text-[11px] font-bold tracking-[.16em]">HELD FOR YOU</span>
            <div className="disp tnum mt-2 leading-none text-[clamp(28px,3.4vw,50px)]">
              ₹{Math.round(held / 100).toLocaleString("en-IN")}
            </div>
            <div className="mono mt-1.5 text-[11px]">
              {pending.length} {pending.length === 1 ? "DECISION" : "DECISIONS"} PENDING
            </div>
          </div>
        </div>
        <div className="border-r-4 border-ink px-8 py-6">
          <span className="mono text-[11px] font-bold tracking-[.16em]">AUTONOMOUS LIMIT</span>
          <div className="disp tnum mt-2 leading-none text-[clamp(28px,3.4vw,50px)]">₹5,000</div>
          <div className="mono mt-1.5 text-[11px] text-ink/60">ANYTHING ABOVE COMES TO YOU</div>
        </div>
        <div className="px-8 py-6">
          <span className="mono text-[11px] font-bold tracking-[.16em]">ALREADY DECIDED</span>
          <div className="disp tnum mt-2 leading-none text-[clamp(28px,3.4vw,50px)]">{decided.length}</div>
          <div className="mono mt-1.5 text-[11px] text-ink/60">THIS BATCH</div>
        </div>
      </section>

      <section className="grow px-8 py-7">
        <SectionRule left="AWAITING YOUR DECISION" right={pending.length ? `${pending.length} OPEN` : "NONE"} />
        <ApprovalQueue initial={pending} />

        {decided.length > 0 && (
          <div className="mt-10">
            <SectionRule left="DECIDED" right={String(decided.length)} />
            <div className="flex flex-col">
              {decided.map((a) => (
                <div key={a.id} className="flex items-center gap-4 border-b-2 border-dotted border-ink/40 py-2.5">
                  <span className="mono w-[150px] shrink-0 text-[12px]">{a.payment_id}</span>
                  <span className="mono tnum w-[110px] shrink-0 text-[13px] font-bold">{a.amount}</span>
                  <span className={`mono text-[11px] font-bold tracking-[.08em] ${a.status === "denied" ? "text-risk" : "text-machine"}`}>
                    {a.status === "denied" ? "DENIED" : "APPROVED"}
                  </span>
                  <div className="grow" />
                  {a.payment_link && (
                    <a href={a.payment_link} target="_blank" rel="noreferrer" className="mono text-[11px] underline">
                      view link
                    </a>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      <DataNote source="SYNTHETIC TEST DATA" />
      <AuditTicker />
    </div>
  );
}
