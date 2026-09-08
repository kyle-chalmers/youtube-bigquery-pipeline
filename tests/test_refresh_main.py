"""The refresh alert contract and refresh_main.py orchestration, offline.

Same shape as tests/test_reporting_alerts.py: every log string
setup/6_setup_monitoring.sh matches for the refresh function must be a string the code
actually emits, or the alert is silently disabled.
"""

import logging
import re
from datetime import date
from pathlib import Path

import pytest

import refresh_main

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "setup" / "6_setup_monitoring.sh").read_text()


def matched_strings(function_var: str) -> set[str]:
    out = set()
    for line in SCRIPT.splitlines():
        if f'log_filter "${function_var}"' in line:
            args = line.split(f'log_filter "${function_var}"', 1)[1]
            out.update(re.findall(r'"([^"]+)"', args))
    return out


def test_monitoring_script_matches_the_refresh_log_strings():
    assert matched_strings("REFRESH_FUNCTION_NAME") == {
        refresh_main.FAILED_LOG,
        refresh_main.REFRESH_INCOMPLETE_LOG,
    }


def test_refresh_main_reuses_analytics_lookback_days_env_var_name(monkeypatch):
    # Reusing the daily function's own env var name (not a separate REFRESH_LOOKBACK_DAYS)
    # is what makes the drift-risk Fable caught impossible instead of merely documented.
    monkeypatch.setenv("ANALYTICS_LOOKBACK_DAYS", "6")
    import importlib

    reloaded = importlib.reload(refresh_main)
    assert reloaded.ANALYTICS_LOOKBACK_DAYS == 6
    importlib.reload(refresh_main)  # restore default env for later tests


def test_run_refresh_logs_complete_with_per_outcome_counts(monkeypatch, caplog):
    monkeypatch.setattr(
        refresh_main, "_video_ids_from_latest_snapshot", lambda bq_client: ["v1", "v2"]
    )

    class FakeAnalyticsAPI:
        def __init__(self, project_id):
            pass

    class FakeWriter:
        def __init__(self, project_id, dataset_id):
            pass

    monkeypatch.setattr(refresh_main, "YouTubeAnalyticsAPI", FakeAnalyticsAPI)
    monkeypatch.setattr(refresh_main, "BigQueryWriter", FakeWriter)
    monkeypatch.setattr(
        refresh_main,
        "refresh_trailing_days",
        lambda **kwargs: {
            "2026-08-10": {"daily_video_analytics": "written", "daily_traffic_sources": "skipped_empty"},
            "2026-08-11": {"daily_video_analytics": "skipped_error", "daily_traffic_sources": "skipped_low_count"},
        },
    )

    class FakeBQClient:
        def __init__(self, project=None):
            pass

    monkeypatch.setattr(refresh_main.bigquery, "Client", FakeBQClient)

    with caplog.at_level(logging.INFO):
        result = refresh_main.run_refresh(
            date(2026, 9, 13), "run1", logging.LoggerAdapter(logging.getLogger("t"), {})
        )

    assert result["counts"] == {
        "written": 1, "skipped_empty": 1, "skipped_error": 1, "skipped_low_count": 1,
    }
    assert any(
        m.startswith(refresh_main.COMPLETE_LOG + " — days=2 written=1")
        for m in caplog.messages
    )


def test_refresh_main_failure_path_redacts_and_returns_500(monkeypatch, caplog):
    def boom(run_date, refresh_run_id, log):
        raise RuntimeError("invalid_grant?access_token=SECRET")

    monkeypatch.setattr(refresh_main, "run_refresh", boom)
    with caplog.at_level(logging.INFO):
        body, status = refresh_main.refresh_main(request=None)
    assert status == 500
    assert "SECRET" not in body["error"]
    assert any(
        r.getMessage().startswith(refresh_main.FAILED_LOG) and "SECRET" not in r.getMessage()
        for r in caplog.records
    )


def test_refresh_main_success_path_returns_200_with_run_id(monkeypatch):
    monkeypatch.setattr(
        refresh_main, "run_refresh",
        lambda run_date, refresh_run_id, log: {"outcomes": {}, "counts": {}},
    )
    body, status = refresh_main.refresh_main(request=None)
    assert status == 200
    assert "run_id" in body
