#!/usr/bin/env python3
"""Load Reporting API reports into BigQuery by hand: catch-up, replay, or a deliberate override.

Runs the same ReportingLoader the Cloud Function runs, against whatever dataset you name,
so a first catch-up of the 60-day backlog or a replay after a schema change is the same
code path as the scheduled job. Reports YouTube has already expired are replayed from the
GCS archive, which needs no YouTube credentials at all.

    python3 setup/backfill_reporting.py --dataset youtube_analytics_staging --dry-run
    python3 setup/backfill_reporting.py --dataset youtube_analytics_staging --max 200
    python3 setup/backfill_reporting.py --dataset youtube_analytics_staging --from-gcs --since 2026-08-01
    python3 setup/backfill_reporting.py --dataset youtube_analytics_staging --from-gcs --job <job id>
    python3 setup/backfill_reporting.py --dataset youtube_analytics --allow-empty-replace \\
        --report-type channel_reach_basic_a1 --report-date 2026-08-20 --report-id <header-only report id>

--from-gcs builds the job and report catalogue from the archive objects' metadata instead
of the API, so it works with OAuth broken or a job deleted; the newest-createTime rule and
every in-transaction assertion apply unchanged. --report-id loads exactly one report file,
bypassing newest-generation selection in Python; the SQL guard still refuses an older
generation when a newer one is loaded. --allow-empty-replace is the ONLY way a populated
day is replaced by a header-only report: it requires the header-only report's id, checks
that report is ledgered as header_only_conflict, and runs the delete and the ledger
changes in one transaction, printing the row count it removed.

Rows are tagged load_source=backfill_YYYYMMDD unless --load-source is given. Requires
GCP_PROJECT (or the active gcloud project) and YOUTUBE_CHANNEL_ID; OAuth secrets only when
reading from the API.
"""

import argparse
import gzip
import os
import sys
from datetime import datetime, timezone

from google.cloud import bigquery, storage

import _bootstrap  # noqa: F401  (adds cloud_function/ to sys.path)

# isort: split   (everything below needs _bootstrap to have run first)
from partition_replacer import StagedTransactionalReplacer
from report_specs import LEDGER_TABLE, SPECS
from reporting_loader import GcsArchive, IngestLedger, ReportingLoader
from youtube_reporting_api import ReportRef


class ArchiveClient:
    """Duck-types YouTubeReportingClient over the GCS archive so the loader can replay.

    Jobs and reports come from object metadata (job_id, report_type, start/end/create
    times), so no YouTube credential or live job is needed.
    """

    def __init__(self, bucket, only_type: str | None = None, only_job: str | None = None, since: str | None = None):
        self.bucket = bucket
        self.only_type = only_type
        self.only_job = only_job
        self.since = since
        self._refs: dict[str, list[ReportRef]] = {}
        self._jobs: dict[str, str] = {}
        for blob in bucket.list_blobs():
            m = blob.metadata or {}
            if not {"job_id", "report_id", "report_type", "start_time", "end_time", "create_time"} <= set(m):
                continue
            if only_type and m["report_type"] != only_type:
                continue
            if only_job and m["job_id"] != only_job:
                continue
            ref = ReportRef(
                report_id=m["report_id"], job_id=m["job_id"], report_type=m["report_type"],
                start_time=datetime.fromisoformat(m["start_time"]), end_time=datetime.fromisoformat(m["end_time"]),
                create_time=datetime.fromisoformat(m["create_time"]),
                download_url=f"gs://{bucket.name}/{blob.name}",
            )
            if since and ref.report_date < since:
                continue
            self._refs.setdefault(m["job_id"], []).append(ref)
            self._jobs[m["job_id"]] = m["report_type"]

    def list_jobs(self):
        return [{"id": j, "reportTypeId": t, "name": f"archive:{t}"} for j, t in sorted(self._jobs.items())]

    def list_reports(self, job_id, rtype):
        return sorted(self._refs.get(job_id, []), key=lambda r: (r.start_time, r.create_time))

    def download(self, ref):
        blob = self.bucket.blob(ref.download_url.split(f"gs://{self.bucket.name}/", 1)[1])
        return gzip.decompress(blob.download_as_bytes(raw_download=True))


class OnlyReport:
    """Restrict a client to one report id so a specific generation can be loaded on purpose."""

    def __init__(self, inner, report_id: str):
        self.inner = inner
        self.report_id = report_id

    def list_jobs(self):
        return self.inner.list_jobs()

    def list_reports(self, job_id, rtype):
        return [r for r in self.inner.list_reports(job_id, rtype) if r.report_id == self.report_id]

    def download(self, ref):
        return self.inner.download(ref)


def empty_replace_script(dataset_ref: str, table: str) -> str:
    """One transaction: verify the conflict row, delete the partition, supersede, promote."""
    ledger = f"`{dataset_ref}.{LEDGER_TABLE}`"
    return f"""
BEGIN
  DECLARE removed INT64;
  BEGIN TRANSACTION;
  IF NOT EXISTS (SELECT 1 FROM {ledger} WHERE report_id = @report_id AND report_type = @report_type
                 AND report_date = @report_date AND status = 'header_only_conflict') THEN
    RAISE USING MESSAGE = 'refused: no header_only_conflict ledger row for that report id, type and date';
  END IF;
  SET removed = (SELECT COUNT(*) FROM `{dataset_ref}.{table}` WHERE report_date = @report_date);
  DELETE FROM `{dataset_ref}.{table}` WHERE report_date = @report_date;
  UPDATE {ledger} SET status = 'superseded', error = NULL
  WHERE report_type = @report_type AND report_date = @report_date AND status = 'loaded';
  UPDATE {ledger} SET status = 'header_only', error = NULL, load_source = @load_source, ingested_at = CURRENT_TIMESTAMP()
  WHERE report_id = @report_id;
  COMMIT TRANSACTION;
  SELECT removed AS rows_removed;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True, help="target dataset, e.g. youtube_analytics_staging")
    ap.add_argument("--max", type=int, default=100, help="max reports to load this run")
    ap.add_argument("--load-source", default=None)
    ap.add_argument("--from-gcs", action="store_true", help="replay from the archive instead of the API")
    ap.add_argument("--report-type", default=None, help="restrict to one report type id")
    ap.add_argument("--job", default=None, help="restrict to one job id")
    ap.add_argument("--since", default=None, help="only report days on or after YYYY-MM-DD")
    ap.add_argument("--report-id", default=None,
                    help="load exactly this report file, bypassing newest-generation selection in Python "
                         "(the in-transaction newest-wins check still applies)")
    ap.add_argument("--dry-run", action="store_true", help="list candidates, write nothing")
    ap.add_argument("--allow-empty-replace", action="store_true",
                    help="replace one populated day with an empty partition; needs --report-type, --report-date, --report-id")
    ap.add_argument("--report-date", default=None)
    args = ap.parse_args()

    project = _bootstrap.resolve_project()
    channel_id = os.environ.get("YOUTUBE_CHANNEL_ID") or sys.exit("YOUTUBE_CHANNEL_ID is required")
    dataset_ref = f"{project}.{args.dataset}"
    load_source = args.load_source or f"backfill_{datetime.now(timezone.utc):%Y%m%d}"
    bucket_name = os.environ.get("REPORTING_ARCHIVE_BUCKET", f"{project}-youtube-reporting-raw")

    bq = bigquery.Client(project=project)
    bucket = storage.Client(project=project).bucket(bucket_name)
    ledger = IngestLedger(bq, dataset_ref)

    if args.allow_empty_replace:
        if not (args.report_type and args.report_date and args.report_id):
            sys.exit("--allow-empty-replace needs --report-type, --report-date and --report-id (the header-only report)")
        spec = SPECS[args.report_type]
        n = ledger.partition_row_count(dataset_ref, spec, args.report_date)
        print(f"{spec.table} {args.report_date} currently holds {n} rows.")
        if args.dry_run:
            print("[dry-run] would DELETE them and promote the header-only report, in one transaction")
            return 0
        answer = input(f"Type the date {args.report_date} to confirm deleting {n} rows: ")
        if answer.strip() != args.report_date:
            print("aborted")
            return 1
        cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("report_id", "STRING", args.report_id),
            bigquery.ScalarQueryParameter("report_type", "STRING", args.report_type),
            bigquery.ScalarQueryParameter("report_date", "DATE", args.report_date),
            bigquery.ScalarQueryParameter("load_source", "STRING", f"{load_source}_empty_replace"),
        ])
        result = list(bq.query(empty_replace_script(dataset_ref, spec.table), job_config=cfg).result())
        removed = result[0]["rows_removed"] if result else "?"
        print(f"removed {removed} rows from {spec.table} {args.report_date}; ledger: loaded -> superseded, "
              f"{args.report_id} -> header_only, load_source={load_source}_empty_replace")
        return 0

    if args.from_gcs:
        client = ArchiveClient(bucket, only_type=args.report_type, only_job=args.job, since=args.since)
    else:
        from oauth_credentials import load_oauth_credentials
        from youtube_reporting_api import YouTubeReportingClient

        live = YouTubeReportingClient(load_oauth_credentials(project))

        class Filtered:
            def list_jobs(self):
                return [j for j in live.list_jobs()
                        if (not args.report_type or j["reportTypeId"] == args.report_type)
                        and (not args.job or j["id"] == args.job)]

            def list_reports(self, job_id, rtype):
                return [r for r in live.list_reports(job_id, rtype) if not args.since or r.report_date >= args.since]

            def download(self, ref):
                return live.download(ref)

        client = Filtered()
    if args.report_id:
        client = OnlyReport(client, args.report_id)

    specs = {args.report_type: SPECS[args.report_type]} if args.report_type else None
    if args.dry_run:
        class DryReplacer:
            def replace_partition(self, spec, rows, provenance):
                print(f"[dry-run] would replace {spec.table} {rows[0]['report_date']} with {len(rows)} rows ({provenance['report_id']})")
                return len(rows)

        class DryLedger(IngestLedger):
            def mark(self, ref, status, **kw):
                print(f"[dry-run] ledger {ref.report_type} {ref.report_date} {ref.report_id} -> {status}")

        loader = ReportingLoader(client, DryReplacer(), DryLedger(bq, dataset_ref), dataset_ref, None,
                                 max_reports_per_run=args.max, load_source=load_source, specs=specs)
    else:
        loader = ReportingLoader(client, StagedTransactionalReplacer(bq, dataset_ref, channel_id), ledger,
                                 dataset_ref, GcsArchive(bucket), max_reports_per_run=args.max,
                                 load_source=load_source, specs=specs)
    summary = loader.run()
    print(summary.as_dict())
    return 1 if (summary.failed or summary.header_only_conflict) else 0


if __name__ == "__main__":
    sys.exit(main())
