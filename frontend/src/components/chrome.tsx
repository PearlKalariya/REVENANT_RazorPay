import Link from "next/link";

const NAV = [
  { href: "/", label: "Now" },
  { href: "/incidents", label: "Incidents" },
  { href: "/recovery", label: "Recovery" },
  { href: "/approvals", label: "Approvals" },
  { href: "/audit", label: "Audit" },
];

export function Header({ current }: { current: string }) {
  return (
    <header className="flex items-stretch border-b-4 border-ink bg-ink text-paper">
      <Link href="/" className="flex items-center bg-decide px-6 text-ink">
        <span className="disp text-[clamp(18px,1.9vw,25px)]">REVENANT</span>
      </Link>
      <nav className="flex items-center gap-[3px] overflow-hidden px-5">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`whitespace-nowrap px-[13px] py-1.5 text-[clamp(11px,1.1vw,13.5px)] uppercase transition-colors ${
              current === item.href
                ? "bg-machine font-bold text-paper"
                : "text-paper/60 hover:text-paper"
            }`}
          >
            {item.label}
          </Link>
        ))}
      </nav>
      <div className="grow" />
      <div className="flex items-center gap-[9px] bg-risk px-5 text-ink">
        <span className="size-2 rounded-full bg-ink" style={{ animation: "blink 1.4s steps(1) infinite" }} />
        <span className="mono text-[11px] font-bold tracking-[.1em]">TEST MODE · LIVE</span>
      </div>
    </header>
  );
}

/** Every screen states what the figures are. A number presented without saying
 *  it is synthetic is a number that gets quoted later as if it were real. */
export function DataNote({ source }: { source: string }) {
  return (
    <p className="mono px-8 py-2 text-[10.5px] tracking-[.1em] text-ink/45">
      {source} · Razorpay test mode · figures in INR
    </p>
  );
}

export function SectionRule({ left, right }: { left: string; right?: string }) {
  return (
    <div className="mb-3 flex items-center gap-2.5">
      <span className="mono text-[11px] font-bold tracking-[.14em]">{left}</span>
      <span className="h-[3px] grow bg-ink" />
      {right && <span className="mono text-[11px]">{right}</span>}
    </div>
  );
}
