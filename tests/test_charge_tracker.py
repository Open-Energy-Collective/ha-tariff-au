"""Tests for ChargeTracker, the per-charge demand accumulator."""

from datetime import datetime, timedelta, timezone

from custom_components.oec_tariff_au.charge_tracker import ChargeTracker

AEST = timezone(timedelta(hours=10))


def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=AEST)


def make_tracker(method="30min_avg", peak_period="month", rate=0.5):
    charge = {
        "peak_period": peak_period,
        "measurement_method": method,
        "rate": rate,
    }
    return ChargeTracker(("month", None), charge)


def test_sample_in_window_buffers_reading():
    tracker = make_tracker()
    tracker.sample(dt(2026, 7, 22, 17, 0), 2.5, True)
    assert tracker.current_block_samples == [2.5]


def test_monthly_peak_updates_directly_without_blocking():
    tracker = make_tracker("monthly_peak")
    tracker.sample(dt(2026, 7, 22, 17, 0), 3.0, True)
    assert tracker.month_peak_kw == 3.0
    assert tracker.current_block_samples == []
    tracker.sample(dt(2026, 7, 22, 17, 5), 2.0, True)
    assert tracker.month_peak_kw == 3.0
    assert tracker.month_peak_recorded_at == dt(2026, 7, 22, 17, 0)


def test_boundary_crossing_flushes_average():
    tracker = make_tracker("30min_avg")
    tracker.sample(dt(2026, 7, 22, 17, 0), 4.0, True)
    tracker.sample(dt(2026, 7, 22, 17, 10), 6.0, True)
    assert tracker.month_peak_kw == 0.0
    assert tracker.current_block_samples == [4.0, 6.0]
    tracker.sample(dt(2026, 7, 22, 17, 31), 1.0, True)
    assert tracker.month_peak_kw == 5.0
    assert tracker.current_block_samples == [1.0]


def test_block_methods_record_block_start():
    tracker = make_tracker("30min_max")
    tracker.sample(dt(2026, 7, 22, 17, 12), 8.0, True)
    assert tracker.month_peak_recorded_at is None
    tracker.sample(dt(2026, 7, 22, 17, 31), 1.0, True)
    assert tracker.month_peak_kw == 8.0
    assert tracker.month_peak_recorded_at == dt(2026, 7, 22, 17, 0)


def test_out_of_window_sample_flushes_block_and_is_ignored():
    tracker = make_tracker("30min_avg")
    tracker.sample(dt(2026, 7, 22, 20, 50), 4.0, True)
    tracker.sample(dt(2026, 7, 22, 21, 0), 99.0, False)
    assert tracker.month_peak_kw == 4.0
    assert tracker.current_block_samples == []
    assert tracker.current_block_start is None


def test_out_of_window_sample_ignored_for_monthly_peak():
    tracker = make_tracker("monthly_peak")
    tracker.sample(dt(2026, 7, 22, 21, 0), 99.0, False)
    assert tracker.month_peak_kw == 0.0


def test_flush_processes_in_progress_block():
    tracker = make_tracker("30min_avg")
    tracker.sample(dt(2026, 7, 22, 17, 5), 25.0, True)
    assert tracker.month_peak_kw == 0.0
    tracker.flush()
    assert tracker.month_peak_kw == 25.0
    assert tracker.current_block_samples == []
    assert tracker.current_block_start is None

    restored = make_tracker("30min_avg")
    restored.restore_state(tracker.save_state())
    assert restored.month_peak_kw == 25.0


def test_flush_with_no_block_is_noop():
    tracker = make_tracker()
    tracker.flush()
    assert tracker.month_peak_kw == 0.0


def test_reset_billing_month():
    tracker = make_tracker("monthly_peak")
    peak_time = dt(2026, 7, 15, 18, 0)
    tracker.month_peak_kw = 12.0
    tracker.month_peak_recorded_at = peak_time
    now = dt(2026, 8, 1, 0, 0)
    tracker.reset_billing_month(now)
    assert tracker.previous_month_peak_kw == 12.0
    assert tracker.monthly_peaks_12 == [(12.0, peak_time)]
    assert tracker.month_peak_kw == 0.0
    assert tracker.month_peak_recorded_at is None
    assert tracker.last_reset == now


def test_monthly_peaks_12_caps_at_twelve():
    tracker = make_tracker("monthly_peak")
    tracker.monthly_peaks_12 = [(float(v), None) for v in range(12)]
    tracker.reset_billing_month(dt(2026, 8, 1, 0, 0))
    assert len(tracker.monthly_peaks_12) == 12
    assert tracker.monthly_peaks_12 == [(float(v), None) for v in range(1, 12)] + [(0.0, None)]


def test_rolling_12_months_follows_peak_period():
    tracker = make_tracker("30min_max", peak_period="rolling_12_months")
    tracker.month_peak_kw = 5.0
    tracker.reset_billing_month(dt(2026, 8, 1, 0, 0))
    tracker.month_peak_kw = 3.0
    tracker.reset_billing_month(dt(2026, 9, 1, 0, 0))
    tracker.month_peak_kw = 2.0
    assert tracker.chargeable_demand == 5.0


def test_rolling_method_with_month_peak_period_bills_month():
    tracker = make_tracker("rolling_12month_max", peak_period="month")
    tracker.monthly_peaks_12 = [(9.0, None)]
    tracker.month_peak_kw = 4.0
    assert tracker.chargeable_demand == 4.0


def test_rolling_method_reduces_block_as_max():
    tracker = make_tracker("rolling_12month_max", peak_period="rolling_12_months")
    tracker.sample(dt(2026, 7, 22, 17, 0), 2.0, True)
    tracker.sample(dt(2026, 7, 22, 17, 10), 6.0, True)
    tracker.flush()
    assert tracker.month_peak_kw == 6.0


def test_chargeable_recorded_at_rolling_picks_matching_month():
    tracker = make_tracker("30min_max", peak_period="rolling_12_months")
    historic = dt(2026, 3, 15, 18, 0)
    tracker.monthly_peaks_12 = [(3.0, dt(2026, 2, 1, 0, 0)), (7.0, historic), (2.0, None)]
    tracker.month_peak_kw = 5.0
    tracker.month_peak_recorded_at = dt(2026, 7, 20, 18, 0)
    assert tracker.chargeable_demand == 7.0
    assert tracker.chargeable_demand_recorded_at == historic


def test_chargeable_recorded_at_month_uses_current():
    tracker = make_tracker("30min_max")
    tracker.month_peak_kw = 4.0
    tracker.month_peak_recorded_at = dt(2026, 7, 22, 17, 0)
    assert tracker.chargeable_demand_recorded_at == dt(2026, 7, 22, 17, 0)


def test_update_charge_replaces_charge():
    tracker = make_tracker(rate=0.5)
    tracker.update_charge({"peak_period": "month", "measurement_method": "30min_max", "rate": 0.9})
    assert tracker.charge["rate"] == 0.9


def test_save_state_carries_rate():
    tracker = make_tracker(rate=0.77)
    assert tracker.save_state()["rate"] == 0.77


def test_round_trip():
    tracker = make_tracker()
    tracker.month_peak_kw = 6.75
    tracker.month_peak_recorded_at = dt(2026, 7, 20, 18, 0)
    tracker.previous_month_peak_kw = 5.5
    tracker.monthly_peaks_12 = [(5.5, dt(2026, 5, 1, 0, 0)), (6.0, None)]
    tracker.last_reset = dt(2026, 7, 1, 0, 0)

    restored = make_tracker()
    restored.restore_state(tracker.save_state())

    assert restored.month_peak_kw == 6.75
    assert restored.month_peak_recorded_at == dt(2026, 7, 20, 18, 0)
    assert restored.previous_month_peak_kw == 5.5
    assert restored.monthly_peaks_12 == [(5.5, dt(2026, 5, 1, 0, 0)), (6.0, None)]
    assert restored.last_reset == dt(2026, 7, 1, 0, 0)


def test_restore_does_not_overwrite_rate():
    tracker = make_tracker(rate=0.5)
    tracker.restore_state({"month_peak_kw": 1.0, "rate": 9.9})
    assert tracker.charge["rate"] == 0.5
    assert tracker.month_peak_kw == 1.0


def test_restore_accepts_1_2_x_blob():
    blob = {
        "month_peak_kw": 4.2,
        "month_peak_recorded_at": "2026-07-20T18:00:00+10:00",
        "previous_month_peak_kw": 3.1,
        "monthly_peaks_12": [5.5, [6.0, "2026-05-01T00:00:00+10:00"]],
        "last_reset": "2026-07-01T00:00:00+10:00",
    }
    tracker = make_tracker()
    tracker.restore_state(blob)
    assert tracker.month_peak_kw == 4.2
    assert tracker.monthly_peaks_12 == [(5.5, None), (6.0, dt(2026, 5, 1, 0, 0))]
    assert tracker.last_reset == dt(2026, 7, 1, 0, 0)


def test_restore_defaults_when_empty():
    tracker = make_tracker()
    tracker.restore_state({})
    assert tracker.month_peak_kw == 0.0
    assert tracker.month_peak_recorded_at is None
    assert tracker.monthly_peaks_12 == []
    assert tracker.last_reset is None


def test_block_peak_is_rounded_to_3_decimals():
    tracker = make_tracker()
    tracker.sample(dt(2027, 1, 15, 15, 0), 4.12345, True)
    tracker.sample(dt(2027, 1, 15, 15, 1), 4.12345, True)
    tracker.flush()
    assert tracker.month_peak_kw == 4.123
