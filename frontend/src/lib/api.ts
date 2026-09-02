/**
 * Backend client.
 *
 * The API returns raw enum values (REQUIRES_APPROVAL, daily_limit_exceeded).
 * Those are correct on the wire and wrong on a screen — a database field
 * escaping into a merchant's view is the clearest sign nobody designed the
 * page. `label()` below is the only place that translation happens.
 *
 * Razorpay's own identifiers (pay_…, plink_…, rv_…) are shown verbatim:
 * that IS their format, and a reader who knows Razorpay should recognise it.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new ApiError(`GET ${path} failed`, res.status);
  return res.json() as Promise<T>;
}

/* ── shapes the backend actually returns ───────────────────────────── */

export type Metrics = {
  data_source: string;
  currency: string;
  revenue_at_risk_minor: number;
  revenue_at_risk: string;
  actions_by_status: Record<string, { count: number; paise: number }>;
  payment_links_issued: number;
  recovery_attempted_minor: number;
  recovery_attempted: string;
  recovered_minor: number;
  recovered: string;
  recovered_count: number;
  recovery_rate_of_attempted: number | null;
  policy_blocks: Record<string, number>;
  recovered_definition: string;
};

export type Incident = {
  id: number;
  title: string;
  status: string;
  currency: string;
  revenue_at_risk_minor: number;
  revenue_at_risk: string;
  affected_count: number;
  detected_at: string;
  root_cause: string | null;
  confidence: number | null;
};

export type RecoveryAction = {
  id: number;
  payment_id: string;
  customer_id: string;
  action: string;
  amount_minor: number;
  amount: string;
  status: string;
  recovery_score: number | null;
  rationale: string | null;
  policy: { result: string | null; rule: string | null; authorized_policy_version: string | null };
  execution_status: string | null;
  payment_link: string | null;
  recovered_minor: number;
  recovered: boolean;
  proposed_at: string;
};

export type AuditEvent = {
  ts: string;
  actor: string;
  event_type: string;
  customer_id: string | null;
  payment_id: string | null;
  incident_id: number | null;
  action_id: number | null;
  execution_id: number | null;
  amount_minor: number | null;
  policy_version: string | null;
  policy_result: string | null;
  reason: string | null;
  error: string | null;
};

export const getMetrics = () => get<Metrics>("/metrics");
export const getIncidents = () => get<{ incidents: Incident[] }>("/incidents");
export const getActions = (status?: string) =>
  get<{ actions: RecoveryAction[] }>(
    `/recovery-actions${status ? `?status_filter=${encodeURIComponent(status)}` : ""}`,
  );
export const getAudit = (limit = 40) => get<{ events: AuditEvent[] }>(`/audit?limit=${limit}`);

export type Decision = {
  action_id: number;
  approved: boolean;
  approver: string;
  executed: boolean;
  execution_status?: string;
  payment_link?: string | null;
  refused_rule?: string;
  note: string;
};

export async function decide(
  actionId: number,
  approved: boolean,
  approver: string,
  note?: string,
): Promise<Decision> {
  const res = await fetch(
    `${BASE}/recovery-actions/${actionId}/${approved ? "approve" : "deny"}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // Demo-scoped key (decision D9). A real deployment moves this behind a
        // session; a browser cannot hold a shared secret safely.
        "x-api-key": process.env.NEXT_PUBLIC_API_KEY ?? "",
      },
      body: JSON.stringify({ approver, note }),
    },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.detail ?? "Decision failed", res.status);
  }
  return res.json();
}

/* ── presentation ──────────────────────────────────────────────────── */

const LABELS: Record<string, string> = {
  AUTO_APPROVED: "Auto-approved",
  REQUIRES_APPROVAL: "Needs approval",
  BLOCKED: "Blocked",
  CREATE_PAYMENT_LINK: "Send payment link",
  SEND_RECOVERY_NOTIFICATION: "Send reminder",
  awaiting_approval: "Needs approval",
  approved: "Approved",
  denied: "Denied",
  executed: "Link sent",
  executing: "Sending",
  failed: "Failed",
  expired: "Expired",
  proposed: "Proposed",
  within_policy: "Within policy",
  above_auto_threshold: "Above the auto limit",
  daily_limit_exceeded: "Daily cap reached",
  customer_opted_out: "Customer opted out",
  cooldown_active: "Cooling down",
  already_paid: "Already paid",
  retry_limit_exceeded: "Retry limit reached",
  action_expired: "Action expired",
  POLICY_EVALUATED: "Policy checked",
  REVENUE_RECOVERED: "Revenue recovered",
  RECOVERY_FAILED: "Recovery failed",
  PAYMENT_LINK_CREATED: "Payment link sent",
  EXECUTION_STARTED: "Execution started",
  EXECUTION_PENDING: "Awaiting confirmation",
  EXECUTION_FAILED: "Execution failed",
  EXECUTION_BLOCKED_AT_RUNTIME: "Blocked at execution",
  PENDING_RESOLVED: "Confirmation resolved",
  ROOT_CAUSE_IDENTIFIED: "Root cause found",
  REVENUE_INCIDENT_DETECTED: "Incident detected",
  RECOVERY_STRATEGY_PROPOSED: "Strategy proposed",
  APPROVAL_GRANTED: "Approved by human",
  APPROVAL_DENIED: "Denied by human",
  OUTCOME_IGNORED_UNTRUSTED_SOURCE: "Ignored — unverified source",
  POLICY_ENGINE: "Policy engine",
  OUTCOME_ENGINE: "Outcome engine",
  INVESTIGATION_AGENT: "Investigation agent",
  RECOVERY_AGENT: "Recovery agent",
  EXECUTOR: "Executor",
  SYSTEM: "System",
};

/** Human phrasing for any enum the API returns. Unknown values degrade to
 *  sentence case rather than leaking raw. */
export function label(value: string | null | undefined): string {
  if (!value) return "—";
  if (LABELS[value]) return LABELS[value];
  if (value.startsWith("HUMAN:")) return value.slice(6);
  // Internal handles read as references, not variables: cust_0141 -> Customer 0141.
  const handle = /^cust_(\d+)$/.exec(value);
  if (handle) return `Customer ${handle[1]}`;
  const words = value.replace(/_/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Money formatting.
 *
 * Amounts are integers in the currency's MINOR unit — paise, cents, pence.
 * The unit is defined by the CURRENCY, never by the field name: 500_000 is
 * ₹5,000.00 in INR and $5,000.00 in USD (decision D16). Nothing here may
 * assume rupees.
 */
const MINOR_PER_MAJOR: Record<string, number> = {
  INR: 100, USD: 100, GBP: 100, EUR: 100, JPY: 1, KWD: 1000, BHD: 1000,
};
const LOCALE: Record<string, string> = {
  INR: "en-IN", USD: "en-US", GBP: "en-GB", EUR: "de-DE", JPY: "ja-JP",
};

export const money = (amountMinor: number, currency = "INR") => {
  const per = MINOR_PER_MAJOR[currency] ?? 100;
  const digits = per > 1 ? 2 : 0;
  return new Intl.NumberFormat(LOCALE[currency] ?? "en-IN", {
    style: "currency", currency,
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(amountMinor / per);
};

/** Same, rounded to whole units — for headline figures where the decimals
 *  are noise rather than information. */
export const moneyShort = (amountMinor: number, currency = "INR") => {
  const per = MINOR_PER_MAJOR[currency] ?? 100;
  return new Intl.NumberFormat(LOCALE[currency] ?? "en-IN", {
    style: "currency", currency, maximumFractionDigits: 0,
  }).format(amountMinor / per);
};

export const clockIST = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false, timeZone: "Asia/Kolkata",
  });
