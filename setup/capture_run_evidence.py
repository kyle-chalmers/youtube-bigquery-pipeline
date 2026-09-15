#!/usr/bin/env python3
"""Capture one Cloud Run invocation and its run-labeled BigQuery jobs under .internal."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from google.cloud import bigquery

import _bootstrap


INTERNAL_ROOT = (_bootstrap.REPO_ROOT / ".internal").resolve()


def _logging_read(project: str, log_filter: str) -> list[dict]:
    command = [
        "gcloud", "logging", "read", log_filter,
        f"--project={project}", "--freshness=2h", "--order=asc", "--limit=1000", "--format=json",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Cloud Logging capture failed: {result.stderr.strip()}")
    value = json.loads(result.stdout or "[]")
    if not isinstance(value, list):
        raise RuntimeError("Cloud Logging output was not a JSON list")
    return value


def jobs_query(region: str) -> str:
    if not region.replace("-", "").isalnum():
        raise ValueError("invalid BigQuery region")
    return f"""
SELECT creation_time, end_time, job_id, job_type, statement_type, state, error_result, query,
       total_bytes_processed, total_bytes_billed, destination_table, labels
FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 HOUR)
  AND EXISTS (
    SELECT 1 FROM UNNEST(labels)
    WHERE key = 'pipeline_run_id' AND value = @run_id
  )
ORDER BY creation_time, job_id
"""


def successful_transaction_jobs(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if row.get("statement_type") == "SCRIPT"
        and row.get("state") == "DONE"
        and not row.get("error_result")
        and "BEGIN TRANSACTION" in str(row.get("query", "")).upper()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bq-region", default="us-central1")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--require-transaction", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if not output_dir.is_relative_to(INTERNAL_ROOT):
        parser.error("run evidence must stay under .internal")
    output_dir.mkdir(parents=True, exist_ok=False)

    project = _bootstrap.resolve_project()
    escaped_run_id = args.run_id.replace('"', '\\"')
    escaped_service = args.service.replace('"', '\\"')
    starts = _logging_read(
        project,
        f'resource.type="cloud_run_revision" AND '
        f'resource.labels.service_name="{escaped_service}" AND '
        f'(textPayload:"run_id={escaped_run_id}" OR jsonPayload.message:"run_id={escaped_run_id}")',
    )
    traces = sorted({str(entry.get("trace")) for entry in starts if entry.get("trace")})
    if len(traces) != 1:
        raise RuntimeError(f"expected exactly one Cloud Run trace for run_id, found {len(traces)}")
    logs = _logging_read(project, f'trace="{traces[0]}"')
    (output_dir / "cloud_run_logs.json").write_text(
        json.dumps(logs, indent=2, sort_keys=True, default=str) + "\n"
    )

    client = bigquery.Client(
        project=project,
        location=args.bq_region,
        credentials=_bootstrap.google_cloud_credentials(),
    )
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("run_id", "STRING", args.run_id)
    ])
    rows = [dict(row) for row in client.query(jobs_query(args.bq_region), job_config=config).result()]
    (output_dir / "bigquery_jobs.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True, default=str) + "\n"
    )
    transaction_jobs = successful_transaction_jobs(rows)
    manifest = {
        "bigquery_job_count": len(rows),
        "cloud_run_log_count": len(logs),
        "run_id": args.run_id,
        "service": args.service,
        "trace": traces[0],
        "transaction_job_count": len(transaction_jobs),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, sort_keys=True))
    if args.require_transaction and not transaction_jobs:
        raise RuntimeError("no successful run-labeled BigQuery transaction script was found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
