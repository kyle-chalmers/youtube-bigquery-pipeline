"""The Reporting alert contract: every log string the monitoring script matches must be
the exact string the code emits. Rewording one side silently disables an alert."""

import logging
import re
from pathlib import Path

import pytest

import reporting_loader
import reporting_main

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "setup" / "6_setup_monitoring.sh").read_text()


def matched_strings(function_var: str) -> set[str]:
    """Quoted string arguments passed to log_filter for the given function variable."""
    out = set()
    for line in SCRIPT.splitlines():
        if f'log_filter "${function_var}"' in line:
            args = line.split(f'log_filter "${function_var}"', 1)[1]
            out.update(re.findall(r'"([^"]+)"', args))
    return out


def test_monitoring_script_matches_the_strings_the_code_emits():
    reporting = matched_strings("REPORTING_FUNCTION_NAME")
    assert reporting == {
        reporting_main.FAILED_LOG,
        "Reporting load error",
        reporting_loader.HEADER_ONLY_CONFLICT_LOG,
        reporting_main.STALE_LOG,
        reporting_main.SKIPPED_LOG,
    }
    pipeline = matched_strings("FUNCTION_NAME")
    assert pipeline == {"Analytics API failed entirely", "Wrote daily_video_analytics — 0 rows"}


def test_reporting_main_emits_the_alert_strings(monkeypatch, caplog):
    summary = reporting_loader.RunSummary(reports_considered=3, rows=10, loaded=2, header_only=1, failed=1,
                                          errors=["channel_reach_basic_a1 2026-09-01 r1: boom"])

    class Loader:
        def run(self):
            return summary

    monkeypatch.setattr(reporting_main, "build_loader", lambda load_source="cron": (Loader(), object()))
    monkeypatch.setattr(reporting_main, "freshness_by_type",
                        lambda ledger: {"channel_reach_basic_a1": 2, "channel_basic_a3": 9, "channel_cards_a1": 12})
    log = logging.LoggerAdapter(logging.getLogger("t"), {})
    with caplog.at_level(logging.INFO):
        result = reporting_main.run_reporting(log)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(reporting_main.COMPLETE_LOG + " — reports=3 rows=10 loaded=2") for m in messages)
    assert any(m.startswith("Reporting load error: channel_reach_basic_a1") for m in messages)
    stale = [m for m in messages if m.startswith(reporting_main.STALE_LOG)]
    assert len(stale) == 1 and "12 days old for 2 report type(s): channel_basic_a3, channel_cards_a1" in stale[0]
    assert result["newest_loaded_age_days"] == 12 and result["freshness_days_by_type"]["channel_reach_basic_a1"] == 2


def test_reporting_main_is_quiet_when_every_type_is_fresh(monkeypatch, caplog):
    class Loader:
        def run(self):
            return reporting_loader.RunSummary()

    monkeypatch.setattr(reporting_main, "build_loader", lambda load_source="cron": (Loader(), object()))
    monkeypatch.setattr(reporting_main, "freshness_by_type", lambda ledger: {"a": 4, "b": 2})  # 4 == threshold, not stale
    with caplog.at_level(logging.INFO):
        reporting_main.run_reporting(logging.LoggerAdapter(logging.getLogger("t"), {}))
    assert not any(r.getMessage().startswith(reporting_main.STALE_LOG) for r in caplog.records)


def test_reporting_main_is_stale_when_nothing_loaded(monkeypatch, caplog):
    class Loader:
        def run(self):
            return reporting_loader.RunSummary()

    monkeypatch.setattr(reporting_main, "build_loader", lambda load_source="cron": (Loader(), object()))
    monkeypatch.setattr(reporting_main, "freshness_by_type", lambda ledger: {})
    with caplog.at_level(logging.INFO):
        result = reporting_main.run_reporting(logging.LoggerAdapter(logging.getLogger("t"), {}))
    assert any("no report type has any loaded report" in r.getMessage() for r in caplog.records)
    assert result["newest_loaded_age_days"] is None


def test_reporting_main_failed_entirely_string_and_500(monkeypatch, caplog):
    monkeypatch.setattr(reporting_main, "REPORTING_ENABLED", True)

    def boom(log):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(reporting_main, "run_reporting", boom)
    with caplog.at_level(logging.INFO):
        body, status = reporting_main.reporting_main(request=None)
    assert status == 500 and body["error"] == "invalid_grant"
    assert any(r.getMessage().startswith(reporting_main.FAILED_LOG + ": invalid_grant") for r in caplog.records)


def test_reporting_main_kill_switch_is_a_noop(monkeypatch, caplog):
    monkeypatch.setattr(reporting_main, "REPORTING_ENABLED", False)
    monkeypatch.setattr(reporting_main, "build_loader", lambda *a, **k: pytest.fail("must not build a loader"))
    with caplog.at_level(logging.INFO):
        body, status = reporting_main.reporting_main(request=None)
    assert status == 200 and body["skipped"] is True
    skips = [r for r in caplog.records if r.getMessage().startswith(reporting_main.SKIPPED_LOG)]
    assert skips and skips[0].levelno == logging.WARNING, "a switched-off ingest must be visible to the alert"


def test_freshness_is_per_type_and_worst_wins():
    class L:
        def newest_loaded_by_type(self):
            return {"channel_reach_basic_a1": "2026-09-03", "channel_basic_a3": "2026-08-20", "channel_cards_a1": "2026-09-05"}

    from datetime import datetime, timezone
    today = datetime(2026, 9, 5, tzinfo=timezone.utc)
    ages = reporting_loader.freshness_by_type(L(), today)
    assert ages == {"channel_reach_basic_a1": 2, "channel_basic_a3": 16, "channel_cards_a1": 0}
    assert reporting_loader.freshness_days(L(), today) == 16

    class Empty:
        def newest_loaded_by_type(self):
            return {}

    assert reporting_loader.freshness_days(Empty(), today) is None
