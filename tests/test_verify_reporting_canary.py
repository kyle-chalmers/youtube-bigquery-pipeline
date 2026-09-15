"""Tests for the production Reporting canary response gate."""

import importlib

import pytest


canary = importlib.import_module("verify_reporting_canary")


def healthy(**overrides):
    result = {
        "run_id": "run-123",
        "loaded": 0,
        "failed": 0,
        "header_only_conflict": 0,
        "concurrency_deferred": 0,
        "errors": [],
    }
    result.update(overrides)
    return result


def test_accepts_a_healthy_noop_canary():
    assert canary.validate(healthy()) == ("run-123", 0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"skipped": True}, "skipped"),
        ({"failed": 1}, "failed"),
        ({"header_only_conflict": 1}, "header-only conflict"),
        ({"concurrency_deferred": 1}, "transaction contention"),
        ({"run_id": ""}, "run_id"),
        ({"loaded": -1}, "loaded"),
        ({"error": "expired writer lease requires operator cleanup"}, "error response"),
        ({"errors": ["backend failure"]}, "errors"),
    ],
)
def test_rejects_an_unhealthy_or_ambiguous_canary(overrides, message):
    with pytest.raises(ValueError, match=message):
        canary.validate(healthy(**overrides))


@pytest.mark.parametrize(
    "missing",
    ("loaded", "failed", "header_only_conflict", "concurrency_deferred", "errors"),
)
def test_rejects_a_canary_missing_required_result_fields(missing):
    result = healthy()
    result.pop(missing)
    with pytest.raises(ValueError, match=missing):
        canary.validate(result)
