"""The two load-bearing properties of the writer, pinned by tests for the first time.

1. The DELETE runs only after there are rows to replace it with. Deleting on an empty
   response destroyed activity 2026-02-22/23/24 on 2026-05-25.
2. The analytics tables key the DELETE on activity_date, not snapshot_date. Recovered
   history shares one collection date, so a snapshot-keyed delete would erase it all.
"""

import json
from datetime import date

import pytest

from bigquery_writer import BigQueryWriter


class FakeJob:
    def __init__(self, events, kind):
        self._events = events
        self._kind = kind

    def result(self):
        self._events.append((f"{self._kind}_result",))
        return None


class FakeClient:
    """Records every query, every wait, and every load in the order they happened."""

    def __init__(self):
        self.events = []

    def query(self, sql, job_config=None):
        params = {p.name: str(p.value) for p in (job_config.query_parameters if job_config else [])}
        self.events.append(("query", sql, params))
        return FakeJob(self.events, "query")

    def load_table_from_file(self, fh, table_ref, job_config=None):
        rows = [json.loads(line) for line in fh.read().decode().splitlines()]
        self.events.append(("load", table_ref, rows))
        return FakeJob(self.events, "load")


@pytest.fixture
def writer():
    w = BigQueryWriter.__new__(BigQueryWriter)
    w.client = FakeClient()
    w.dataset_ref = "proj.ds"
    return w


def kinds(writer):
    return [e[0] for e in writer.client.events]


def test_zero_rows_never_deletes(writer):
    for fn in (writer.write_daily_video_analytics, writer.write_daily_traffic_sources):
        assert fn([], date(2026, 9, 4), date(2026, 8, 30)) == 0
    assert writer.write_video_metadata([], date(2026, 9, 4)) == 0
    assert writer.write_daily_video_stats([], date(2026, 9, 4)) == 0
    assert writer.client.events == [], "an empty response is not a licence to erase a partition"


def test_delete_completes_before_load_starts(writer):
    rows = [{"video_id": "v1", "estimated_minutes_watched": 1.0}]
    assert writer.write_daily_video_analytics(rows, date(2026, 9, 4), date(2026, 8, 30)) == 1
    # The DELETE's .result() must be awaited before the load job is submitted, or the two
    # could race and the load land before the delete.
    assert kinds(writer) == ["query", "query_result", "load", "load_result"]


def test_daily_video_analytics_keys_delete_on_activity_date(writer):
    writer.write_daily_video_analytics(
        [{"video_id": "v1"}], snapshot_date=date(2026, 9, 4), activity_date=date(2026, 8, 30),
        load_source="recovery_20260829",
    )
    _, sql, params = writer.client.events[0]
    assert "DELETE FROM `proj.ds.daily_video_analytics` WHERE activity_date = @partition_value" in sql
    assert "snapshot_date" not in sql
    assert params == {"partition_value": "2026-08-30"}
    loaded = writer.client.events[2][2]
    assert loaded[0] == {
        "video_id": "v1", "snapshot_date": "2026-09-04", "load_source": "recovery_20260829",
        "activity_date": "2026-08-30",
    }


def test_daily_traffic_sources_keys_delete_on_activity_date(writer):
    rows = [{"video_id": "v1", "traffic_source_type": "YT_SEARCH", "views": 3}]
    writer.write_daily_traffic_sources(
        rows, snapshot_date=date(2026, 9, 4), activity_date=date(2026, 8, 30), load_source="gap_repair",
    )
    _, sql, params = writer.client.events[0]
    assert "DELETE FROM `proj.ds.daily_traffic_sources` WHERE activity_date = @partition_value" in sql
    assert params == {"partition_value": "2026-08-30"}
    _, table_ref, loaded = writer.client.events[2]
    assert table_ref == "proj.ds.daily_traffic_sources"
    assert loaded[0]["activity_date"] == "2026-08-30"
    assert loaded[0]["snapshot_date"] == "2026-09-04"
    assert loaded[0]["load_source"] == "gap_repair"


def test_snapshot_tables_key_delete_on_snapshot_date(writer):
    videos = [{
        "video_id": "v1", "title": "t", "published_at": "2026-01-01T00:00:00Z",
        "duration_seconds": 10, "duration_formatted": "0:10", "video_type": "short",
        "tags": "", "category_id": "28", "thumbnail_url": "",
        "view_count": 1, "like_count": 0, "comment_count": 0, "favorite_count": 0,
    }]
    writer.write_video_metadata(videos, date(2026, 9, 4))
    writer.write_daily_video_stats(videos, date(2026, 9, 4))
    assert kinds(writer) == ["query", "query_result", "load", "load_result"] * 2
    queries = [e for e in writer.client.events if e[0] == "query"]
    assert [q[1].split("`")[1] for q in queries] == ["proj.ds.video_metadata", "proj.ds.daily_video_stats"]
    for _, sql, params in queries:
        assert "WHERE snapshot_date = @partition_value" in sql
        assert params == {"partition_value": "2026-09-04"}
    loads = [e for e in writer.client.events if e[0] == "load"]
    assert [l[1] for l in loads] == ["proj.ds.video_metadata", "proj.ds.daily_video_stats"]
    assert loads[0][2][0]["snapshot_date"] == "2026-09-04"
    assert "view_count" not in loads[0][2][0] and "title" not in loads[1][2][0]


def test_load_source_defaults_to_cron(writer):
    writer.write_daily_video_analytics([{"video_id": "v1"}], date(2026, 9, 4), date(2026, 8, 30))
    assert writer.client.events[2][2][0]["load_source"] == "cron"


def test_find_missing_dates_parameterises_range_and_limit(writer):
    captured = {}

    class R:
        def result(self):
            return iter([[date(2026, 8, 20)]])

    def fake_query(sql, job_config=None):
        captured["sql"] = sql
        captured["params"] = {p.name: str(p.value) for p in job_config.query_parameters}
        return R()

    writer.client.query = fake_query
    got = writer.find_missing_activity_dates("daily_video_analytics", date(2026, 8, 10), date(2026, 8, 30), 5)
    assert got == [date(2026, 8, 20)]
    assert captured["params"] == {"earliest": "2026-08-10", "latest": "2026-08-30"}
    assert "`proj.ds.daily_video_analytics`" in captured["sql"]
    assert "LIMIT 5" in captured["sql"]
    assert "@earliest" in captured["sql"] and "@latest" in captured["sql"]
