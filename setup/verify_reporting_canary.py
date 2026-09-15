#!/usr/bin/env python3
"""Fail unless a Reporting canary response proves an uncontended successful run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _count(result: dict[str, Any], name: str) -> int:
    if name not in result:
        raise ValueError(f"Reporting canary {name} is missing")
    value = result[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def validate(result: dict[str, Any]) -> tuple[str, int]:
    """Return the run ID and loaded count when the response is safe to accept."""
    if "error" in result:
        raise ValueError("Reporting canary returned an error response")
    if result.get("skipped"):
        raise ValueError("Reporting canary was skipped")
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Reporting canary run_id is missing")
    if _count(result, "failed") != 0:
        raise ValueError("Reporting canary recorded failed reports")
    if _count(result, "header_only_conflict") != 0:
        raise ValueError("Reporting canary recorded a header-only conflict")
    if _count(result, "concurrency_deferred") != 0:
        raise ValueError("Reporting canary detected transaction contention")
    if "errors" not in result:
        raise ValueError("Reporting canary errors is missing")
    errors = result["errors"]
    if not isinstance(errors, list):
        raise ValueError("Reporting canary errors must be a list")
    if errors:
        raise ValueError("Reporting canary recorded errors")
    return run_id, _count(result, "loaded")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.input.read_text())
    if not isinstance(result, dict):
        raise ValueError("Reporting canary response must be a JSON object")
    run_id, loaded = validate(result)
    print(json.dumps({"loaded": loaded, "run_id": run_id}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
