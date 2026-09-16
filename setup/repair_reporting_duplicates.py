#!/usr/bin/env python3
"""Validate and repair duplicated Reporting partitions from their archived CSV source.

Dry-run is the default. Applying a repair requires an exact dataset confirmation and a
snapshot prefix. Every target is re-read and revalidated while the reporting writer
lease is held before any snapshot or replacement is created.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from google.cloud import bigquery, storage

import _bootstrap  # noqa: F401

from partition_replacer import _schema_for
from report_specs import LEDGER_TABLE, PROVENANCE_COLUMNS, SPECS, ReportSpec
from reporting_parser import parse_report
from run_lease import manual_writer_lease


@dataclass(frozen=True)
class RepairValidation:
    report_type: str
    report_date: str
    report_id: str
    job_id: str
    gcs_uri: str
    sha256: str
    physical_rows: int
    canonical_rows: int
    copy_count: int
    ledger_rows: int
    replacement_rows: list[dict[str, Any]]
    ledger: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return {
            "canonical_rows": self.canonical_rows,
            "copy_count": self.copy_count,
            "gcs_uri": self.gcs_uri,
            "job_id": self.job_id,
            "ledger_rows": self.ledger_rows,
            "physical_rows": self.physical_rows,
            "report_date": self.report_date,
            "report_id": self.report_id,
            "report_type": self.report_type,
            "sha256": self.sha256,
        }


def _normal(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _key(row: dict[str, Any], columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(_normal(row.get(column)) for column in columns)


def download_archive_csv(blob) -> bytes:
    """Download the stored object bytes and explicitly inflate the gzip archive."""
    return gzip.decompress(blob.download_as_bytes(raw_download=True))


def validate_repair_inputs(
    spec: ReportSpec,
    report_date: str,
    table_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    archive_csv: bytes,
    archive_metadata: dict[str, Any],
    *,
    require_duplicates: bool = True,
) -> RepairValidation:
    """Prove the table is uniform duplication of the exact archived CSV."""
    if not table_rows:
        raise RuntimeError("target partition is empty")
    if not ledger_rows:
        raise RuntimeError("target partition has no ledger rows")

    sha = hashlib.sha256(archive_csv).hexdigest()
    if archive_metadata.get("csv_sha256") != sha:
        raise RuntimeError("archive metadata sha256 does not match the downloaded CSV")

    report_ids = {str(row.get("report_id")) for row in table_rows}
    if len(report_ids) != 1:
        raise RuntimeError(f"partition contains {len(report_ids)} report ids, expected one")
    report_id = next(iter(report_ids))
    if archive_metadata.get("report_id") != report_id:
        raise RuntimeError("archive report_id does not match the table")
    if archive_metadata.get("report_type") != spec.report_type:
        raise RuntimeError("archive report_type does not match the target")
    if archive_metadata.get("report_date") != report_date:
        raise RuntimeError("archive report_date does not match the target")

    full_columns = spec.columns + tuple(name for name, _, _ in PROVENANCE_COLUMNS if name != "ingested_at")
    counts = Counter(_key(row, full_columns) for row in table_rows)
    multiplicities = set(counts.values())
    minimum_copies = 2 if require_duplicates else 1
    if len(multiplicities) != 1 or next(iter(multiplicities)) < minimum_copies:
        raise RuntimeError("partition is not made of uniform duplicate copies")
    copy_count = next(iter(multiplicities))
    canonical_by_key = {}
    for row in table_rows:
        canonical_by_key.setdefault(_key(row, full_columns), dict(row))
    canonical = list(canonical_by_key.values())

    grain_counts = Counter(_key(row, spec.grain_columns) for row in canonical)
    if any(n != 1 for n in grain_counts.values()):
        raise RuntimeError("canonical table rows are not unique at the native grain")

    parsed = parse_report(archive_csv, spec)
    if Counter(_key(row, spec.columns) for row in parsed) != Counter(
        _key(row, spec.columns) for row in canonical
    ):
        raise RuntimeError("archive rows differ from the canonical table rows")

    ledger_columns = (
        "report_id", "job_id", "report_type", "report_date", "report_create_time",
        "status", "row_count", "csv_bytes", "content_sha256", "gcs_uri", "load_source", "error",
    )
    ledger_keys = {_key(row, ledger_columns) for row in ledger_rows}
    if len(ledger_keys) != 1 or len(ledger_rows) < minimum_copies:
        raise RuntimeError("ledger is not made of duplicate copies of one record")
    ledger = dict(ledger_rows[0])
    if ledger.get("status") != "loaded" or ledger.get("report_id") != report_id:
        raise RuntimeError("ledger does not identify the loaded table report")
    if ledger.get("content_sha256") != sha:
        raise RuntimeError("ledger sha256 does not match the downloaded CSV")
    if ledger.get("row_count") != len(parsed) or ledger.get("csv_bytes") != len(archive_csv):
        raise RuntimeError("ledger row_count or csv_bytes does not match the archive")
    if ledger.get("gcs_uri") is None:
        raise RuntimeError("ledger has no archive URI")
    if archive_metadata.get("job_id") != ledger.get("job_id"):
        raise RuntimeError("archive job_id does not match the ledger")

    return RepairValidation(
        report_type=spec.report_type,
        report_date=report_date,
        report_id=report_id,
        job_id=str(ledger["job_id"]),
        gcs_uri=str(ledger["gcs_uri"]),
        sha256=sha,
        physical_rows=len(table_rows),
        canonical_rows=len(canonical),
        copy_count=copy_count,
        ledger_rows=len(ledger_rows),
        replacement_rows=canonical,
        ledger=ledger,
    )


def build_repair_script(dataset_ref: str, spec: ReportSpec, work_table: str) -> str:
    target = f"`{dataset_ref}.{spec.table}`"
    work = f"`{dataset_ref}.{work_table}`"
    ledger = f"`{dataset_ref}.{LEDGER_TABLE}`"
    cols = ", ".join(spec.columns + tuple(name for name, _, _ in PROVENANCE_COLUMNS))
    grain = ", ".join(spec.grain_columns)
    return f"""
BEGIN
  BEGIN TRANSACTION;
  UPDATE `{dataset_ref}.pipeline_write_mutex_reporting`
  SET touched_at = CURRENT_TIMESTAMP()
  WHERE mutex_name = 'reporting';
  ASSERT @@row_count = 1 AS 'refused: reporting pipeline write mutex must contain exactly one row';
  ASSERT (SELECT COUNT(*) FROM {work}) > 0 AS 'refused: repair work table is empty';
  ASSERT (SELECT COUNT(DISTINCT report_date) FROM {work}) = 1
     AND (SELECT ANY_VALUE(report_date) FROM {work}) = @report_date
     AS 'refused: repair work table date mismatch';
  ASSERT NOT EXISTS (SELECT 1 FROM {work} GROUP BY {grain} HAVING COUNT(*) > 1)
     AS 'refused: repair work table native grain is duplicated';

  DELETE FROM {target} WHERE report_date = @report_date;
  INSERT INTO {target} ({cols}) SELECT {cols} FROM {work};

  UPDATE {ledger} SET status = 'superseded', error = NULL
  WHERE job_id = @job_id AND report_date = @report_date AND report_id != @report_id
    AND status IN ('loaded', 'header_only', 'header_only_conflict');
  DELETE FROM {ledger} WHERE report_id = @report_id;
  INSERT INTO {ledger}
    (report_id, job_id, report_type, report_date, report_create_time, status, row_count,
     csv_bytes, content_sha256, gcs_uri, load_source, ingested_at, error)
  VALUES
    (@report_id, @job_id, @report_type, @report_date, @report_create_time, 'loaded', @row_count,
     @csv_bytes, @content_sha256, @gcs_uri, @load_source, CURRENT_TIMESTAMP(), NULL);
  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;
"""


def _query_rows(client, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    config = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(row) for row in client.query(sql, job_config=config).result()]


def load_validation(
    client,
    storage_client,
    dataset_ref: str,
    spec: ReportSpec,
    report_date: str,
    *,
    require_duplicates: bool = True,
) -> RepairValidation:
    date_param = [bigquery.ScalarQueryParameter("report_date", "DATE", report_date)]
    table_rows = _query_rows(
        client,
        f"SELECT * FROM `{dataset_ref}.{spec.table}` WHERE report_date = @report_date "
        f"ORDER BY {', '.join(spec.grain_columns)}, ingested_at",
        date_param,
    )
    ledger_rows = _query_rows(
        client,
        f"SELECT * FROM `{dataset_ref}.{LEDGER_TABLE}` WHERE report_type = @report_type "
        "AND report_date = @report_date ORDER BY report_id, ingested_at",
        date_param + [bigquery.ScalarQueryParameter("report_type", "STRING", spec.report_type)],
    )
    uris = {row.get("gcs_uri") for row in ledger_rows if row.get("gcs_uri")}
    if len(uris) != 1:
        raise RuntimeError(f"expected one archive URI, found {len(uris)}")
    uri = next(iter(uris))
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme != "gs" or not parsed_uri.netloc or not parsed_uri.path.strip("/"):
        raise RuntimeError("ledger archive URI is not a valid gs:// URI")
    blob = storage_client.bucket(parsed_uri.netloc).blob(parsed_uri.path.lstrip("/"))
    blob.reload()
    raw = download_archive_csv(blob)
    return validate_repair_inputs(
        spec,
        report_date,
        table_rows,
        ledger_rows,
        raw,
        blob.metadata or {},
        require_duplicates=require_duplicates,
    )


def create_snapshots(client, dataset_ref: str, specs: list[ReportSpec], prefix: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_]+", prefix):
        raise ValueError("snapshot prefix must contain only letters, digits and underscores")
    tables = sorted({spec.table for spec in specs} | {LEDGER_TABLE})
    created = []
    for table in tables:
        snapshot = f"{table}__{prefix}"
        sql = (
            f"CREATE SNAPSHOT TABLE `{dataset_ref}.{snapshot}` CLONE `{dataset_ref}.{table}` "
            "OPTIONS(expiration_timestamp=TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 30 DAY))"
        )
        client.query(sql).result()
        created.append(snapshot)
    return created


def apply_repair(client, dataset_ref: str, spec: ReportSpec, validation: RepairValidation, load_source: str) -> None:
    work_table = f"_repair_{spec.report_type}_{uuid.uuid4().hex[:8]}"
    table_ref = f"{dataset_ref}.{work_table}"
    schema = _schema_for(spec)
    table = bigquery.Table(table_ref, schema=schema)
    table.expires = datetime.now(timezone.utc) + timedelta(hours=1)
    client.create_table(table)
    now = datetime.now(timezone.utc).isoformat()
    rows = [dict(row, load_source=load_source, ingested_at=now) for row in validation.replacement_rows]
    payload = "\n".join(json.dumps(row, default=_normal) for row in rows).encode()
    config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=schema,
    )
    try:
        client.load_table_from_file(io.BytesIO(payload), table_ref, job_config=config).result()
        ledger = validation.ledger
        params = [
            bigquery.ScalarQueryParameter("report_id", "STRING", validation.report_id),
            bigquery.ScalarQueryParameter("job_id", "STRING", validation.job_id),
            bigquery.ScalarQueryParameter("report_type", "STRING", validation.report_type),
            bigquery.ScalarQueryParameter("report_date", "DATE", validation.report_date),
            bigquery.ScalarQueryParameter("report_create_time", "TIMESTAMP", ledger["report_create_time"]),
            bigquery.ScalarQueryParameter("row_count", "INT64", validation.canonical_rows),
            bigquery.ScalarQueryParameter("csv_bytes", "INT64", ledger["csv_bytes"]),
            bigquery.ScalarQueryParameter("content_sha256", "STRING", validation.sha256),
            bigquery.ScalarQueryParameter("gcs_uri", "STRING", validation.gcs_uri),
            bigquery.ScalarQueryParameter("load_source", "STRING", load_source),
        ]
        client.query(
            build_repair_script(dataset_ref, spec, work_table),
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()
    finally:
        client.delete_table(table_ref, not_found_ok=True)


def _parse_targets(values: list[str]) -> list[tuple[ReportSpec, str]]:
    targets = []
    for value in values:
        try:
            report_type, report_date = value.split(":", 1)
            date.fromisoformat(report_date)
            targets.append((SPECS[report_type], report_date))
        except (ValueError, KeyError):
            raise ValueError(f"bad target {value!r}; expected registered_report_type:YYYY-MM-DD") from None
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-dataset")
    parser.add_argument("--snapshot-prefix")
    args = parser.parse_args()

    project = _bootstrap.resolve_project()
    dataset_ref = f"{project}.{args.dataset}"
    targets = _parse_targets(args.target)
    credentials = _bootstrap.google_cloud_credentials()
    bq = bigquery.Client(project=project, credentials=credentials)
    gcs = storage.Client(project=project, credentials=credentials)

    if not args.apply:
        for spec, report_date in targets:
            result = load_validation(bq, gcs, dataset_ref, spec, report_date)
            print(json.dumps(result.evidence(), sort_keys=True))
        print(json.dumps({"dry_run": True, "writes": 0}, sort_keys=True))
        return 0

    if args.confirm_dataset != args.dataset:
        parser.error("--apply requires --confirm-dataset matching --dataset")
    if not args.snapshot_prefix:
        parser.error("--apply requires --snapshot-prefix")

    load_source = f"repair_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    with manual_writer_lease(
        project_id=project,
        dataset=args.dataset,
        domain="reporting-writer",
        entrypoint="repair_reporting_duplicates",
    ):
        validations = [load_validation(bq, gcs, dataset_ref, spec, report_date) for spec, report_date in targets]
        snapshots = create_snapshots(bq, dataset_ref, [spec for spec, _ in targets], args.snapshot_prefix)
        print(json.dumps({"snapshots": snapshots}, sort_keys=True))
        for (spec, _), validation in zip(targets, validations, strict=True):
            apply_repair(bq, dataset_ref, spec, validation, load_source)
            after = load_validation(
                bq,
                gcs,
                dataset_ref,
                spec,
                validation.report_date,
                require_duplicates=False,
            )
            if after.copy_count != 1:
                raise RuntimeError("post-repair partition is not singular")
            print(json.dumps({"repaired": after.evidence()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
