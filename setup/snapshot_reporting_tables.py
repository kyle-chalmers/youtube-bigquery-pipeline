#!/usr/bin/env python3
"""Plan, create, or restore 30-day snapshots for every Reporting table and ledger."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

from google.cloud import bigquery

import _bootstrap

from report_specs import LEDGER_TABLE, SPECS
from run_lease import manual_writer_lease


REPORTING_TABLES = tuple(sorted(spec.table for spec in SPECS.values())) + (LEDGER_TABLE,)


def validate_prefix(prefix: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", prefix):
        raise ValueError("snapshot prefix must contain only letters, digits and underscores")
    return prefix


def snapshot_name(table: str, prefix: str) -> str:
    return f"{table}__{validate_prefix(prefix)}"


def create_sql(dataset_ref: str, table: str, prefix: str, expiration_days: int) -> str:
    return (
        f"CREATE SNAPSHOT TABLE `{dataset_ref}.{snapshot_name(table, prefix)}` "
        f"CLONE `{dataset_ref}.{table}` "
        "OPTIONS(expiration_timestamp=TIMESTAMP_ADD(CURRENT_TIMESTAMP(), "
        f"INTERVAL {int(expiration_days)} DAY))"
    )


def restore_sql(dataset_ref: str, table: str, prefix: str) -> str:
    return (
        f"CREATE OR REPLACE TABLE `{dataset_ref}.{table}` "
        f"CLONE `{dataset_ref}.{snapshot_name(table, prefix)}`"
    )


def fingerprint_sql(dataset_ref: str, table: str) -> str:
    return (
        "SELECT FORMAT('%d:%d:%s', COUNT(*), COALESCE(BIT_XOR(row_hash), 0), "
        "CAST(COALESCE(SUM(CAST(row_hash AS BIGNUMERIC)), 0) AS STRING)) AS fingerprint "
        "FROM (SELECT FARM_FINGERPRINT(TO_JSON_STRING(row_value)) AS row_hash "
        f"FROM `{dataset_ref}.{table}` AS row_value)"
    )


def _query_with_retries(client, statement: str, attempts: int = 3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return client.query(statement).result()
        except Exception as error:
            last_error = error
            if attempt == attempts:
                break
            print(
                f"snapshot query failed, retrying attempt {attempt + 1}/{attempts}: "
                f"{type(error).__name__}",
                file=sys.stderr,
            )
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"snapshot query failed after {attempts} attempts") from last_error


def _fingerprint(client, dataset_ref: str, table: str) -> str:
    rows = list(_query_with_retries(client, fingerprint_sql(dataset_ref, table)))
    if len(rows) != 1:
        raise RuntimeError(f"fingerprint query returned {len(rows)} rows for {table}")
    return rows[0]["fingerprint"]


def preflight_restore_fingerprints(client, dataset_ref: str, prefix: str) -> dict[str, str]:
    """Read every source snapshot before replacing the first live table."""
    return {
        table: _fingerprint(client, dataset_ref, snapshot_name(table, prefix))
        for table in REPORTING_TABLES
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "create", "restore"))
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--expiration-days", type=int, default=30)
    parser.add_argument("--confirm-dataset")
    parser.add_argument("--confirm-restore-prefix")
    args = parser.parse_args()
    validate_prefix(args.prefix)
    if args.expiration_days <= 0:
        parser.error("--expiration-days must be positive")

    project = _bootstrap.resolve_project()
    dataset_ref = f"{project}.{args.dataset}"
    statements = [
        create_sql(dataset_ref, table, args.prefix, args.expiration_days)
        if args.action != "restore"
        else restore_sql(dataset_ref, table, args.prefix)
        for table in REPORTING_TABLES
    ]
    if args.action == "plan":
        print(json.dumps({
            "action": "plan",
            "statements": statements,
            "table_count": len(REPORTING_TABLES),
            "tables": REPORTING_TABLES,
            "writes": 0,
        }, indent=2, sort_keys=True))
        return 0

    if args.confirm_dataset != args.dataset:
        parser.error(f"{args.action} requires --confirm-dataset matching --dataset")
    if args.action == "restore" and args.confirm_restore_prefix != args.prefix:
        parser.error("restore requires --confirm-restore-prefix matching --prefix")

    client = bigquery.Client(
        project=project,
        credentials=_bootstrap.google_cloud_credentials(),
    )
    completed = []
    with manual_writer_lease(
        project_id=project,
        dataset=args.dataset,
        domain="reporting-writer",
        entrypoint=f"snapshot_reporting_tables_{args.action}",
        dry_run=args.action == "plan",
    ):
        restore_fingerprints = (
            preflight_restore_fingerprints(client, dataset_ref, args.prefix)
            if args.action == "restore"
            else {}
        )
        for table, statement in zip(REPORTING_TABLES, statements, strict=True):
            _query_with_retries(client, statement)
            snapshot = snapshot_name(table, args.prefix)
            source = table if args.action == "create" else snapshot
            target = snapshot if args.action == "create" else table
            source_fingerprint = restore_fingerprints.get(table) or _fingerprint(
                client, dataset_ref, source
            )
            target_fingerprint = _fingerprint(client, dataset_ref, target)
            if source_fingerprint != target_fingerprint:
                raise RuntimeError(
                    f"{args.action} fingerprint mismatch for {table}: "
                    f"{source_fingerprint} != {target_fingerprint}"
                )
            completed.append(table)
            print(json.dumps({
                "action": args.action,
                "fingerprint": target_fingerprint,
                "table": table,
                "verified": True,
            }, sort_keys=True))
    print(json.dumps({"action": args.action, "completed": completed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
