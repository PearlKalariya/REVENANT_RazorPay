"""Revenue detection.

Deterministic. No LLM. Finds windows where a payment method fails far more
than its own normal rate, and turns each into a revenue incident.

Baseline is computed in two passes, and this matters. The spike we are trying
to detect is itself part of the data, so any baseline drawn from all of it is
contaminated by the very anomaly it measures against.

Pass 1 flags candidate windows by absolute failure rate alone. Pass 2 computes
each method's baseline by POOLING every hour that pass 1 did not flag — real
failures over real volume, with the anomaly excluded.

An earlier version took the median of per-hour rates. It was robust to the
spike, but with roughly nine payments per method-hour most hours contain zero
failures, so the median was 0.0 and every severity ratio came out as infinity.
Pooling fixes that: it aggregates thin hours into one honest denominator
instead of averaging a pile of zeros.

Two guards against false positives:

* `min_failures` — three failures out of four payments is noise, not an
  incident. Small denominators produce wild rates.
* `min_multiple` AND `min_absolute_rate` must BOTH be exceeded. A method with
  a 0.5% baseline hitting 2% is a 4x jump but still not worth waking anyone.

This module is pure — it takes payment records and returns incidents. No I/O,
so the thresholds can be tested directly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DetectionConfig:
    #: Observed failure rate must be at least this multiple of the method's median.
    min_multiple: float = 3.0
    #: ...and at least this absolute rate. Stops trivial baselines triggering.
    min_absolute_rate: float = 0.30
    #: Minimum failed payments in a window before it can be an incident.
    #: Tuned against real bucket density: ~9 payments per method-hour in this
    #: dataset, so a threshold of 5 silently dropped genuine spike hours.
    min_failures: int = 3
    #: Minimum total payments in a window, so rates have a real denominator.
    min_volume: int = 5


@dataclass(frozen=True)
class PaymentRecord:
    id: str
    customer_id: str
    amount_minor: int
    status: str
    method: str
    created_at: datetime
    failure_reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class Incident:
    title: str
    method: str
    window_start: datetime
    window_end: datetime
    affected_payment_ids: list[str] = field(default_factory=list)
    revenue_at_risk_minor: int = 0
    observed_failure_rate: float = 0.0
    baseline_failure_rate: float = 0.0
    total_in_window: int = 0
    top_failure_reason: str | None = None

    @property
    def affected_count(self) -> int:
        return len(self.affected_payment_ids)

    @property
    def severity_multiple(self) -> float:
        """How many times worse than normal. The headline number."""
        if self.baseline_failure_rate <= 0:
            return float("inf")
        return self.observed_failure_rate / self.baseline_failure_rate


def _hour_bucket(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def detect(
    payments: list[PaymentRecord], config: DetectionConfig | None = None
) -> list[Incident]:
    """Find revenue incidents. Deterministic: same input, same output, always."""
    config = config or DetectionConfig()
    if not payments:
        return []

    # Bucket by (method, hour).
    buckets: dict[tuple[str, datetime], list[PaymentRecord]] = defaultdict(list)
    for p in payments:
        buckets[(p.method, _hour_bucket(p.created_at))].append(p)

    # Pass 1: which hours look anomalous on absolute rate alone?
    suspect: set[tuple[str, datetime]] = set()
    for key, rows in buckets.items():
        if len(rows) < config.min_volume:
            continue
        if sum(r.failed for r in rows) / len(rows) >= config.min_absolute_rate:
            suspect.add(key)

    # Pass 2: baseline pooled over the hours pass 1 did NOT flag, so the
    # anomaly cannot inflate the number it is compared against.
    pooled: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [failed, total]
    for (method, hour), rows in buckets.items():
        if (method, hour) in suspect:
            continue
        pooled[method][0] += sum(r.failed for r in rows)
        pooled[method][1] += len(rows)
    baselines = {
        m: (f / t if t else 0.0) for m, (f, t) in pooled.items()
    }

    # Flag anomalous windows.
    flagged: dict[str, list[tuple[datetime, list[PaymentRecord], float]]] = defaultdict(list)
    for (method, hour), rows in sorted(buckets.items(), key=lambda kv: kv[0][1]):
        if len(rows) < config.min_volume:
            continue
        failures = [r for r in rows if r.failed]
        if len(failures) < config.min_failures:
            continue

        rate = len(failures) / len(rows)
        baseline = baselines.get(method, 0.0)

        if rate < config.min_absolute_rate:
            continue
        if baseline > 0 and rate < baseline * config.min_multiple:
            continue

        flagged[method].append((hour, rows, rate))

    # Merge adjacent hours of the same method into one incident. A three-hour
    # outage is one incident to act on, not three.
    incidents: list[Incident] = []
    for method, windows in flagged.items():
        windows.sort(key=lambda w: w[0])
        group: list[tuple[datetime, list[PaymentRecord], float]] = []

        def flush(g):
            if not g:
                return
            rows = [r for _, rs, _ in g for r in rs]
            failures = [r for r in rows if r.failed]
            reasons = [r.failure_reason for r in failures if r.failure_reason]
            top = (
                max(set(reasons), key=reasons.count) if reasons else None
            )
            observed = len(failures) / len(rows)
            baseline = baselines.get(method, 0.0)
            start, end = g[0][0], g[-1][0]
            incidents.append(
                Incident(
                    title=f"{method.upper()} failure spike",
                    method=method,
                    window_start=start,
                    window_end=end,
                    affected_payment_ids=[r.id for r in failures],
                    revenue_at_risk_minor=sum(r.amount_minor for r in failures),
                    observed_failure_rate=round(observed, 4),
                    baseline_failure_rate=round(baseline, 4),
                    total_in_window=len(rows),
                    top_failure_reason=top,
                )
            )

        for w in windows:
            if group and (w[0] - group[-1][0]).total_seconds() > 3600:
                flush(group)
                group = []
            group.append(w)
        flush(group)

    incidents.sort(key=lambda i: i.revenue_at_risk_minor, reverse=True)
    return incidents
