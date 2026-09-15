"""Tests for the production writer-drain log gate."""

import importlib
import subprocess
import sys

import pytest


drain = importlib.import_module("verify_writer_drain")


def entry(message, trace="trace-1", timestamp="2026-09-14T01:00:00Z", *, request_status=None):
    value = {
        "resource": {"labels": {"service_name": "youtube-reporting-ingest"}},
        "textPayload": message,
        "timestamp": timestamp,
        "trace": trace,
    }
    if request_status is not None:
        value["logName"] = "projects/p/logs/run.googleapis.com%2Frequests"
        value["httpRequest"] = {"status": request_status}
    return value


def test_completed_trace_is_drained():
    entries = [
        entry("Reporting ingest started", timestamp="2026-09-14T01:00:00Z"),
        entry("Reporting ingest complete", timestamp="2026-09-14T01:01:00Z"),
    ]
    assert drain.active_starts(entries) == []


def test_successful_request_log_is_a_terminal_event():
    entries = [
        entry("Analytics refresh started", timestamp="2026-09-14T01:00:00Z"),
        entry("", timestamp="2026-09-14T01:25:00Z", request_status=200),
    ]
    assert drain.active_starts(entries) == []


def test_timed_out_request_remains_active_without_application_terminal():
    entries = [
        entry("Analytics refresh started", timestamp="2026-09-14T01:00:00Z"),
        entry("", timestamp="2026-09-14T01:25:00Z", request_status=504),
    ]
    assert len(drain.active_starts(entries)) == 1


@pytest.mark.parametrize(
    "terminal",
    ["Reporting API step complete", "Analytics refresh complete"],
)
def test_old_revision_write_complete_marker_closes_a_timed_out_trace(terminal):
    entries = [
        entry("Reporting ingest started", timestamp="2026-09-14T01:00:00Z"),
        entry("", timestamp="2026-09-14T01:25:00Z", request_status=504),
        entry(terminal, timestamp="2026-09-14T01:26:00Z"),
    ]
    assert drain.active_starts(entries) == []


def test_unmatched_and_untraced_starts_fail_closed():
    entries = [
        entry("Pipeline started", trace="trace-active"),
        entry("Reporting ingest started", trace=""),
    ]
    active = drain.active_starts(entries)
    assert {item["trace"] for item in active} == {"trace-active", ""}


def test_terminal_before_start_does_not_count():
    entries = [
        entry("Pipeline complete", timestamp="2026-09-14T00:59:00Z"),
        entry("Pipeline started", timestamp="2026-09-14T01:00:00Z"),
    ]
    assert len(drain.active_starts(entries)) == 1


def test_mixed_precision_timestamps_use_chronological_order():
    entries = [
        entry("Pipeline started", timestamp="2026-09-14T01:00:00.900Z"),
        entry("Pipeline complete", timestamp="2026-09-14T01:00:01Z"),
    ]
    assert drain.active_starts(entries) == []


def test_cli_requires_the_capture_entry_limit(tmp_path):
    input_path = tmp_path / "logs.json"
    input_path.write_text("[]")
    result = subprocess.run(
        [sys.executable, drain.__file__, "--input", str(input_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--entry-limit" in result.stderr
