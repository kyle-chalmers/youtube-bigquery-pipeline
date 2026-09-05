"""main.py orchestration, offline.

The most important assertion here is the alert contract: setup/6_setup_monitoring.sh
matches two literal log strings. Rewording either silently disables the only alert the
pipeline has, and until now nothing but a comment guarded that.
"""

import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent


class FakeWriter:
    def __init__(self, missing=None, fail_detect=False):
        self.calls = []
        self.missing = missing or []
        self.fail_detect = fail_detect

    def write_video_metadata(self, videos, snapshot_date):
        self.calls.append(("metadata", len(videos), snapshot_date))
        return len(videos)

    def write_daily_video_stats(self, videos, snapshot_date):
        self.calls.append(("stats", len(videos), snapshot_date))
        return len(videos)

    def write_daily_video_analytics(self, rows, snapshot_date, activity_date, load_source="cron"):
        self.calls.append(("analytics", len(rows), snapshot_date, activity_date, load_source))
        return len(rows)

    def write_daily_traffic_sources(self, rows, snapshot_date, activity_date, load_source="cron"):
        self.calls.append(("traffic", len(rows), snapshot_date, activity_date, load_source))
        return len(rows)

    def find_missing_activity_dates(self, table, earliest, latest, limit):
        if self.fail_detect:
            raise RuntimeError("bq down")
        self.calls.append(("detect", table, earliest, latest, limit))
        return self.missing


class FakeAnalytics:
    def __init__(self, rows_by_date, traffic=None, raise_on_init=False):
        self.rows_by_date = rows_by_date
        self.traffic = traffic or []
        self.queried = []

    def get_video_analytics(self, video_ids, analytics_date):
        self.queried.append(analytics_date)
        return list(self.rows_by_date.get(analytics_date, [])), []

    def get_traffic_sources(self, video_ids, analytics_date):
        return list(self.traffic), []


class FakeDataAPI:
    def __init__(self, *a, **k):
        pass

    def get_all_video_ids(self):
        return ["v1", "v2"]

    def get_video_details(self, ids):
        return [{"video_id": i, "video_type": "short" if i == "v1" else "full_length"} for i in ids]


@pytest.fixture
def wired(monkeypatch):
    """Wire fake Data API, writer and analytics client into main."""
    state = {"writer": FakeWriter(), "analytics": FakeAnalytics({})}
    monkeypatch.setattr(main, "YouTubeDataAPI", FakeDataAPI)
    monkeypatch.setattr(main, "BigQueryWriter", lambda **k: state["writer"])
    import youtube_analytics_api
    monkeypatch.setattr(youtube_analytics_api, "YouTubeAnalyticsAPI", lambda project_id: state["analytics"])
    return state


def alert_strings_from_monitoring_script():
    text = (ROOT / "setup" / "6_setup_monitoring.sh").read_text()
    return set(re.findall(r'textPayload:\\"([^"\\]+)\\"', text))


def test_alert_filter_strings_are_emitted_verbatim(wired, caplog):
    expected = alert_strings_from_monitoring_script()
    assert expected == {"Analytics API failed entirely", "Wrote daily_video_analytics — 0 rows"}, \
        "the monitoring filter changed; update this test and main.py together"

    # Case 1: analytics returns no rows -> the 0-rows string must appear exactly.
    wired["analytics"] = FakeAnalytics({})
    with caplog.at_level(logging.INFO):
        main.run_pipeline(date(2026, 9, 4), logging.LoggerAdapter(logging.getLogger("t"), {}))
    assert any("Wrote daily_video_analytics — 0 rows" in r.getMessage() for r in caplog.records)

    # Case 2: analytics client construction raises -> the failure string must appear.
    caplog.clear()
    import youtube_analytics_api

    def boom(project_id):
        raise RuntimeError("invalid_grant")

    youtube_analytics_api.YouTubeAnalyticsAPI = boom
    with caplog.at_level(logging.INFO):
        result = main.run_pipeline(date(2026, 9, 4), logging.LoggerAdapter(logging.getLogger("t"), {}))
    assert any(r.getMessage().startswith("Analytics API failed entirely") for r in caplog.records)
    assert result["analytics_errors"] == ["Analytics API: invalid_grant"]


def test_graceful_degradation_keeps_data_api_writes_and_returns_summary(wired):
    import youtube_analytics_api

    def boom(project_id):
        raise RuntimeError("no creds")

    youtube_analytics_api.YouTubeAnalyticsAPI = boom
    result = main.run_pipeline(date(2026, 9, 4), logging.LoggerAdapter(logging.getLogger("t"), {}))
    assert result["rows_inserted"] == {
        "video_metadata": 2, "daily_video_stats": 2,
        "daily_video_analytics": 0, "daily_traffic_sources": 0,
    }
    assert result["shorts"] == 1 and result["full_length"] == 1
    assert [c[0] for c in wired["writer"].calls] == ["metadata", "stats"]


def test_run_analytics_keys_on_the_lookback_activity_date(wired):
    snapshot = date(2026, 9, 4)
    activity = snapshot - main.timedelta(days=main.ANALYTICS_LOOKBACK_DAYS)
    wired["analytics"] = FakeAnalytics({activity: [{"video_id": "v1"}]}, traffic=[{"video_id": "v1"}])
    result = main.run_pipeline(snapshot, logging.LoggerAdapter(logging.getLogger("t"), {}))
    a = [c for c in wired["writer"].calls if c[0] == "analytics"][0]
    t = [c for c in wired["writer"].calls if c[0] == "traffic"][0]
    assert a[2:] == (snapshot, activity, "cron")
    assert t[2:] == (snapshot, activity, "cron")
    assert result["rows_inserted"]["daily_video_analytics"] == 1


def test_repair_gaps_requeries_missing_days_and_tags_them(wired):
    gap = date(2026, 8, 20)
    still_empty = date(2026, 8, 21)
    wired["writer"] = FakeWriter(missing=[gap, still_empty])
    wired["analytics"] = FakeAnalytics({gap: [{"video_id": "v1"}, {"video_id": "v2"}]})
    repaired = main._repair_gaps(wired["analytics"], wired["writer"], ["v1", "v2"], date(2026, 8, 30), date(2026, 9, 4))
    assert repaired == ["2026-08-20"]
    detect = [c for c in wired["writer"].calls if c[0] == "detect"][0]
    assert detect[1:] == ("daily_video_analytics", date(2026, 8, 30) - main.timedelta(days=main.GAP_LOOKBACK_DAYS),
                          date(2026, 8, 30), main.MAX_GAP_REPAIRS_PER_RUN)
    writes = [c for c in wired["writer"].calls if c[0] == "analytics"]
    assert writes == [("analytics", 2, date(2026, 9, 4), gap, "gap_repair")]
    assert sorted(wired["analytics"].queried) == [gap, still_empty]


def test_repair_gaps_swallows_detection_failure_and_returns_empty(wired):
    writer = FakeWriter(fail_detect=True)
    assert main._repair_gaps(FakeAnalytics({}), writer, ["v1"], date(2026, 8, 30), date(2026, 9, 4)) == []


def test_snapshot_date_is_the_phoenix_day_not_utc():
    # 23:50 Phoenix on 2026-09-04 is 06:50Z on 2026-09-05. The row must say 09-04.
    now_utc = datetime(2026, 9, 5, 6, 50, tzinfo=ZoneInfo("UTC"))
    assert now_utc.astimezone(main.PIPELINE_TZ).date() == date(2026, 9, 4)
    assert str(main.PIPELINE_TZ) == "America/Phoenix"


def test_uploads_playlist_is_channel_id_with_uu_prefix():
    assert main.CHANNEL_ID.startswith("UC")
    assert main.UPLOADS_PLAYLIST_ID == "UU" + main.CHANNEL_ID[2:]


def test_tuning_defaults_match_the_deploy_script():
    text = (ROOT / "setup" / "4_deploy_function.sh").read_text()
    assert f'ANALYTICS_LOOKBACK_DAYS:-{main.ANALYTICS_LOOKBACK_DAYS}' in text
    assert f'GAP_LOOKBACK_DAYS:-{main.GAP_LOOKBACK_DAYS}' in text
    assert f'MAX_GAP_REPAIRS_PER_RUN:-{main.MAX_GAP_REPAIRS_PER_RUN}' in text
