"""Decide which Reporting API reports to load, and load them.

Each run lists every retained report for every job (bounded by YouTube's retention, a few
dozen to a hundred per job) and diffs against the ledger. There is no watermark, so a
report that failed last time is simply a candidate again, and nothing can be stranded
behind a later success. Never-attempted reports go first; retries of failed ones are
capped per run so one persistently failing report type cannot starve the other eighteen.

Per report day only the newest createTime is loaded; older generations are ledgered as
`skipped_older` without being downloaded (a failed older generation is re-ledgered too,
so it does not stay `failed` forever). A day already loaded from an equal-or-newer
generation is skipped. Every downloaded body is written to the GCS archive before it is
parsed, so the archive is continuous. Header-only reports are ledgered and never delete;
a header-only report that would supersede a populated day, or that arrives while an
older populated generation of the same day was never loaded, is ledgered as a conflict
and logged at WARNING for the alert to catch.

Every per-report failure, whatever raised it, is ledgered as `failed` and the run
continues; only listing failures abort a run.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery

from partition_replacer import AlreadyLoaded, PartitionReplacer, ReplaceRefused
from report_specs import LEDGER_TABLE, SPECS, ReportSpec
from reporting_parser import parse_report
from youtube_reporting_api import ReportRef, YouTubeReportingClient, newest_per_day

logger = logging.getLogger(__name__)

HEADER_ONLY_CONFLICT_LOG = "Reporting header-only report supersedes populated day"
TERMINAL_OK = ("loaded", "header_only", "header_only_conflict")


@dataclass
class RunSummary:
    reports_considered: int = 0
    loaded: int = 0
    rows: int = 0
    header_only: int = 0
    header_only_conflict: int = 0
    superseded: int = 0
    skipped_older: int = 0
    skipped_current: int = 0
    failed: int = 0
    deferred: int = 0
    retried_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def csv_data_rows(body: bytes) -> int:
    """Data rows in a CSV body, header excluded, blank lines ignored."""
    return max(len([ln for ln in body.splitlines() if ln.strip()]) - 1, 0)


class IngestLedger:
    """Reads the ledger and applies non-transactional status transitions via MERGE.

    A `loaded` row is never overwritten here; only the transaction (which knows it has
    replaced the partition) may move a loaded row, and only to `superseded`.
    """

    def __init__(self, client: bigquery.Client, dataset_ref: str) -> None:
        self.client = client
        self.table = f"`{dataset_ref}.{LEDGER_TABLE}`"

    def load_state(self) -> dict[str, dict[str, Any]]:
        """{report_id: row} for every ledger row."""
        query = f"SELECT report_id, job_id, report_type, report_date, report_create_time, status FROM {self.table}"
        return {r["report_id"]: dict(r) for r in self.client.query(query).result()}

    def loaded_generation(self, state: dict[str, dict[str, Any]], job_id: str, report_date: str) -> datetime | None:
        """create_time of the currently loaded report for (job, day), if any."""
        best = None
        for row in state.values():
            if row["job_id"] == job_id and str(row["report_date"]) == report_date and row["status"] == "loaded":
                ct = row["report_create_time"]
                if best is None or ct > best:
                    best = ct
        return best

    def mark(self, ref: ReportRef, status: str, *, row_count: int | None = None, csv_bytes: int | None = None,
             sha: str | None = None, gcs_uri: str | None = None, load_source: str | None = None,
             error: str | None = None) -> None:
        query = f"""
        MERGE {self.table} L USING (SELECT @report_id AS report_id) S ON L.report_id = S.report_id
        WHEN MATCHED AND L.status != 'loaded' THEN UPDATE SET status = @status, row_count = @row_count,
             csv_bytes = @csv_bytes, content_sha256 = @sha, gcs_uri = @gcs_uri, load_source = @load_source,
             ingested_at = CURRENT_TIMESTAMP(), error = @error
        WHEN NOT MATCHED THEN INSERT (report_id, job_id, report_type, report_date, report_create_time, status,
             row_count, csv_bytes, content_sha256, gcs_uri, load_source, ingested_at, error)
        VALUES (@report_id, @job_id, @report_type, @report_date, @report_create_time, @status, @row_count,
             @csv_bytes, @sha, @gcs_uri, @load_source, CURRENT_TIMESTAMP(), @error)
        """
        params = [
            bigquery.ScalarQueryParameter("report_id", "STRING", ref.report_id),
            bigquery.ScalarQueryParameter("job_id", "STRING", ref.job_id),
            bigquery.ScalarQueryParameter("report_type", "STRING", ref.report_type),
            bigquery.ScalarQueryParameter("report_date", "DATE", ref.report_date),
            bigquery.ScalarQueryParameter("report_create_time", "TIMESTAMP", ref.create_time.isoformat()),
            bigquery.ScalarQueryParameter("status", "STRING", status),
            bigquery.ScalarQueryParameter("row_count", "INT64", row_count),
            bigquery.ScalarQueryParameter("csv_bytes", "INT64", csv_bytes),
            bigquery.ScalarQueryParameter("sha", "STRING", sha),
            bigquery.ScalarQueryParameter("gcs_uri", "STRING", gcs_uri),
            bigquery.ScalarQueryParameter("load_source", "STRING", load_source),
            bigquery.ScalarQueryParameter("error", "STRING", (error or "")[:1000] or None),
        ]
        self.client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

    def partition_row_count(self, dataset_ref: str, spec: ReportSpec, report_date: str) -> int:
        query = f"SELECT COUNT(*) AS n FROM `{dataset_ref}.{spec.table}` WHERE report_date = @d"
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", report_date)])
        return next(iter(self.client.query(query, job_config=cfg).result()))["n"]

    def newest_loaded_by_type(self) -> dict[str, str]:
        """{report_type: newest report_date with status loaded or header_only} as ISO strings."""
        query = (f"SELECT report_type, CAST(MAX(report_date) AS STRING) AS d FROM {self.table} "
                 f"WHERE status IN ('loaded', 'header_only') GROUP BY 1")
        return {r["report_type"]: r["d"] for r in self.client.query(query).result()}


class GcsArchive:
    """Create-if-absent archive of raw CSV bodies, same layout as setup/archive_reporting_raw.py."""

    def __init__(self, bucket: Any) -> None:
        self.bucket = bucket

    @staticmethod
    def object_name(ref: ReportRef) -> str:
        return f"{ref.report_type}/{ref.report_date}/{ref.report_id}.csv.gz"

    def store(self, ref: ReportRef, csv_bytes: bytes, sha: str, data_rows: int) -> str:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(self.object_name(ref))
        blob.metadata = {
            "job_id": ref.job_id, "report_id": ref.report_id, "report_type": ref.report_type,
            "report_date": ref.report_date, "start_time": ref.start_time.isoformat(),
            "end_time": ref.end_time.isoformat(), "create_time": ref.create_time.isoformat(),
            "csv_sha256": sha, "csv_bytes": str(len(csv_bytes)), "data_rows": str(data_rows),
        }
        blob.content_encoding = "gzip"
        try:
            blob.upload_from_string(gzip.compress(csv_bytes, mtime=0), content_type="text/csv", if_generation_match=0)
        except PreconditionFailed:
            # Already archived by an earlier run. Same report_id must mean same bytes; prove it.
            blob.reload()
            stored = (blob.metadata or {}).get("csv_sha256")
            if stored != sha:
                raise RuntimeError(
                    f"archive object {self.object_name(ref)} holds sha256 {stored}, download is {sha}"
                ) from None
        return f"gs://{self.bucket.name}/{self.object_name(ref)}"


class ReportingLoader:
    def __init__(
        self,
        client: YouTubeReportingClient,
        replacer: PartitionReplacer,
        ledger: IngestLedger,
        dataset_ref: str,
        archive: GcsArchive | None,
        *,
        max_reports_per_run: int = 30,
        max_failed_retries_per_run: int = 5,
        load_source: str = "cron",
        specs: dict[str, ReportSpec] | None = None,
    ) -> None:
        self.client = client
        self.replacer = replacer
        self.ledger = ledger
        self.dataset_ref = dataset_ref
        self.archive = archive
        self.max_reports_per_run = max_reports_per_run
        self.max_failed_retries_per_run = max_failed_retries_per_run
        self.load_source = load_source
        self.specs = specs or SPECS
        self._older_siblings: dict[str, list[ReportRef]] = {}

    def run(self) -> RunSummary:
        summary = RunSummary()
        state = self.ledger.load_state()
        fresh: list[ReportRef] = []
        retries: list[ReportRef] = []
        self._older_siblings = {}

        for job in self.client.list_jobs():
            rtype = job["reportTypeId"]
            spec = self.specs.get(rtype)
            if spec is None:
                logger.warning(f"Reporting job {job.get('name')} has unregistered report type {rtype}; skipping")
                continue
            refs = self.client.list_reports(job["id"], rtype)
            summary.reports_considered += len(refs)
            newest = newest_per_day(refs)
            for ref in refs:
                top = newest[ref.report_date]
                if top.report_id != ref.report_id:
                    self._older_siblings.setdefault(top.report_id, []).append(ref)
                    row = state.get(ref.report_id)
                    # Never touch a loaded row here; failed/conflict older generations become skipped_older.
                    if row is None or row["status"] not in ("loaded", "superseded", "skipped_older"):
                        self.ledger.mark(ref, "skipped_older")
                        state[ref.report_id] = {"status": "skipped_older", "job_id": ref.job_id,
                                                "report_date": ref.report_date, "report_create_time": ref.create_time}
                    summary.skipped_older += 1
                    continue
                row = state.get(ref.report_id)
                if row and row["status"] in TERMINAL_OK:
                    summary.skipped_current += 1
                    continue
                loaded_ct = self.ledger.loaded_generation(state, ref.job_id, ref.report_date)
                if loaded_ct is not None and loaded_ct >= ref.create_time:
                    summary.skipped_current += 1
                    continue
                (retries if row and row["status"] == "failed" else fresh).append(ref)

        fresh.sort(key=lambda r: (r.start_time, r.create_time))
        retries.sort(key=lambda r: (r.start_time, r.create_time))
        summary.retried_failed = min(len(retries), self.max_failed_retries_per_run)
        candidates = fresh + retries[: self.max_failed_retries_per_run]
        summary.deferred = max(len(retries) - self.max_failed_retries_per_run, 0)
        if len(candidates) > self.max_reports_per_run:
            summary.deferred += len(candidates) - self.max_reports_per_run
            candidates = candidates[: self.max_reports_per_run]

        for ref in candidates:
            self._ingest_one(ref, state, summary)
        return summary

    def _fail(self, ref: ReportRef, summary: RunSummary, err: str, **kw: Any) -> None:
        summary.failed += 1
        summary.errors.append(f"{ref.report_type} {ref.report_date} {ref.report_id}: {err}")
        self.ledger.mark(ref, "failed", error=err, **kw)
        logger.warning(f"Reporting load failed for {ref.report_type} {ref.report_date}: {err}")

    def _ingest_one(self, ref: ReportRef, state: dict[str, dict[str, Any]], summary: RunSummary) -> None:
        spec = self.specs[ref.report_type]
        try:
            body = self.client.download(ref)
            sha = hashlib.sha256(body).hexdigest()
            gcs_uri = self.archive.store(ref, body, sha, csv_data_rows(body)) if self.archive else None
            rows = parse_report(body, spec)
            if rows and rows[0]["report_date"] != ref.report_date:
                raise ValueError(
                    f"CSV date {rows[0]['report_date']} disagrees with the report's startTime day {ref.report_date}"
                )
        except Exception as e:  # noqa: BLE001 - recorded in the ledger as failed; the run continues
            self._fail(ref, summary, f"{type(e).__name__}: {e}")
            return

        provenance = {
            "report_id": ref.report_id, "job_id": ref.job_id,
            "report_create_time": ref.create_time.isoformat(),
            "load_source": self.load_source, "csv_bytes": len(body), "content_sha256": sha, "gcs_uri": gcs_uri,
        }
        meta = dict(csv_bytes=len(body), sha=sha, gcs_uri=gcs_uri, load_source=self.load_source)

        if not rows:
            conflict = self._header_only_conflict(ref, spec, state)
            if conflict:
                summary.header_only_conflict += 1
                logger.warning(f"{HEADER_ONLY_CONFLICT_LOG}: {spec.table} {ref.report_date}: {conflict}. Partition left untouched.")
                self.ledger.mark(ref, "header_only_conflict", row_count=0, error=conflict, **meta)
            else:
                summary.header_only += 1
                self.ledger.mark(ref, "header_only", row_count=0, **meta)
            return

        had_loaded = self.ledger.loaded_generation(state, ref.job_id, ref.report_date) is not None
        try:
            n = self.replacer.replace_partition(spec, rows, provenance)
        except AlreadyLoaded as e:
            # Another run committed this day first. Its ledger row is right; leave it alone.
            summary.skipped_current += 1
            logger.info(f"Reporting skip for {ref.report_type} {ref.report_date}: {e}")
            return
        except ReplaceRefused as e:
            self._fail(ref, summary, str(e), **meta)
            return
        except Exception as e:  # noqa: BLE001 - a BigQuery error on one report must not abort the run
            self._fail(ref, summary, f"{type(e).__name__}: {e}", **meta)
            return
        summary.loaded += 1
        summary.rows += n
        if had_loaded:
            summary.superseded += 1
        state[ref.report_id] = {"status": "loaded", "job_id": ref.job_id, "report_date": ref.report_date,
                                "report_create_time": ref.create_time}

    def _header_only_conflict(self, ref: ReportRef, spec: ReportSpec, state: dict[str, dict[str, Any]]) -> str | None:
        """Why a header-only report must not be accepted as the day's truth, or None.

        Two cases: the table already holds rows for the day; or an older generation of the
        day was never loaded and turns out to be populated (downloaded to check, since the
        newest-wins rule skipped it). Either way a human decides; see backfill_reporting.py.
        """
        existing = self.ledger.partition_row_count(self.dataset_ref, spec, ref.report_date)
        if existing > 0:
            return f"table holds {existing} rows; report {ref.report_id} is header-only"
        for older in sorted(self._older_siblings.get(ref.report_id, []), key=lambda r: r.create_time, reverse=True):
            row = state.get(older.report_id)
            if row and row["status"] in ("loaded", "superseded", "header_only"):
                continue
            try:
                older_rows = csv_data_rows(self.client.download(older))
            except Exception as e:  # noqa: BLE001 - cannot tell; be conservative
                return f"older generation {older.report_id} could not be inspected ({type(e).__name__})"
            if older_rows > 0:
                return f"older generation {older.report_id} has {older_rows} rows and was never loaded"
        return None


def freshness_by_type(ledger: IngestLedger, today: datetime | None = None) -> dict[str, int]:
    """{report_type: days since its newest loaded or header-only report day}."""
    now = (today or datetime.now(timezone.utc)).date()
    return {t: (now - datetime.fromisoformat(d).date()).days for t, d in ledger.newest_loaded_by_type().items()}


def freshness_days(ledger: IngestLedger, today: datetime | None = None) -> int | None:
    """The worst (largest) age across report types, or None when nothing is loaded."""
    ages = freshness_by_type(ledger, today)
    return max(ages.values()) if ages else None
