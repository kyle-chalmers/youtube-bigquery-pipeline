"""Atomic writer lease behavior, entirely offline."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import PreconditionFailed

from run_lease import ExpiredLease, LeaseHeld, RunLease, inspect_lease


NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, payload, *, content_type, if_generation_match):
        assert content_type == "application/json"
        assert if_generation_match == 0
        if self.name in self.bucket.objects:
            raise PreconditionFailed("already exists")
        self.bucket.next_generation += 1
        self.bucket.objects[self.name] = {
            "generation": self.bucket.next_generation,
            "payload": payload.encode() if isinstance(payload, str) else payload,
        }
        self.generation = self.bucket.next_generation

    def reload(self):
        self.generation = self.bucket.objects[self.name]["generation"]

    def download_as_bytes(self):
        return self.bucket.objects[self.name]["payload"]

    def delete(self, *, if_generation_match):
        if self.bucket.delete_error:
            raise self.bucket.delete_error
        current = self.bucket.objects[self.name]
        if current["generation"] != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        del self.bucket.objects[self.name]


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.next_generation = 40
        self.delete_error = None

    def blob(self, name):
        return FakeBlob(self, name)


def lease(bucket, *, now=NOW, owner="run-a", ttl=60):
    return RunLease(
        bucket,
        dataset="youtube_analytics_staging",
        domain="reporting-writer",
        owner={"run_id": owner, "entrypoint": "test"},
        ttl_seconds=ttl,
        now=lambda: now,
    )


def test_acquire_uses_dataset_scoped_name_and_atomic_create():
    bucket = FakeBucket()
    acquired = lease(bucket).acquire()

    assert acquired.generation == 41
    assert acquired.object_name == "leases/youtube_analytics_staging/reporting-writer.json"
    payload = json.loads(bucket.objects[acquired.object_name]["payload"])
    assert payload == {
        "acquired_at": "2026-09-13T18:00:00+00:00",
        "dataset": "youtube_analytics_staging",
        "domain": "reporting-writer",
        "expires_at": "2026-09-13T18:01:00+00:00",
        "owner": {"entrypoint": "test", "run_id": "run-a"},
    }


def test_live_lease_raises_with_existing_owner_and_never_overwrites():
    bucket = FakeBucket()
    first = lease(bucket).acquire()

    with pytest.raises(LeaseHeld) as caught:
        lease(bucket, owner="run-b", now=NOW + timedelta(seconds=30)).acquire()

    assert caught.value.record.owner["run_id"] == "run-a"
    assert bucket.objects[first.object_name]["generation"] == first.generation


def test_expired_lease_fails_closed_instead_of_stealing():
    bucket = FakeBucket()
    first = lease(bucket).acquire()

    with pytest.raises(ExpiredLease) as caught:
        lease(bucket, owner="run-b", now=NOW + timedelta(seconds=61)).acquire()

    assert caught.value.record.generation == first.generation
    assert json.loads(bucket.objects[first.object_name]["payload"])["owner"]["run_id"] == "run-a"


def test_release_deletes_only_the_generation_it_acquired():
    bucket = FakeBucket()
    acquired = lease(bucket).acquire()
    acquired.release()
    assert acquired.object_name not in bucket.objects


def test_old_owner_cannot_delete_a_successor_generation():
    bucket = FakeBucket()
    old = lease(bucket).acquire()
    name = old.object_name
    del bucket.objects[name]
    successor = lease(bucket, owner="run-b").acquire()

    with pytest.raises(PreconditionFailed):
        old.release()

    assert bucket.objects[name]["generation"] == successor.generation


def test_context_manager_releases_after_body_failure():
    bucket = FakeBucket()
    with pytest.raises(RuntimeError):
        with lease(bucket) as acquired:
            assert acquired.object_name in bucket.objects
            raise RuntimeError("boom")
    assert acquired.object_name not in bucket.objects


def test_release_failure_does_not_replace_the_body_exception(caplog):
    bucket = FakeBucket()
    bucket.delete_error = RuntimeError("storage unavailable")

    with pytest.raises(ValueError, match="body failed"):
        with lease(bucket):
            raise ValueError("body failed")

    assert "Writer lease release failed" in caplog.text


def test_release_failure_after_success_is_not_silently_ignored(caplog):
    bucket = FakeBucket()
    bucket.delete_error = RuntimeError("storage unavailable")

    with pytest.raises(RuntimeError, match="storage unavailable"):
        with lease(bucket):
            pass

    assert "Writer lease release failed" in caplog.text


def test_inspect_returns_none_or_a_generation_bound_record():
    bucket = FakeBucket()
    assert inspect_lease(bucket, "youtube_analytics_staging", "reporting-writer") is None
    acquired = lease(bucket).acquire()
    record = inspect_lease(bucket, "youtube_analytics_staging", "reporting-writer")
    assert record.generation == acquired.generation
    assert record.owner == {"entrypoint": "test", "run_id": "run-a"}
