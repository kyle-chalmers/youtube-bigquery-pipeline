#!/usr/bin/env python3
"""Inspect or generation-check clear one pipeline writer lease.

Expired leases are never cleared automatically. Confirm that no matching function or
manual writer is active, inspect the current record, then pass its exact generation to
the clear command.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime

from google.cloud import storage

import _bootstrap  # noqa: F401

from run_lease import LeaseRecord, inspect_lease


def record_as_dict(record: LeaseRecord) -> dict:
    value = asdict(record)
    for key in ("acquired_at", "expires_at"):
        value[key] = value[key].isoformat()
    return value


def clear_lease(
    bucket,
    dataset: str,
    domain: str,
    *,
    generation: int,
    active_writer_checked: bool,
) -> LeaseRecord:
    if not active_writer_checked:
        raise RuntimeError("confirm no active writer before clearing a lease")
    current = inspect_lease(bucket, dataset, domain)
    if current is None:
        raise RuntimeError("no current lease exists")
    if current.generation != generation:
        raise RuntimeError(
            f"expected generation {generation}, current generation {current.generation}; inspect again"
        )
    now = datetime.now(current.expires_at.tzinfo)
    if current.expires_at > now:
        raise RuntimeError("refusing to clear a live unexpired lease")
    bucket.blob(current.object_name).delete(if_generation_match=generation)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("status", "clear"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--domain", required=True, choices=("analytics-writer", "reporting-writer"))
    parser.add_argument("--generation", type=int)
    parser.add_argument("--confirm-dataset")
    parser.add_argument(
        "--confirm-no-active-writer",
        action="store_true",
        help="attest that Cloud Run and manual-writer activity was checked and is idle",
    )
    args = parser.parse_args()

    project = _bootstrap.resolve_project()
    bucket = storage.Client(project=project, credentials=_bootstrap.google_cloud_credentials()).bucket(args.bucket)
    current = inspect_lease(bucket, args.dataset, args.domain)
    if args.action == "status":
        if current is None:
            print(json.dumps({"lease": None}, sort_keys=True))
        else:
            value = record_as_dict(current)
            value["expired_as_of"] = datetime.now(current.expires_at.tzinfo).isoformat()
            value["expired"] = current.expires_at <= datetime.now(current.expires_at.tzinfo)
            print(json.dumps(value, sort_keys=True))
        return 0

    if args.generation is None:
        parser.error("clear requires --generation from a fresh status result")
    if args.confirm_dataset != args.dataset:
        parser.error("clear requires --confirm-dataset matching --dataset")
    cleared = clear_lease(
        bucket,
        args.dataset,
        args.domain,
        generation=args.generation,
        active_writer_checked=args.confirm_no_active_writer,
    )
    print(json.dumps({"cleared": record_as_dict(cleared)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
