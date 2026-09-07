"""Orchestration logic for the trailing-N-day Analytics refresh, offline.

archive_and_replace's own atomicity/retry/refusal behavior is covered in
tests/test_bigquery_writer.py, co-located with the class it extends. This file covers
window math and the refuse/skip/write decision tree in analytics_refresh.py itself,
using a FakeWriter/FakeAnalytics pair in the same style as tests/test_main.py.
"""

import logging
from datetime import date, timedelta

import pytest

import analytics_refresh
from analytics_refresh import (
    INCOMPLETE_LOG,
    compute_refresh_window,
    refresh_trailing_days,
)
from bigquery_writer import ReplaceRefused


def test_compute_refresh_window_ends_lookback_days_before_run_date():
    start, end = compute_refresh_window(date(2026, 9, 13), trailing_days=30, lookback_days=6)
    assert end == date(2026, 9, 7)
    assert start == date(2026, 8, 9)
    assert (end - start).days + 1 == 30


def test_compute_refresh_window_crosses_a_month_and_year_boundary():
    # 2026 is not a leap year; crossing Jan 1 and the Feb 28/Mar 1 boundary in one window.
    start, end = compute_refresh_window(date(2027, 1, 20), trailing_days=60, lookback_days=5)
    assert end == date(2027, 1, 15)
    assert start == date(2026, 11, 17)
    assert (end - start).days + 1 == 60


class FakeWriter:
    def __init__(self, existing_counts=None):
        self.calls = []
        self.existing_counts = existing_counts or {}
        self.archive_side_effect = None

    def count_rows_for_activity_date(self, table_name, activity_date):
        self.calls.append(("count", table_name, activity_date))
        return self.existing_counts.get((table_name, activity_date), 0)

    def archive_and_replace(self, **kwargs):
        self.calls.append(("archive_and_replace", kwargs))
        if self.archive_side_effect:
            raise self.archive_side_effect
        return len(kwargs["new_rows"])


class FakeAnalytics:
    """rows_by_date maps an activity_date to (rows, errors), for get_video_analytics.

    traffic_rows_by_date maps an activity_date to a plain rows list — refresh_trailing_days
    calls get_traffic_sources_range ONCE for the whole window (not per day), so this fake
    mirrors that: one call, returns every date in [start_date, end_date], and
    traffic_shard_errors simulates a shard-level failure affecting the whole call.
    """

    def __init__(self, rows_by_date=None, traffic_rows_by_date=None, traffic_shard_errors=None):
        self.rows_by_date = rows_by_date or {}
        self.traffic_rows_by_date = traffic_rows_by_date or {}
        self.traffic_shard_errors = list(traffic_shard_errors or [])
        self.queried_analytics = []
        self.queried_traffic_range = None

    def get_video_analytics(self, video_ids, activity_date):
        self.queried_analytics.append(activity_date)
        return self.rows_by_date.get(activity_date, ([], []))

    def get_traffic_sources_range(self, video_ids, start_date, end_date):
        self.queried_traffic_range = (start_date, end_date)
        rows_by_date = {}
        current = start_date
        while current <= end_date:
            rows_by_date[current] = list(self.traffic_rows_by_date.get(current, []))
            current += timedelta(days=1)
        return rows_by_date, list(self.traffic_shard_errors)


def make_log():
    return logging.LoggerAdapter(logging.getLogger("t"), {})


def test_refresh_one_table_skips_on_fetch_error(caplog):
    writer = FakeWriter()
    with caplog.at_level(logging.WARNING):
        outcome = analytics_refresh._refresh_one_table(
            bq_writer=writer, table_name="daily_traffic_sources",
            archive_table="daily_traffic_sources_refresh_archive",
            fetch_result=([{"video_id": "v1"}], ["v2: boom"]),
            activity_date=date(2026, 8, 20), run_date=date(2026, 9, 13),
            load_source="refresh_20260913", refresh_run_id="r1", min_row_ratio=0.5,
            log=make_log(),
        )
    assert outcome == "skipped_error"
    assert writer.calls == [], "a partial result must never be archived or written"
    assert any(m.startswith(INCOMPLETE_LOG) for m in caplog.messages)


def test_refresh_one_table_skips_empty_previously_empty_day_at_info(caplog):
    writer = FakeWriter(existing_counts={})
    with caplog.at_level(logging.INFO):
        outcome = analytics_refresh._refresh_one_table(
            bq_writer=writer, table_name="daily_video_analytics",
            archive_table="daily_video_analytics_refresh_archive",
            fetch_result=([], []),
            activity_date=date(2026, 8, 20), run_date=date(2026, 9, 13),
            load_source="refresh_20260913", refresh_run_id="r1", min_row_ratio=0.5,
            log=make_log(),
        )
    assert outcome == "skipped_empty"
    assert ("count", "daily_video_analytics", date(2026, 8, 20)) in writer.calls
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a day that was already empty and is still empty is not suspicious"
    )


def test_refresh_one_table_skips_empty_previously_populated_day_at_warning(caplog):
    writer = FakeWriter(existing_counts={("daily_video_analytics", date(2026, 8, 20)): 52})
    with caplog.at_level(logging.WARNING):
        outcome = analytics_refresh._refresh_one_table(
            bq_writer=writer, table_name="daily_video_analytics",
            archive_table="daily_video_analytics_refresh_archive",
            fetch_result=([], []),
            activity_date=date(2026, 8, 20), run_date=date(2026, 9, 13),
            load_source="refresh_20260913", refresh_run_id="r1", min_row_ratio=0.5,
            log=make_log(),
        )
    assert outcome == "skipped_empty"
    assert any(m.startswith(INCOMPLETE_LOG) for m in caplog.messages), (
        "a previously-populated day fetching empty must be visible, not silent"
    )


def test_refresh_one_table_writes_via_archive_and_replace(caplog):
    writer = FakeWriter()
    rows = [{"video_id": "v1", "estimated_minutes_watched": 3.0}]
    with caplog.at_level(logging.INFO):
        outcome = analytics_refresh._refresh_one_table(
            bq_writer=writer, table_name="daily_video_analytics",
            archive_table="daily_video_analytics_refresh_archive",
            fetch_result=(rows, []),
            activity_date=date(2026, 8, 20), run_date=date(2026, 9, 13),
            load_source="refresh_20260913", refresh_run_id="r1", min_row_ratio=0.5,
            log=make_log(),
        )
    assert outcome == "written"
    call = [c for c in writer.calls if c[0] == "archive_and_replace"][0]
    kwargs = call[1]
    assert kwargs["table_name"] == "daily_video_analytics"
    assert kwargs["archive_table"] == "daily_video_analytics_refresh_archive"
    assert kwargs["new_rows"] == rows
    assert kwargs["activity_date"] == date(2026, 8, 20)
    assert kwargs["snapshot_date"] == date(2026, 9, 13)
    assert kwargs["load_source"] == "refresh_20260913"
    assert kwargs["refresh_run_id"] == "r1"
    assert kwargs["min_row_ratio"] == 0.5


def test_refresh_one_table_reports_skipped_low_count_on_replace_refused(caplog):
    writer = FakeWriter()
    writer.archive_side_effect = ReplaceRefused("refused: new row count is below min_row_ratio")
    with caplog.at_level(logging.WARNING):
        outcome = analytics_refresh._refresh_one_table(
            bq_writer=writer, table_name="daily_video_analytics",
            archive_table="daily_video_analytics_refresh_archive",
            fetch_result=([{"video_id": "v1"}], []),
            activity_date=date(2026, 8, 20), run_date=date(2026, 9, 13),
            load_source="refresh_20260913", refresh_run_id="r1", min_row_ratio=0.5,
            log=make_log(),
        )
    assert outcome == "skipped_low_count"
    assert any(m.startswith(INCOMPLETE_LOG) for m in caplog.messages)


def test_refresh_trailing_days_iterates_full_window_and_aggregates_outcomes():
    run_date = date(2026, 9, 13)
    start, end = compute_refresh_window(run_date, trailing_days=5, lookback_days=6)
    writer = FakeWriter()
    written_day = end  # the last day in the window gets real rows; the rest are empty
    analytics = FakeAnalytics(
        rows_by_date={written_day: ([{"video_id": "v1"}], [])},
        traffic_rows_by_date={written_day: [{"video_id": "v1", "traffic_source_type": "YT_SEARCH"}]},
    )
    outcomes = refresh_trailing_days(
        video_ids=["v1"], analytics_api=analytics, bq_writer=writer,
        run_date=run_date, refresh_run_id="r1", lookback_days=6, trailing_days=5,
        log=make_log(),
    )
    assert set(outcomes.keys()) == {str(start + timedelta(days=i)) for i in range(5)}
    assert outcomes[str(written_day)]["daily_video_analytics"] == "written"
    assert outcomes[str(written_day)]["daily_traffic_sources"] == "written"
    for d, per_table in outcomes.items():
        if d != str(written_day):
            assert per_table["daily_video_analytics"] == "skipped_empty"
            assert per_table["daily_traffic_sources"] == "skipped_empty"
    assert len(analytics.queried_analytics) == 5, "video analytics is still fetched once per day"
    assert analytics.queried_traffic_range == (start, end), (
        "traffic sources must be fetched ONCE for the whole window, not once per day"
    )


def test_refresh_trailing_days_propagates_traffic_shard_error_to_every_day():
    run_date = date(2026, 9, 13)
    start, end = compute_refresh_window(run_date, trailing_days=3, lookback_days=6)
    writer = FakeWriter()
    analytics = FakeAnalytics(traffic_shard_errors=["shard of 600 videos: boom"])
    outcomes = refresh_trailing_days(
        video_ids=["v1"], analytics_api=analytics, bq_writer=writer,
        run_date=run_date, refresh_run_id="r1", lookback_days=6, trailing_days=3,
        log=make_log(),
    )
    assert all(o["daily_traffic_sources"] == "skipped_error" for o in outcomes.values()), (
        "a shard-level traffic failure can leave some videos silently missing from "
        "EVERY day it covers, so every day must be refused, not just the ones that "
        "happen to look empty or incomplete"
    )
    assert not any(c[0] == "archive_and_replace" for c in writer.calls), (
        "no archive_and_replace call for traffic on any day"
    )


def test_refresh_trailing_days_default_load_source_matches_run_date():
    run_date = date(2026, 9, 13)
    writer = FakeWriter()
    only_day = compute_refresh_window(run_date, 1, 6)[0]
    analytics = FakeAnalytics(rows_by_date={only_day: ([{"video_id": "v1"}], [])})
    refresh_trailing_days(
        video_ids=["v1"], analytics_api=analytics, bq_writer=writer,
        run_date=run_date, refresh_run_id="r1", lookback_days=6, trailing_days=1,
        log=make_log(),
    )
    call = [c for c in writer.calls if c[0] == "archive_and_replace"][0]
    assert call[1]["load_source"] == "refresh_20260913"


def test_refresh_trailing_days_honors_load_source_override():
    run_date = date(2026, 9, 13)
    writer = FakeWriter()
    only_day = compute_refresh_window(run_date, 1, 6)[0]
    analytics = FakeAnalytics(rows_by_date={only_day: ([{"video_id": "v1"}], [])})
    refresh_trailing_days(
        video_ids=["v1"], analytics_api=analytics, bq_writer=writer,
        run_date=run_date, refresh_run_id="r1", lookback_days=6, trailing_days=1,
        load_source="refresh_manual_test", log=make_log(),
    )
    call = [c for c in writer.calls if c[0] == "archive_and_replace"][0]
    assert call[1]["load_source"] == "refresh_manual_test"
