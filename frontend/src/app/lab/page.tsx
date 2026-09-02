import { Header, DataNote } from "@/components/chrome";
import { getLab, type LabLimits } from "@/lib/api";
import { Lab } from "./lab";

export const dynamic = "force-dynamic";

export default async function LabPage() {
  let data: LabLimits;
  try {
    data = await getLab();
  } catch {
    return (
      <div className="flex min-h-screen flex-col">
        <Header current="/lab" />
        <p className="p-10 text-[15px]">Backend unreachable. Start the API and reload.</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col">
      <Header current="/lab" />

      <section className="relative overflow-hidden border-b-4 border-ink bg-judgement px-8 py-7 text-paper">
        <div className="dots pointer-events-none absolute inset-0 opacity-[.12]" />
        <div className="relative">
          <h1 className="disp text-[clamp(26px,3.4vw,44px)] uppercase">Try to make it misbehave</h1>
          <p className="mt-3 max-w-[80ch] text-[15px] leading-[1.55] text-paper/85">
            Every button below runs the <strong>same Policy Engine</strong> the executor
            calls immediately before money moves — not a mock of it. Nothing here
            writes anything or reaches Razorpay, so change whatever you like.
          </p>
        </div>
      </section>

      <Lab data={data} />
      <DataNote source="LIVE POLICY ENGINE · NO SIDE EFFECTS" />
    </div>
  );
}
