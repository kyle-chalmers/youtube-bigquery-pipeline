"""Cross-entrypoint writer lease backed by one generation-bound GCS object.

Creation uses a zero-generation precondition, so exactly one caller acquires a named
lease. Expiry is diagnostic only. A caller never steals an expired object because the
process that created it may still be running after its HTTP request timed out.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from google.api_core.exceptions import NotFound, PreconditionFailed

LEASE_HELD_LOG = "Writer lease held"
LEASE_EXPIRED_LOG = "Writer lease expired"
LEASE_RELEASE_FAILED_LOG = "Writer lease release failed"

logger = logging.getLogger(__name__)


def _object_name(dataset: str, domain: str) -> str:
    return f"leases/{dataset}/{domain}.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LeaseRecord:
    object_name: str
    generation: int
    dataset: str
    domain: str
    owner: dict[str, Any]
    acquired_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= _utcnow()


class LeaseHeld(RuntimeError):
    """A named lease exists and has not expired."""

    def __init__(self, record: LeaseRecord):
        self.record = record
        super().__init__(f"writer lease held by {record.owner!r} until {record.expires_at.isoformat()}")


class ExpiredLease(RuntimeError):
    """A named lease expired and requires generation-checked operator cleanup."""

    def __init__(self, record: LeaseRecord):
        self.record = record
        super().__init__(f"writer lease expired at {record.expires_at.isoformat()}; operator cleanup required")


def inspect_lease(bucket: Any, dataset: str, domain: str) -> LeaseRecord | None:
    """Return the current generation-bound record without changing it."""
    name = _object_name(dataset, domain)
    blob = bucket.blob(name)
    try:
        blob.reload()
        payload = json.loads(blob.download_as_bytes())
    except (NotFound, KeyError):
        return None
    return LeaseRecord(
        object_name=name,
        generation=int(blob.generation),
        dataset=payload["dataset"],
        domain=payload["domain"],
        owner=dict(payload["owner"]),
        acquired_at=datetime.fromisoformat(payload["acquired_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
    )


class RunLease:
    """Acquire and release one dataset-scoped writer lease."""

    def __init__(
        self,
        bucket: Any,
        *,
        dataset: str,
        domain: str,
        owner: dict[str, Any],
        ttl_seconds: int = 2100,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.bucket = bucket
        self.dataset = dataset
        self.domain = domain
        self.owner = dict(owner)
        self.ttl_seconds = ttl_seconds
        self._now = now
        self.object_name = _object_name(dataset, domain)
        self.generation: int | None = None

    def acquire(self) -> RunLease:
        now = self._now().astimezone(timezone.utc)
        payload = {
            "acquired_at": now.isoformat(),
            "dataset": self.dataset,
            "domain": self.domain,
            "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "owner": self.owner,
        }
        blob = self.bucket.blob(self.object_name)
        try:
            blob.upload_from_string(
                json.dumps(payload, sort_keys=True),
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed as error:
            record = inspect_lease(self.bucket, self.dataset, self.domain)
            if record is None:
                raise RuntimeError("writer lease changed while it was being inspected") from error
            if record.expires_at <= now:
                raise ExpiredLease(record) from error
            raise LeaseHeld(record) from error
        if blob.generation is None:
            blob.reload()
        self.generation = int(blob.generation)
        return self

    def release(self) -> None:
        if self.generation is None:
            return
        self.bucket.blob(self.object_name).delete(if_generation_match=self.generation)
        self.generation = None

    def __enter__(self) -> RunLease:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.release()
        except Exception:  # noqa: BLE001 - preserve the pipeline error when cleanup also fails
            logger.error(
                f"{LEASE_RELEASE_FAILED_LOG}: dataset={self.dataset} domain={self.domain} "
                f"generation={self.generation}"
            )
            if exc_type is None:
                raise
        return False


def build_run_lease(
    *,
    project_id: str,
    bucket_name: str,
    dataset: str,
    domain: str,
    run_id: str,
    entrypoint: str,
    ttl_seconds: int = 2100,
) -> RunLease:
    """Build a production lease from explicit deployment configuration."""
    if not bucket_name:
        raise ValueError("PIPELINE_LOCK_BUCKET is required for every table writer")
    from google.cloud import storage

    from gcloud_credentials import credentials_from_environment

    credentials = credentials_from_environment()
    bucket = storage.Client(project=project_id, credentials=credentials).bucket(bucket_name)
    return RunLease(
        bucket,
        dataset=dataset,
        domain=domain,
        owner={"entrypoint": entrypoint, "run_id": run_id},
        ttl_seconds=ttl_seconds,
    )


def manual_writer_lease(
    *,
    project_id: str,
    dataset: str,
    domain: str,
    entrypoint: str,
    dry_run: bool = False,
    run_id: str | None = None,
    ttl_seconds: int = 2100,
):
    """Return a lease context for a manual writer, or a no-op for a true dry run."""
    if dry_run:
        return nullcontext()
    return build_run_lease(
        project_id=project_id,
        bucket_name=os.environ.get("PIPELINE_LOCK_BUCKET", ""),
        dataset=dataset,
        domain=domain,
        run_id=run_id or f"manual-{uuid.uuid4()}",
        entrypoint=entrypoint,
        ttl_seconds=ttl_seconds,
    )
