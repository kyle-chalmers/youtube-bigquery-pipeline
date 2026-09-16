"""Tests for run-correlated production evidence capture."""

import importlib

import pytest


capture = importlib.import_module("capture_run_evidence")


def test_jobs_query_filters_exact_pipeline_run_label():
    sql = capture.jobs_query("us-central1")
    assert "`region-us-central1`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    assert "key = 'pipeline_run_id' AND value = @run_id" in sql
    assert "ORDER BY creation_time, job_id" in sql
    assert "statement_type" in sql
    assert "query" in sql


def test_successful_transaction_requires_done_error_free_script_with_begin():
    good = {
        "statement_type": "SCRIPT",
        "state": "DONE",
        "error_result": None,
        "query": "BEGIN TRANSACTION; DELETE FROM t WHERE TRUE; COMMIT TRANSACTION;",
    }
    assert capture.successful_transaction_jobs([good]) == [good]
    for change in (
        {"statement_type": "SELECT"},
        {"state": "RUNNING"},
        {"error_result": {"reason": "bad"}},
        {"query": "SELECT 1"},
    ):
        assert capture.successful_transaction_jobs([{**good, **change}]) == []


@pytest.mark.parametrize("region", ("us;DROP", "us central1", "`us`"))
def test_jobs_query_rejects_unsafe_region(region):
    with pytest.raises(ValueError):
        capture.jobs_query(region)
