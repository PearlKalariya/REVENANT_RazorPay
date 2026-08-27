"""Revenue detection tests.

Pure function, so these run with no database and no network. The point of
most of them is FALSE POSITIVES: a detector that flags noise as an incident
will send an agent to investigate nothing and, worse, propose recovery
actions against customers whose payments were never really at risk.
"""

from datetime import datetime, timedelta

from backend.detection.engine import (
    DetectionConfig,
    Incident,
    PaymentRecord,
    detect,
)

BASE = datetime(2026, 8, 26, 0, 0)


def payments_at(hour, *, method="upi", n=10, failures=0, amount=100_000,
                start_id=0, reason="UPI collect request expired"):
    out = []
    for i in range(n):
        failed = i < failures
        out.append(
            PaymentRecord(
                id=f"pay_{method}_{hour}_{start_id + i}",
                customer_id=f"cust_{i}",
                amount_paise=amount,
                status="failed" if failed else "captured",
                method=method,
                created_at=BASE + timedelta(hours=hour, minutes=i),
                failure_reason=reason if failed else None,
            )
        )
    return out


def quiet_day(method="upi", failures_per_hour=0, hours=24):
    out = []
    for h in range(hours):
        out += payments_at(h, method=method, n=10,
                           failures=failures_per_hour, start_id=h * 100)
    return out


# --- detection ------------------------------------------------------------


def test_clear_spike_is_detected():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=12, failures=9, start_id=900)
    incidents = detect(data)
    assert len(incidents) == 1
    assert incidents[0].method == "upi"
    assert incidents[0].affected_count >= 9


def test_incident_reports_revenue_at_risk():
    """Revenue at risk covers EVERY failure in the window, not only the ones
    caused by the spike.

    The window also contains one ordinary background failure of 100,000 paise.
    That money is genuinely at risk too, so it is counted. Reporting only the
    spike-attributable subset would understate what the merchant actually lost.
    """
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=12, failures=9, amount=250_000, start_id=900)
    inc = detect(data)[0]
    assert inc.affected_count == 10           # 9 spike + 1 background
    assert inc.revenue_at_risk_paise == 9 * 250_000 + 100_000


def test_top_failure_reason_is_surfaced():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=12, failures=9, start_id=900,
                        reason="UPI collect request expired")
    assert detect(data)[0].top_failure_reason == "UPI collect request expired"


def test_adjacent_hours_merge_into_one_incident():
    """A three-hour outage is one thing to act on, not three."""
    data = quiet_day(failures_per_hour=1)
    for h in (14, 15, 16):
        data += payments_at(h, n=12, failures=9, start_id=900 + h)
    incidents = detect(data)
    assert len(incidents) == 1
    assert incidents[0].window_start.hour == 14
    assert incidents[0].window_end.hour == 16


def test_separated_spikes_are_separate_incidents():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(6, n=12, failures=9, start_id=800)
    data += payments_at(18, n=12, failures=9, start_id=900)
    assert len(detect(data)) == 2


# --- false positives ------------------------------------------------------


def test_uniform_failures_produce_no_incident():
    """A steady background rate is business as usual, not an incident."""
    assert detect(quiet_day(failures_per_hour=1)) == []


def test_low_volume_window_is_ignored():
    """3 failures out of 4 is 75%, but it is noise, not an outage."""
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=4, failures=3, start_id=900)
    assert detect(data) == []


def test_too_few_failures_ignored():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=20, failures=2, start_id=900)
    assert detect(data) == []


def test_moderate_rise_below_absolute_threshold_ignored():
    """4x a tiny baseline is still tiny. Both thresholds must be crossed."""
    data = quiet_day(failures_per_hour=1)          # ~10% baseline
    data += payments_at(14, n=20, failures=4, start_id=900)   # 20%
    assert detect(data) == []


def test_empty_input():
    assert detect([]) == []


def test_no_payments_for_a_method_does_not_crash():
    assert detect(payments_at(0, method="card", n=6, failures=0)) == []


# --- baseline correctness -------------------------------------------------


def test_baseline_excludes_the_spike_itself():
    """The anomaly must not inflate the number it is compared against.

    This is the bug the two-pass baseline exists to prevent: a contaminated
    baseline makes a severe spike look mild.
    """
    data = quiet_day(failures_per_hour=1)          # true baseline ~10%
    data += payments_at(14, n=20, failures=18, start_id=900)
    inc = detect(data)[0]
    assert inc.baseline_failure_rate < 0.15
    assert inc.severity_multiple > 5


def test_severity_is_finite_with_sparse_failures():
    """Regression: median-of-hourly-rates gave 0.0 baseline and inf severity."""
    data = []
    for h in range(24):
        data += payments_at(h, n=9, failures=1 if h % 3 == 0 else 0,
                            start_id=h * 100)
    data += payments_at(14, n=20, failures=17, start_id=990)
    inc = detect(data)[0]
    assert inc.baseline_failure_rate > 0
    assert inc.severity_multiple != float("inf")


def test_other_methods_unaffected_by_one_method_spike():
    data = quiet_day(method="upi", failures_per_hour=1)
    data += quiet_day(method="card", failures_per_hour=1)
    data += payments_at(14, method="upi", n=20, failures=18, start_id=900)
    incidents = detect(data)
    assert {i.method for i in incidents} == {"upi"}


# --- determinism and ordering --------------------------------------------


def test_detection_is_deterministic():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=20, failures=18, start_id=900)
    first = detect(data)
    for _ in range(10):
        again = detect(data)
        assert [i.affected_count for i in again] == [i.affected_count for i in first]
        assert [i.window_start for i in again] == [i.window_start for i in first]


def test_incidents_sorted_by_revenue_at_risk():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(6, n=12, failures=9, amount=50_000, start_id=800)
    data += payments_at(18, n=12, failures=9, amount=500_000, start_id=900)
    incidents = detect(data)
    assert incidents[0].revenue_at_risk_paise > incidents[1].revenue_at_risk_paise


def test_config_thresholds_are_honoured():
    data = quiet_day(failures_per_hour=1)
    data += payments_at(14, n=12, failures=9, start_id=900)
    assert detect(data) != []
    strict = DetectionConfig(min_failures=50)
    assert detect(data, strict) == []
