"""Cloud Function entry point for the trailing-30-day Analytics refresh.

Deployed as its own function (`youtube-analytics-refresh`, entry point `refresh_main`)
from the same source directory as the daily pipeline and the Reporting ingest, on its own
weekly scheduler — mirrors reporting_main.py's reasoning: an independent failure domain
and timeout, and no interleaving with the daily function's own writes (see the soft-guard
invariant documented at the top of analytics_refresh.py — read that before touching either
this function's schedule or main.py's gap-repair schedule).

Log strings the monitoring policy matches. If you change them, change
setup/6_setup_monitoring.sh in the same commit:
    Analytics refresh complete — days=N written=W skipped_empty=E skipped_error=X skipped_low_count=L
    Analytics refresh failed entirely: ...
    refresh_incomplete — ...   (emitted per-day by analytics_refresh._refresh_one_table)
"""

from __future__ import annotations

import logging
import os
import traceback
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import functions_framework
from google.cloud import bigquery

try:
    import google.cloud.logging

    google.cloud.logging.Client().setup_logging()
except Exception:  # noqa: BLE001 - local runs have no Cloud Logging
    logging.basicConfig(level=logging.INFO)

from analytics_refresh import DEFAULT_MIN_ROW_RATIO, INCOMPLETE_LOG, refresh_trailing_days
from bigquery_writer import BigQueryWriter
from log_safety import redact
from youtube_analytics_api import YouTubeAnalyticsAPI

PROJECT_ID = os.environ["GCP_PROJECT"]
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
# Cloud Run is UTC; see main.py's PIPELINE_TZ comment for why this matters for date math.
PIPELINE_TZ = ZoneInfo(os.environ.get("PIPELINE_TZ", "America/Phoenix"))
REFRESH_TRAILING_DAYS = int(os.environ.get("REFRESH_TRAILING_DAYS", "30"))
# Reuses the daily function's own env var name rather than inventing a separate
# REFRESH_LOOKBACK_DAYS — deploy this function with the same value the daily function is
# actually deployed with (see setup/12_deploy_refresh_function.sh), not the code default.
ANALYTICS_LOOKBACK_DAYS = int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "5"))
REFRESH_MIN_ROW_RATIO = float(os.environ.get("REFRESH_MIN_ROW_RATIO", str(DEFAULT_MIN_ROW_RATIO)))

logger = logging.getLogger(__name__)

COMPLETE_LOG = "Analytics refresh complete"
FAILED_LOG = "Analytics refresh failed entirely"
REFRESH_INCOMPLETE_LOG = INCOMPLETE_LOG


def _video_ids_from_latest_snapshot(bq_client: bigquery.Client) -> list[str]:
    """Video universe from the latest video_metadata snapshot.

    Spends zero Data API quota — matters given the 2026-08-14 quota-exhaustion incident.
    Same query as setup/backfill_analytics.py. A video no longer in the latest snapshot
    (fully removed from the channel) silently drops out of refresh coverage; this matches
    that script's existing behavior rather than introducing a new inconsistency.
    """
    query = (
        f"SELECT DISTINCT video_id FROM `{PROJECT_ID}.{DATASET_ID}.video_metadata` "
        f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM "
        f"`{PROJECT_ID}.{DATASET_ID}.video_metadata`)"
    )
    return [row.video_id for row in bq_client.query(query).result()]


def run_refresh(run_date, refresh_run_id: str, log: logging.LoggerAdapter) -> dict:
    bq_client = bigquery.Client(project=PROJECT_ID)
    video_ids = _video_ids_from_latest_snapshot(bq_client)
    log.info(f"Refreshing {len(video_ids)} videos from the latest video_metadata snapshot")

    analytics_api = YouTubeAnalyticsAPI(project_id=PROJECT_ID)
    bq_writer = BigQueryWriter(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    outcomes = refresh_trailing_days(
        video_ids=video_ids,
        analytics_api=analytics_api,
        bq_writer=bq_writer,
        run_date=run_date,
        refresh_run_id=refresh_run_id,
        lookback_days=ANALYTICS_LOOKBACK_DAYS,
        trailing_days=REFRESH_TRAILING_DAYS,
        min_row_ratio=REFRESH_MIN_ROW_RATIO,
        log=log,
    )

    counts = {"written": 0, "skipped_empty": 0, "skipped_error": 0, "skipped_low_count": 0}
    for per_table in outcomes.values():
        for outcome in per_table.values():
            counts[outcome] = counts.get(outcome, 0) + 1

    log.info(
        f"{COMPLETE_LOG} — days={len(outcomes)} written={counts['written']} "
        f"skipped_empty={counts['skipped_empty']} skipped_error={counts['skipped_error']} "
        f"skipped_low_count={counts['skipped_low_count']}"
    )
    return {"outcomes": outcomes, "counts": counts}


@functions_framework.http
def refresh_main(request) -> tuple[dict, int]:
    run_id = str(uuid.uuid4())[:8]
    log = logging.LoggerAdapter(logger, extra={"run_id": run_id})
    try:
        run_date = datetime.now(PIPELINE_TZ).date()
        log.info(f"Analytics refresh started — run_date={run_date}, run_id={run_id}")
        result = run_refresh(run_date, run_id, log)
        result["run_id"] = run_id
        return result, 200
    except Exception as e:
        # Not log.exception: the traceback and message can carry request URLs; redact both.
        log.error(f"{FAILED_LOG}: {redact(str(e))}\n{redact(traceback.format_exc())}")
        return {"error": redact(str(e)), "run_id": run_id}, 500
