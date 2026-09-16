#!/usr/bin/env python3
"""Fail unless every recent Cloud Run writer start has a terminal log entry."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


START_MARKERS = (
    "Pipeline started",
    "Reporting ingest started",
    "Analytics refresh started",
)
TERMINAL_MARKERS = (
    "Pipeline complete",
    "Pipeline failed",
    "Reporting API step complete",
    "Reporting ingest complete",
    "Reporting API failed entirely",
    "Reporting API skipped",
    "Analytics refresh complete",
    "Analytics refresh handler complete",
    "Analytics refresh failed entirely",
    "Writer lease held",
    "Writer lease expired",
    "Writer lease release failed",
)


def _message(entry: dict[str, Any]) -> str:
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        return str(payload.get("message", ""))
    return str(entry.get("textPayload", ""))


def _service(entry: dict[str, Any]) -> str:
    return str(entry.get("resource", {}).get("labels", {}).get("service_name", ""))


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def active_starts(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return starts whose trace has no later terminal application or request log."""
    ordered = sorted(entries, key=lambda item: _timestamp(str(item.get("timestamp", ""))))
    starts: dict[str, dict[str, str]] = {}
    terminals: dict[str, str] = {}
    for entry in ordered:
        message = _message(entry)
        trace = str(entry.get("trace", ""))
        service = _service(entry)
        timestamp = str(entry.get("timestamp", ""))
        if any(marker in message for marker in START_MARKERS):
            key = trace or f"untraced:{service}:{timestamp}"
            starts[key] = {
                "service": service,
                "started_at": timestamp,
                "trace": trace,
            }
        request_finished = bool(
            trace
            and "run.googleapis.com%2Frequests" in str(entry.get("logName", ""))
            and isinstance(entry.get("httpRequest"), dict)
            and isinstance(entry["httpRequest"].get("status"), int)
            and 200 <= entry["httpRequest"]["status"] < 300
        )
        if trace and (request_finished or any(marker in message for marker in TERMINAL_MARKERS)):
            terminals[trace] = timestamp
    return [
        start
        for key, start in starts.items()
        if not start["trace"]
        or key not in terminals
        or _timestamp(terminals[key]) < _timestamp(start["started_at"])
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--entry-limit",
        type=int,
        required=True,
        help="the exact --limit used when capturing the Cloud Logging input",
    )
    args = parser.parse_args()
    entries = json.loads(args.input.read_text())
    if not isinstance(entries, list):
        raise RuntimeError("Cloud Logging capture must be a JSON list")
    active = active_starts(entries)
    if args.entry_limit <= 0:
        parser.error("--entry-limit must be positive")
    capture_truncated = len(entries) >= args.entry_limit
    result = {
        "active_count": len(active),
        "active_starts": active,
        "capture_truncated": capture_truncated,
        "entries_checked": len(entries),
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if active or capture_truncated else 0


if __name__ == "__main__":
    sys.exit(main())
