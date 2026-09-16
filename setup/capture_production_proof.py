#!/usr/bin/env python3
"""Dry-run and capture every query in the generated production proof pack.

The command is read-only. Results, rendered SQL, query scan estimates, archived CSVs
and current runtime configuration are written under the ignored .internal directory.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from google.cloud import bigquery, storage

import _bootstrap
from report_specs import SPECS
from reporting_parser import parse_report


PROOF_SQL = _bootstrap.REPO_ROOT / "sql" / "verification" / "production_proof.sql"
INTERNAL_ROOT = _bootstrap.REPO_ROOT / ".internal"


def parse_blocks(text: str) -> list[tuple[str, str]]:
    blocks = []
    name = None
    collecting = False
    sql_lines = []
    for line in text.splitlines():
        if line.startswith("-- --") and not set(line.removeprefix("-- ")) <= {"-"}:
            name = line.removeprefix("-- --").strip()
            collecting = False
            sql_lines = []
            continue
        if line.startswith("-- ") and len(line.removeprefix("-- ")) >= 20 \
                and set(line.removeprefix("-- ")) == {"-"}:
            if name and collecting:
                blocks.append((name, "\n".join(sql_lines).strip()))
                name = None
                collecting = False
                sql_lines = []
            elif name:
                collecting = True
            continue
        if name and collecting:
            sql_lines.append(line)
    if name and collecting:
        blocks.append((name, "\n".join(sql_lines).strip()))
    return blocks


def _csv_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return json.dumps(value, default=str, sort_keys=True)


def execute_query(client: bigquery.Client, name: str, sql: str, output_dir: Path) -> dict[str, Any]:
    dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dry_job = client.query(sql, job_config=dry_config)
    except Exception as error:
        raise RuntimeError(f"proof query {name!r} dry-run failed") from error
    query_job = client.query(sql)
    rows = query_job.result(page_size=10_000)
    target = output_dir / f"{name}.csv"
    count = 0
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([field.name for field in rows.schema])
        for row in rows:
            writer.writerow([_csv_value(value) for value in row.values()])
            count += 1
    return {
        "bytes_estimated": int(dry_job.total_bytes_processed or 0),
        "job_id": query_job.job_id,
        "output": target.name,
        "rows": count,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def capture_archives(
    bq: bigquery.Client,
    gcs: storage.Client,
    project: str,
    dataset: str,
    affected_date: str,
    output_dir: Path,
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT DISTINCT report_type, report_id, gcs_uri, content_sha256 "
        f"FROM `{project}.{dataset}.reporting_ingest_ledger` "
        "WHERE report_date = @report_date AND report_type IN "
        "('channel_device_os_a3', 'channel_traffic_source_a3') ORDER BY report_type, report_id"
    )
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("report_date", "DATE", affected_date)
    ])
    records = []
    archive_dir = output_dir / "archives"
    archive_dir.mkdir()
    for row in bq.query(sql, job_config=config).result():
        uri = urlparse(row.gcs_uri)
        blob = gcs.bucket(uri.netloc).blob(uri.path.lstrip("/"))
        blob.reload()
        compressed = blob.download_as_bytes(raw_download=True)
        raw = gzip.decompress(compressed)
        calculated = hashlib.sha256(raw).hexdigest()
        filename = f"{row.report_type}__{row.report_id}.csv"
        (archive_dir / filename).write_bytes(raw)
        metadata_file = archive_dir / f"{row.report_type}__{row.report_id}.metadata.json"
        metadata = {
            "blob_metadata": blob.metadata or {},
            "bucket": uri.netloc,
            "calculated_sha256": calculated,
            "generation": str(blob.generation),
            "ledger_sha256": row.content_sha256,
            "object": uri.path.lstrip("/"),
            "raw_bytes": len(raw),
            "report_id": row.report_id,
            "report_type": row.report_type,
            "sha256_equal": calculated == row.content_sha256 == (blob.metadata or {}).get("csv_sha256"),
        }
        metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n")
        records.append({"csv": str(Path("archives") / filename), "metadata": str(metadata_file.relative_to(output_dir)), **metadata})
    return records


def _canonical_key(row: dict[str, Any], columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        json.dumps(_csv_value(row.get(column)), sort_keys=True, default=str)
        for column in columns
    )


def capture_archive_reconciliation(
    bq: bigquery.Client,
    project: str,
    dataset: str,
    archives: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Write complete canonical rows present on only one side of archive versus BigQuery."""
    records = []
    for archive in archives:
        spec = SPECS[archive["report_type"]]
        archive_rows = parse_report((output_dir / archive["csv"]).read_bytes(), spec)
        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("report_date", "DATE", archive["blob_metadata"]["report_date"]),
            bigquery.ScalarQueryParameter("report_id", "STRING", archive["report_id"]),
        ])
        selected = ", ".join(spec.columns)
        table_rows = [
            dict(row)
            for row in bq.query(
                f"SELECT {selected} FROM `{project}.{dataset}.{spec.table}` "
                "WHERE report_date = @report_date AND report_id = @report_id",
                job_config=config,
            ).result()
        ]
        archive_by_key = {_canonical_key(row, spec.columns): row for row in archive_rows}
        table_by_key = {_canonical_key(row, spec.columns): row for row in table_rows}
        directions = (
            ("archive_only", archive_by_key, table_by_key),
            ("bigquery_only", table_by_key, archive_by_key),
        )
        outputs = {}
        for direction, left, right in directions:
            target = output_dir / f"{direction}_{spec.report_type}.csv"
            different = [left[key] for key in sorted(set(left) - set(right))]
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(spec.columns)
                for row in different:
                    writer.writerow([_csv_value(row.get(column)) for column in spec.columns])
            outputs[direction] = {
                "output": target.name,
                "rows": len(different),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        records.append({
            "archive_canonical_rows": len(archive_by_key),
            "bigquery_canonical_rows": len(table_by_key),
            "report_id": archive["report_id"],
            "report_type": spec.report_type,
            **outputs,
        })
    return records


def capture_gcloud(
    project: str,
    region: str,
    output_dir: Path,
    lock_bucket: str | None,
    resource_suffix: str = "",
) -> list[dict[str, Any]]:
    commands = []
    for base_function in ("youtube-bigquery-pipeline", "youtube-reporting-ingest", "youtube-analytics-refresh"):
        function = f"{base_function}{resource_suffix}"
        commands.append((f"function_{function}", [
            "gcloud", "functions", "describe", function, "--gen2", f"--region={region}",
            f"--project={project}", "--format=json",
        ]))
    for base_job in ("youtube-daily-snapshot", "youtube-reporting-daily", "youtube-analytics-refresh-weekly"):
        job = f"{base_job}{resource_suffix}"
        commands.append((f"scheduler_{job}", [
            "gcloud", "scheduler", "jobs", "describe", job, f"--location={region}",
            f"--project={project}", "--format=json",
        ]))
    commands.append(("monitoring_policies", [
        "gcloud", "beta", "monitoring", "policies", "list", f"--project={project}", "--format=json",
    ]))
    if lock_bucket:
        commands.extend([
            ("lock_bucket", ["gcloud", "storage", "buckets", "describe", f"gs://{lock_bucket}", f"--project={project}", "--format=json"]),
            ("lock_bucket_iam", ["gcloud", "storage", "buckets", "get-iam-policy", f"gs://{lock_bucket}", f"--project={project}", "--format=json"]),
            ("lease_objects", ["gcloud", "storage", "ls", "--json", "--recursive", f"gs://{lock_bucket}"]),
        ])
    results = []
    config_dir = output_dir / "configuration"
    config_dir.mkdir()
    for name, command in commands:
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
        output = config_dir / f"{name}.json"
        output.write_text(proc.stdout if proc.stdout else "[]\n")
        results.append({"command": command, "exit_code": proc.returncode, "output": str(output.relative_to(output_dir)),
                        "stderr": proc.stderr.strip()})
    failures = [result for result in results if result["exit_code"] != 0]
    if failures:
        names = ", ".join(Path(result["output"]).stem for result in failures)
        raise RuntimeError(f"configuration capture failed for: {names}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", default="youtube_analytics")
    parser.add_argument("--affected-date", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--lock-bucket")
    parser.add_argument("--resource-suffix", default="")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    date.fromisoformat(args.affected_date)

    project = _bootstrap.resolve_project()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (
        INTERNAL_ROOT / "production-validation" / stamp
    ).resolve()
    if not output_dir.is_relative_to(INTERNAL_ROOT.resolve()):
        parser.error("proof output must stay under .internal")
    output_dir.mkdir(parents=True, exist_ok=False)

    template = PROOF_SQL.read_text()
    rendered = template.replace("${PROJECT_ID}", project).replace("${BQ_DATASET}", args.dataset).replace(
        "${AFFECTED_DATE}", args.affected_date
    )
    rendered_file = output_dir / "production_proof_rendered.sql"
    rendered_file.write_text(rendered)
    blocks = parse_blocks(rendered)
    if not blocks:
        raise RuntimeError("proof SQL contains no tagged query blocks")

    credentials = _bootstrap.google_cloud_credentials()
    bq = bigquery.Client(project=project, location=args.region, credentials=credentials)
    gcs = storage.Client(project=project, credentials=credentials)
    queries = {}
    for name, sql in blocks:
        queries[name] = execute_query(bq, name, sql, output_dir)
    archives = capture_archives(bq, gcs, project, args.dataset, args.affected_date, output_dir)
    archive_reconciliation = capture_archive_reconciliation(
        bq, project, args.dataset, archives, output_dir
    )
    if args.resource_suffix and not re.fullmatch(r"-[a-z0-9-]+", args.resource_suffix):
        parser.error("--resource-suffix must be empty or a lowercase hyphenated suffix")
    configuration = capture_gcloud(
        project, args.region, output_dir, args.lock_bucket, args.resource_suffix
    )
    manifest = {
        "affected_date": args.affected_date,
        "archives": archives,
        "archive_reconciliation": archive_reconciliation,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "configuration": configuration,
        "dataset": args.dataset,
        "project": project,
        "queries": queries,
        "resource_suffix": args.resource_suffix,
        "rendered_sql": rendered_file.name,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    print(output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
