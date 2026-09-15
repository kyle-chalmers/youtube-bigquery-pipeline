"""Every deployed table writer holds its domain lease for the complete run."""

from datetime import datetime, timezone
from types import SimpleNamespace

import main
import refresh_main
import reporting_main
from run_lease import ExpiredLease, LeaseHeld


class RecordingLease:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def __enter__(self):
        self.events.append("acquire")
        if self.error:
            raise self.error
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.events.append("release")
        return False


def record():
    return SimpleNamespace(
        owner={"run_id": "winner", "entrypoint": "test"},
        expires_at=datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc),
        generation=7,
    )


def test_daily_handler_holds_analytics_lease_around_pipeline(monkeypatch):
    events = []
    monkeypatch.setattr(main, "build_run_lease", lambda **kwargs: RecordingLease(events))
    monkeypatch.setattr(main, "run_pipeline", lambda snapshot_date, log, run_id=None: events.append("run") or {
        "videos_processed": 0, "shorts": 0, "full_length": 0,
        "rows_inserted": {"video_metadata": 0, "daily_video_stats": 0,
                          "daily_video_analytics": 0, "daily_traffic_sources": 0},
        "analytics_errors": [],
    })
    body, status = main.main(None)
    assert status == 200 and events == ["acquire", "run", "release"]


def test_reporting_handler_returns_200_without_writes_when_lease_is_held(monkeypatch, caplog):
    events = []
    monkeypatch.setattr(reporting_main, "REPORTING_ENABLED", True)
    monkeypatch.setattr(
        reporting_main, "build_run_lease",
        lambda **kwargs: RecordingLease(events, LeaseHeld(record())),
    )
    monkeypatch.setattr(reporting_main, "run_reporting", lambda *args: events.append("run"))
    body, status = reporting_main.reporting_main(None)
    assert status == 200
    assert body["reason"] == "lease_held"
    assert events == ["acquire"]
    assert reporting_main.LEASE_HELD_LOG in caplog.text


def test_refresh_handler_returns_500_without_writes_for_expired_lease(monkeypatch, caplog):
    events = []
    monkeypatch.setattr(
        refresh_main, "build_run_lease",
        lambda **kwargs: RecordingLease(events, ExpiredLease(record())),
    )
    monkeypatch.setattr(refresh_main, "run_refresh", lambda *args: events.append("run"))
    body, status = refresh_main.refresh_main(None)
    assert status == 500
    assert body["reason"] == "lease_expired"
    assert events == ["acquire"]
    assert refresh_main.LEASE_EXPIRED_LOG in caplog.text


def test_daily_handler_returns_500_without_writes_for_expired_lease(monkeypatch, caplog):
    events = []
    monkeypatch.setattr(
        main, "build_run_lease",
        lambda **kwargs: RecordingLease(events, ExpiredLease(record())),
    )
    monkeypatch.setattr(main, "run_pipeline", lambda *args: events.append("run"))
    body, status = main.main(None)
    assert status == 500
    assert body["reason"] == "lease_expired"
    assert events == ["acquire"]
    assert main.LEASE_EXPIRED_LOG in caplog.text


def test_reporting_handler_passes_request_entry_deadline_to_loader(monkeypatch):
    events = []
    monkeypatch.setattr(reporting_main, "REPORTING_ENABLED", True)
    monkeypatch.setattr(reporting_main, "build_run_lease", lambda **kwargs: RecordingLease(events))
    monkeypatch.setattr(reporting_main.time, "monotonic", lambda: 100.0)

    def fake_run(log, work_deadline, run_id=None):
        assert run_id
        events.append(("deadline", work_deadline))
        return {"budget_deferred": 0, "concurrency_deferred": 0}

    monkeypatch.setattr(reporting_main, "run_reporting", fake_run)
    body, status = reporting_main.reporting_main(None)
    assert status == 200
    assert ("deadline", 100.0 + reporting_main.RUNTIME_BUDGET_SECONDS) in events
