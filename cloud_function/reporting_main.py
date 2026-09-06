"""Cloud Function entry point for the YouTube Reporting API ingest.

Deployed as its own function (`youtube-reporting-ingest`, entry point `reporting_main`)
from the same source directory as the daily pipeline, on its own scheduler. Separate
function means an independent failure domain and timeout, and no interleaving with the
daily function's DELETE-then-load on the four original tables.

Log strings the monitoring policies match. If you change them, change
setup/6_setup_monitoring.sh in the same commit:
    Reporting API step complete — reports=N rows=M header_only=H superseded=S failed=F
    Reporting API failed entirely: ...
    Reporting header-only report supersedes populated day     (emitted by reporting_loader)
    Reporting freshness stale — newest loaded report_date is N days old for K report type(s)
    Reporting API skipped — REPORTING_ENABLED=false
"""

from __future__ import annotations

import logging
import os
import uuid

import functions_framework
from google.cloud import bigquery

try:
    import google.cloud.logging

    google.cloud.logging.Client().setup_logging()
except Exception:  # noqa: BLE001 - local runs have no Cloud Logging
    logging.basicConfig(level=logging.INFO)

from oauth_credentials import load_oauth_credentials
from partition_replacer import StagedTransactionalReplacer
from reporting_loader import GcsArchive, IngestLedger, ReportingLoader, freshness_by_type
from youtube_reporting_api import YouTubeReportingClient

PROJECT_ID = os.environ["GCP_PROJECT"]
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
REPORTING_ENABLED = os.environ.get("REPORTING_ENABLED", "false").lower() == "true"
MAX_REPORTS_PER_RUN = int(os.environ.get("MAX_REPORTS_PER_RUN", "30"))
ARCHIVE_BUCKET = os.environ.get("REPORTING_ARCHIVE_BUCKET", f"{PROJECT_ID}-youtube-reporting-raw")
STALE_DAYS = int(os.environ.get("REPORTING_STALE_DAYS", "4"))

logger = logging.getLogger(__name__)

COMPLETE_LOG = "Reporting API step complete"
FAILED_LOG = "Reporting API failed entirely"
STALE_LOG = "Reporting freshness stale"
SKIPPED_LOG = "Reporting API skipped"


def build_loader(load_source: str = "cron") -> tuple[ReportingLoader, IngestLedger]:
    creds = load_oauth_credentials(PROJECT_ID)
    bq = bigquery.Client(project=PROJECT_ID)
    dataset_ref = f"{PROJECT_ID}.{DATASET_ID}"
    archive = None
    if ARCHIVE_BUCKET:
        from google.cloud import storage

        archive = GcsArchive(storage.Client(project=PROJECT_ID).bucket(ARCHIVE_BUCKET))
    ledger = IngestLedger(bq, dataset_ref)
    loader = ReportingLoader(
        YouTubeReportingClient(creds),
        StagedTransactionalReplacer(bq, dataset_ref, CHANNEL_ID),
        ledger,
        dataset_ref,
        archive,
        max_reports_per_run=MAX_REPORTS_PER_RUN,
        load_source=load_source,
    )
    return loader, ledger


def run_reporting(log: logging.LoggerAdapter) -> dict:
    loader, ledger = build_loader()
    summary = loader.run()
    log.info(
        f"{COMPLETE_LOG} — reports={summary.reports_considered} rows={summary.rows} "
        f"loaded={summary.loaded} header_only={summary.header_only} superseded={summary.superseded} "
        f"failed={summary.failed} deferred={summary.deferred} conflicts={summary.header_only_conflict}"
    )
    for err in summary.errors:
        log.warning(f"Reporting load error: {err}")
    ages = freshness_by_type(ledger)
    result = summary.as_dict()
    result["freshness_days_by_type"] = ages
    result["newest_loaded_age_days"] = max(ages.values()) if ages else None
    if not ages:
        log.error(f"{STALE_LOG} — no report type has any loaded report yet (threshold {STALE_DAYS} days)")
    else:
        stale = {t: a for t, a in ages.items() if a > STALE_DAYS}
        if stale:
            worst = max(stale.values())
            log.error(
                f"{STALE_LOG} — newest loaded report_date is {worst} days old for "
                f"{len(stale)} report type(s): {', '.join(sorted(stale))} (threshold {STALE_DAYS})"
            )
    return result


@functions_framework.http
def reporting_main(request) -> tuple[dict, int]:
    run_id = str(uuid.uuid4())[:8]
    log = logging.LoggerAdapter(logger, extra={"run_id": run_id})
    if not REPORTING_ENABLED:
        # WARNING and a monitored string: a function that stays switched off is a failure
        # mode, not a quiet default. Reports expire 60 days after generation.
        log.warning(f"{SKIPPED_LOG} — REPORTING_ENABLED=false; nothing ingested this run")
        return {"skipped": True, "reason": "REPORTING_ENABLED=false", "run_id": run_id}, 200
    try:
        log.info(f"Reporting ingest started — dataset={DATASET_ID}, run_id={run_id}")
        result = run_reporting(log)
        result["run_id"] = run_id
        return result, 200
    except Exception as e:
        log.exception(f"{FAILED_LOG}: {e}")
        return {"error": str(e), "run_id": run_id}, 500
