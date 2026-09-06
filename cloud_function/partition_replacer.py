"""Atomic partition replacement for the Reporting API raw tables.

The existing writer does DELETE then load as two jobs. A crash between them leaves an
emptied partition, and nothing records whether the load happened. For the new tables the
replacement is one BigQuery multi-statement transaction:

    create a work table with a one-hour expiry, load NDJSON into it (explicit schema)
    BEGIN TRANSACTION
      assert: work table has rows
      assert: every row carries the expected report_date, and exactly one date
      assert: every row carries the configured channel_id
      assert: the native grain is unique
      assert: ledger has no `loaded` row for this report_id
      assert: ledger has no `loaded` row for (job_id, report_date) with an equal or newer
              report_create_time   <- newest-wins enforced here, not only in Python
      DELETE the partition; INSERT from the work table with explicit column lists
      ledger: previous loaded / header-only rows for the day -> superseded;
              MERGE this report_id -> loaded
    COMMIT

Every assertion is a RAISE inside the transaction, so a failed check rolls back both the
data and the ledger together. A transaction conflict (two runs on the same table) is
retried after a short backoff, up to three times; BigQuery aborts the loser immediately
while the winner is still in flight, so the retry has to wait for the winner to commit,
after which the assertions see its ledger row and refuse cleanly with already_loaded.

The two "already loaded" refusals are not failures: they mean another run got there
first. They raise AlreadyLoaded so the loader can count them as skipped and leave the
winning run's ledger row alone.

A newer generation that is header-only does NOT block loading an older populated one:
a header-only report never replaces populated data (see reporting_loader), so loading
the populated generation is one of the two legitimate resolutions of that conflict.

DDL is not allowed inside a transaction, which is why the work table is created and
filled beforehand and dropped afterwards. Its name carries a per-call random suffix so
two overlapping runs on the same report never share a work table.
"""

from __future__ import annotations

import io
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from google.cloud import bigquery

from report_specs import LEDGER_TABLE, PROVENANCE_COLUMNS, ReportSpec

logger = logging.getLogger(__name__)

TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = (5, 10, 20)
TRANSIENT_MARKERS = ("concurrent", "transaction is aborted", "abort", "backenderror", "internalerror", "ratelimitexceeded")


class ReplaceRefused(RuntimeError):
    """An in-transaction assertion failed; nothing was written."""


class AlreadyLoaded(ReplaceRefused):
    """This report, or an equal-or-newer generation of its day, is already loaded. Not an error."""


class PartitionReplacer(Protocol):
    def replace_partition(self, spec: ReportSpec, rows: list[dict[str, Any]], provenance: dict[str, Any]) -> int: ...


def _schema_for(spec: ReportSpec) -> list[bigquery.SchemaField]:
    fields = [
        bigquery.SchemaField(c, spec.column_type(c), mode="REQUIRED" if c in ("report_date", "channel_id") else "NULLABLE")
        for c in spec.grain_columns
    ]
    fields += [bigquery.SchemaField(m, spec.column_type(m), mode="NULLABLE") for m in spec.metrics]
    fields += [bigquery.SchemaField(n, t, mode="REQUIRED" if req else "NULLABLE") for n, t, req in PROVENANCE_COLUMNS]
    return fields


def build_replace_script(dataset_ref: str, spec: ReportSpec, work_table: str) -> str:
    """The transaction as a parameterised BigQuery script (parameters bound at run time)."""
    target = f"`{dataset_ref}.{spec.table}`"
    work = f"`{dataset_ref}.{work_table}`"
    ledger = f"`{dataset_ref}.{LEDGER_TABLE}`"
    cols = ", ".join(spec.columns + tuple(n for n, _, _ in PROVENANCE_COLUMNS))
    grain = ", ".join(spec.grain_columns)
    return f"""
BEGIN
  BEGIN TRANSACTION;

  IF (SELECT COUNT(*) FROM {work}) = 0 THEN
    RAISE USING MESSAGE = 'refused: work table has zero rows';
  END IF;
  IF (SELECT COUNT(DISTINCT report_date) FROM {work}) != 1
     OR (SELECT ANY_VALUE(report_date) FROM {work}) != @report_date THEN
    RAISE USING MESSAGE = 'refused: work table rows are not all for the expected report_date';
  END IF;
  IF (SELECT COUNTIF(channel_id != @channel_id) FROM {work}) > 0 THEN
    RAISE USING MESSAGE = 'refused: rows for a channel other than the configured one';
  END IF;
  IF (SELECT COUNT(*) FROM (SELECT 1 FROM {work} GROUP BY {grain} HAVING COUNT(*) > 1)) > 0 THEN
    RAISE USING MESSAGE = 'refused: native grain is not unique in the work table';
  END IF;
  IF EXISTS (SELECT 1 FROM {ledger} WHERE report_id = @report_id AND status = 'loaded') THEN
    RAISE USING MESSAGE = 'already_loaded: this report_id is already loaded';
  END IF;
  IF EXISTS (SELECT 1 FROM {ledger}
             WHERE job_id = @job_id AND report_date = @report_date AND status = 'loaded'
               AND report_create_time >= @report_create_time) THEN
    RAISE USING MESSAGE = 'already_loaded: an equal or newer generation of this day is already loaded';
  END IF;

  DELETE FROM {target} WHERE report_date = @report_date;
  INSERT INTO {target} ({cols}) SELECT {cols} FROM {work};

  UPDATE {ledger} SET status = 'superseded', error = NULL
  WHERE job_id = @job_id AND report_date = @report_date
    AND status IN ('loaded', 'header_only', 'header_only_conflict') AND report_id != @report_id;

  MERGE {ledger} L
  USING (SELECT @report_id AS report_id) S ON L.report_id = S.report_id
  WHEN MATCHED THEN UPDATE SET
    status = 'loaded', row_count = @row_count, csv_bytes = @csv_bytes, content_sha256 = @content_sha256,
    gcs_uri = @gcs_uri, load_source = @load_source, ingested_at = CURRENT_TIMESTAMP(), error = NULL
  WHEN NOT MATCHED THEN INSERT
    (report_id, job_id, report_type, report_date, report_create_time, status, row_count, csv_bytes,
     content_sha256, gcs_uri, load_source, ingested_at, error)
  VALUES (@report_id, @job_id, @report_type, @report_date, @report_create_time, 'loaded', @row_count,
          @csv_bytes, @content_sha256, @gcs_uri, @load_source, CURRENT_TIMESTAMP(), NULL);

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;
"""


def classify_error(message: str) -> str:
    """'already_loaded' | 'refused' | 'transient' | 'other' from a BigQuery error message."""
    low = message.lower()
    if "already_loaded:" in low:
        return "already_loaded"
    if "refused:" in low:
        return "refused"
    if any(m in low for m in TRANSIENT_MARKERS):
        return "transient"
    return "other"


class StagedTransactionalReplacer:
    """Load to a work table, then replace the partition and update the ledger in one transaction."""

    def __init__(self, client: bigquery.Client, dataset_ref: str, channel_id: str) -> None:
        self.client = client
        self.dataset_ref = dataset_ref
        self.channel_id = channel_id

    def _work_table_name(self, spec: ReportSpec, report_id: str) -> str:
        safe = "".join(ch for ch in report_id if ch.isalnum())[:12]
        return f"_load_{spec.report_type}_{safe}_{uuid.uuid4().hex[:8]}"

    def _stage(self, spec: ReportSpec, work_table: str, rows: list[dict[str, Any]]) -> None:
        table_ref = f"{self.dataset_ref}.{work_table}"
        # Expiry is set at creation, so a crash between create and load cannot leave an
        # unexpiring table behind.
        table = bigquery.Table(table_ref, schema=_schema_for(spec))
        table.expires = datetime.now(timezone.utc) + timedelta(hours=1)
        self.client.create_table(table)
        payload = "\n".join(json.dumps(r) for r in rows).encode()
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=_schema_for(spec),
        )
        self.client.load_table_from_file(io.BytesIO(payload), table_ref, job_config=job_config).result()

    def _drop(self, work_table: str) -> None:
        try:
            self.client.delete_table(f"{self.dataset_ref}.{work_table}", not_found_ok=True)
        except Exception as e:  # noqa: BLE001 - the table expires in an hour anyway
            logger.warning(f"could not drop work table {work_table}: {e}")

    def replace_partition(self, spec: ReportSpec, rows: list[dict[str, Any]], provenance: dict[str, Any]) -> int:
        if not rows:
            raise ReplaceRefused("refused in Python: zero rows; header-only reports never replace a partition")
        report_date = rows[0]["report_date"]
        now = datetime.now(timezone.utc).isoformat()
        staged = [
            dict(r, report_id=provenance["report_id"], report_create_time=provenance["report_create_time"],
                 job_id=provenance["job_id"], load_source=provenance["load_source"], ingested_at=now)
            for r in rows
        ]

        work_table = self._work_table_name(spec, provenance["report_id"])
        try:
            self._stage(spec, work_table, staged)
            script = build_replace_script(self.dataset_ref, spec, work_table)
            params = [
                bigquery.ScalarQueryParameter("report_date", "DATE", report_date),
                bigquery.ScalarQueryParameter("channel_id", "STRING", self.channel_id),
                bigquery.ScalarQueryParameter("report_id", "STRING", provenance["report_id"]),
                bigquery.ScalarQueryParameter("job_id", "STRING", provenance["job_id"]),
                bigquery.ScalarQueryParameter("report_type", "STRING", spec.report_type),
                bigquery.ScalarQueryParameter("report_create_time", "TIMESTAMP", provenance["report_create_time"]),
                bigquery.ScalarQueryParameter("row_count", "INT64", len(rows)),
                bigquery.ScalarQueryParameter("csv_bytes", "INT64", provenance.get("csv_bytes")),
                bigquery.ScalarQueryParameter("content_sha256", "STRING", provenance.get("content_sha256")),
                bigquery.ScalarQueryParameter("gcs_uri", "STRING", provenance.get("gcs_uri")),
                bigquery.ScalarQueryParameter("load_source", "STRING", provenance["load_source"]),
            ]
            self._run_with_retries(script, params)
        finally:
            self._drop(work_table)
        logger.info(f"Replaced {spec.table} partition {report_date} with {len(rows)} rows (report {provenance['report_id']})")
        return len(rows)

    def _run_with_retries(self, script: str, params: list[Any]) -> None:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                self.client.query(script, job_config=job_config).result()
                return
            except Exception as e:  # noqa: BLE001 - classified below
                msg = str(e)
                kind = classify_error(msg)
                if kind == "already_loaded":
                    raise AlreadyLoaded(msg) from e
                if kind == "refused":
                    raise ReplaceRefused(msg) from e
                if kind == "transient" and attempt < TRANSIENT_RETRIES:
                    wait = TRANSIENT_BACKOFF_SECONDS[attempt]
                    logger.warning(f"transient BigQuery error, retrying in {wait}s (attempt {attempt + 1}/{TRANSIENT_RETRIES}): {msg[:200]}")
                    time.sleep(wait)
                    continue
                raise
