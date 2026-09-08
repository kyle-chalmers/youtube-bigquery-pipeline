"""Offline tests for get_traffic_sources_range, the range-query replacement for the
per-video-per-day traffic fetch used only by the trailing-N-day refresh job.

Measured live against staging on 2026-09-07: the per-video-per-day loop
(get_traffic_sources) took ~30 minutes for 204 videos x 30 days; this range shape did
the same coverage in under a second. These tests are offline (stubbed API responses),
same style as tests/test_analytics_sharding.py's cap-handling tests.
"""

from datetime import date

import youtube_analytics_api as ya


def make_api(range_response_by_call):
    """An instance whose _fetch_traffic_range's underlying API call is stubbed.

    range_response_by_call: a list of row-lists returned in call order, so a test can
    script "first call returns a capped response, second call (the split) returns the
    real data" without touching the network.
    """
    api = ya.YouTubeAnalyticsAPI.__new__(ya.YouTubeAnalyticsAPI)
    calls = []
    responses = list(range_response_by_call)

    def fake_api_call_with_retry(callable_fn, max_retries=3):
        return callable_fn()

    class FakeQuery:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def execute(self):
            return {"rows": responses.pop(0)} if responses else {"rows": []}

    class FakeReports:
        def query(self, **kwargs):
            return FakeQuery(**kwargs)

    class FakeAnalyticsClient:
        def reports(self):
            return FakeReports()

    api.analytics = FakeAnalyticsClient()
    api._api_call_with_retry = staticmethod(fake_api_call_with_retry)
    return api, calls


def row(video_id, day, source_type="YT_SEARCH", views=1, minutes=0.5):
    return [video_id, day, source_type, views, minutes]


def test_single_shard_single_call_covers_the_whole_window():
    api, calls = make_api([[row("v1", "2026-08-01"), row("v2", "2026-08-02")]])
    rows_by_date, errors = api.get_traffic_sources_range(
        ["v1", "v2"], date(2026, 8, 1), date(2026, 8, 3)
    )
    assert errors == []
    assert len(calls) == 1, "the whole window must be one call, not one per day"
    assert set(rows_by_date.keys()) == {date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)}
    assert rows_by_date[date(2026, 8, 1)] == [
        {"video_id": "v1", "traffic_source_type": "YT_SEARCH", "views": 1, "estimated_minutes_watched": 0.5}
    ]
    assert rows_by_date[date(2026, 8, 3)] == [], "a day with no rows still appears as an empty list, not a missing key"


def test_shards_by_video_id_when_over_max_filter_ids():
    many = [f"v{i}" for i in range(ya.MAX_FILTER_IDS + 50)]
    api, calls = make_api([[], []])  # two shards, both empty responses
    rows_by_date, errors = api.get_traffic_sources_range(many, date(2026, 8, 1), date(2026, 8, 1))
    assert errors == []
    assert len(calls) == 2
    assert len(calls[0]["filters"].split(",")) == ya.MAX_FILTER_IDS
    assert len(calls[1]["filters"].split(",")) == 50


def test_hitting_the_cap_splits_the_date_range_and_retries_both_halves():
    capped = [row(f"v{i}", "2026-08-05") for i in range(ya.RANGE_TRAFFIC_MAX_RESULTS)]
    first_half = [row("v1", "2026-08-01")]
    second_half = [row("v1", "2026-08-06")]
    api, calls = make_api([capped, first_half, second_half])
    rows_by_date, errors = api.get_traffic_sources_range(["v1"], date(2026, 8, 1), date(2026, 8, 10))
    assert errors == []
    assert len(calls) == 3, "one capped call, then two narrower calls covering each half"
    assert rows_by_date[date(2026, 8, 1)] != []
    assert rows_by_date[date(2026, 8, 6)] != []


def test_a_shard_failure_is_reported_and_does_not_abort_other_shards():
    many = [f"v{i}" for i in range(ya.MAX_FILTER_IDS + 10)]
    api, calls = make_api([[row("v1", "2026-08-01")]])  # only one response queued

    original = api._fetch_traffic_range
    call_count = {"n": 0}

    def flaky(video_ids, start_date, end_date):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("quotaExceeded")
        return original(video_ids, start_date, end_date)

    api._fetch_traffic_range = flaky
    rows_by_date, errors = api.get_traffic_sources_range(many, date(2026, 8, 1), date(2026, 8, 1))
    assert len(errors) == 1 and "quotaExceeded" in errors[0]
    # The second shard still ran and its data made it into the result.
    assert rows_by_date[date(2026, 8, 1)] == [
        {"video_id": "v1", "traffic_source_type": "YT_SEARCH", "views": 1, "estimated_minutes_watched": 0.5}
    ]


def test_capped_on_a_single_day_raises_instead_of_looping_forever():
    capped = [row(f"v{i}", "2026-08-01") for i in range(ya.RANGE_TRAFFIC_MAX_RESULTS)]
    api, calls = make_api([capped])
    import pytest

    with pytest.raises(RuntimeError, match="cannot split"):
        api._fetch_traffic_range(["v1"], date(2026, 8, 1), date(2026, 8, 1))
