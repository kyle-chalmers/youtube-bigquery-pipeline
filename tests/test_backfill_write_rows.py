"""The backfill hands (run_date, activity_date) to a writer whose signature is
(snapshot_date, activity_date). Swapping them keys the DELETE on the collection date,
which is exactly how activity 2026-02-22/23/24 was destroyed. Pin the order."""

from datetime import date

import pytest

import backfill_analytics as bf


class FakeWriter:
    def __init__(self):
        self.calls = []

    def write_daily_video_analytics(self, rows, snapshot_date, activity_date, load_source="cron"):
        self.calls.append(("analytics", rows, snapshot_date, activity_date, load_source))
        return len(rows)

    def write_daily_traffic_sources(self, rows, snapshot_date, activity_date, load_source="cron"):
        self.calls.append(("traffic", rows, snapshot_date, activity_date, load_source))
        return len(rows)


def test_write_rows_passes_activity_and_snapshot_dates_in_the_right_slots():
    w = FakeWriter()
    n = bf.write_rows(w, "daily_video_analytics", [{"video_id": "v"}],
                      activity_date=date(2026, 2, 22), run_date=date(2026, 9, 5),
                      load_source="backfill_20260905")
    assert n == 1
    kind, rows, snapshot_date, activity_date, load_source = w.calls[0]
    assert kind == "analytics"
    assert activity_date == date(2026, 2, 22), "the DELETE must key on the activity day"
    assert snapshot_date == date(2026, 9, 5), "snapshot_date is the collection day"
    assert load_source == "backfill_20260905"


def test_write_rows_routes_traffic_table():
    w = FakeWriter()
    bf.write_rows(w, "daily_traffic_sources", [{"video_id": "v"}], date(2026, 2, 22), date(2026, 9, 5), "x")
    assert w.calls[0][0] == "traffic"


def test_write_rows_refuses_unknown_tables():
    with pytest.raises(ValueError):
        bf.write_rows(FakeWriter(), "video_metadata", [{}], date(2026, 2, 22), date(2026, 9, 5), "x")


def test_write_rows_zero_rows_is_zero_and_still_delegates_the_guard():
    w = FakeWriter()
    assert bf.write_rows(w, "daily_video_analytics", [], date(2026, 2, 22), date(2026, 9, 5), "x") == 0
    # The zero-row guard lives in BigQueryWriter, not here; the backfill must not add a
    # second guard that could mask a writer regression.
    assert w.calls[0][1] == []
