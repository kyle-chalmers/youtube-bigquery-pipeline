"""Operator lease inspection and generation-checked cleanup."""

import importlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from run_lease import LeaseRecord


manage = importlib.import_module("manage_run_lease")


class Blob:
    def __init__(self):
        self.deleted = []

    def delete(self, *, if_generation_match):
        self.deleted.append(if_generation_match)


class Bucket:
    def __init__(self):
        self.item = Blob()

    def blob(self, name):
        assert name == "leases/d/reporting-writer.json"
        return self.item


def record(generation=7):
    at = datetime(2026, 9, 13, tzinfo=timezone.utc)
    return LeaseRecord(
        object_name="leases/d/reporting-writer.json",
        generation=generation,
        dataset="d",
        domain="reporting-writer",
        owner={"entrypoint": "reporting_main", "run_id": "r1"},
        acquired_at=at,
        expires_at=at,
    )


def test_clear_requires_the_current_exact_generation(monkeypatch):
    bucket = Bucket()
    monkeypatch.setattr(manage, "inspect_lease", lambda *args: record(8))
    with pytest.raises(RuntimeError, match="expected generation 7, current generation 8"):
        manage.clear_lease(
            bucket, "d", "reporting-writer", generation=7, active_writer_checked=True
        )
    assert bucket.item.deleted == []


def test_clear_deletes_with_generation_precondition(monkeypatch):
    bucket = Bucket()
    monkeypatch.setattr(manage, "inspect_lease", lambda *args: record(7))
    cleared = manage.clear_lease(
        bucket, "d", "reporting-writer", generation=7, active_writer_checked=True
    )
    assert cleared == record(7)
    assert bucket.item.deleted == [7]


def test_clear_refuses_a_live_unexpired_lease(monkeypatch):
    bucket = Bucket()
    live = replace(record(7), expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    monkeypatch.setattr(manage, "inspect_lease", lambda *args: live)
    with pytest.raises(RuntimeError, match="live unexpired lease"):
        manage.clear_lease(
            bucket, "d", "reporting-writer", generation=7, active_writer_checked=True
        )
    assert bucket.item.deleted == []


def test_clear_refuses_when_no_lease_exists(monkeypatch):
    monkeypatch.setattr(manage, "inspect_lease", lambda *args: None)
    with pytest.raises(RuntimeError, match="no current lease"):
        manage.clear_lease(
            Bucket(), "d", "reporting-writer", generation=7, active_writer_checked=True
        )


def test_clear_requires_active_writer_check_attestation(monkeypatch):
    bucket = Bucket()
    monkeypatch.setattr(manage, "inspect_lease", lambda *args: record(7))
    with pytest.raises(RuntimeError, match="confirm no active writer"):
        manage.clear_lease(
            bucket, "d", "reporting-writer", generation=7, active_writer_checked=False
        )
    assert bucket.item.deleted == []


def test_record_json_contains_owner_generation_and_expiry():
    value = manage.record_as_dict(record())
    assert value["generation"] == 7
    assert value["owner"]["run_id"] == "r1"
    assert value["expires_at"] == "2026-09-13T00:00:00+00:00"
