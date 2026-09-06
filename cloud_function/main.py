"""YouTube BigQuery Pipeline — Cloud Function Entry Point.

Orchestrates daily snapshot of YouTube analytics data into BigQuery.
Triggered by Cloud Scheduler via HTTP.
"""

import logging
import os
import traceback
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import functions_framework

from bigquery_writer import BigQueryWriter
from log_safety import redact
from youtube_data_api import YouTubeDataAPI

# ─── Structured Logging Setup ────────────────────────────────────
# In Cloud Functions 2nd gen (Cloud Run), google-cloud-logging redirects
# Python's logging module to Cloud Logging as structured JSON.
# Falls back to basic stderr logging for local development.
try:
    import google.cloud.logging

    cloud_logging_client = google.cloud.logging.Client()
    cloud_logging_client.setup_logging()
except ImportError:
    logging.basicConfig(level=logging.INFO)
except Exception:
    logging.basicConfig(level=logging.INFO)

# ─── Configuration ───────────────────────────────────────────────
PROJECT_ID = os.environ["GCP_PROJECT"]  # required — set in Cloud Function env or local .env
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
# Required, with no default. A hardcoded channel default meant anyone who deployed this
# repo without setting the env var silently scraped someone else's channel.
CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
# The uploads playlist is always the channel id with UC -> UU.
UPLOADS_PLAYLIST_ID = os.environ.get(
    "UPLOADS_PLAYLIST_ID", "UU" + CHANNEL_ID[2:] if CHANNEL_ID.startswith("UC") else ""
)
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
# Cloud Run is UTC. The scheduler fires at 23:50 America/Phoenix, which is already
# the next day in UTC, so date.today() stamped every row one day ahead of the local
# day it summarised. Stamp the local day instead.
PIPELINE_TZ = ZoneInfo(os.environ.get("PIPELINE_TZ", "America/Phoenix"))

# Was 3, which is exactly the edge of YouTube Analytics availability: probing on
# 2026-08-29 showed T-0/T-1/T-2 empty and T-3 the first populated day. A single day
# of extra latency therefore returned nothing. 5 buys margin; GAP_LOOKBACK_DAYS
# catches whatever still slips through.
ANALYTICS_LOOKBACK_DAYS = int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "5"))
GAP_LOOKBACK_DAYS = int(os.environ.get("GAP_LOOKBACK_DAYS", "21"))
MAX_GAP_REPAIRS_PER_RUN = int(os.environ.get("MAX_GAP_REPAIRS_PER_RUN", "5"))

logger = logging.getLogger(__name__)


@functions_framework.http
def main(request) -> tuple[dict, int]:
    """HTTP Cloud Function entry point.

    Returns:
        Tuple of (response_dict, status_code).
    """
    run_id = str(uuid.uuid4())[:8]
    log = logging.LoggerAdapter(logger, extra={"run_id": run_id})

    try:
        if not YOUTUBE_API_KEY:
            log.error("YOUTUBE_API_KEY not set")
            return {"error": "YOUTUBE_API_KEY not set"}, 500

        snapshot_date = datetime.now(PIPELINE_TZ).date()
        log.info(f"Pipeline started — snapshot_date={snapshot_date}, run_id={run_id}")

        result = run_pipeline(snapshot_date, log)
        log.info(
            f"Pipeline complete — videos={result['videos_processed']}, "
            f"shorts={result['shorts']}, full_length={result['full_length']}, "
            f"rows={{metadata={result['rows_inserted']['video_metadata']}, "
            f"stats={result['rows_inserted']['daily_video_stats']}, "
            f"analytics={result['rows_inserted']['daily_video_analytics']}, "
            f"traffic={result['rows_inserted']['daily_traffic_sources']}}}, "
            f"analytics_errors={len(result['analytics_errors'])}"
        )
        return result, 200

    except Exception as e:
        # Not log.exception: the traceback and the message carry the request URL, which
        # for the Data API includes the API key. Both are redacted before logging.
        log.error(f"Pipeline failed — {redact(str(e))}\n{redact(traceback.format_exc())}")
        return {"error": redact(str(e))}, 500


def run_pipeline(snapshot_date: date, log: logging.LoggerAdapter) -> dict:
    """Execute the full pipeline for a given snapshot date.

    Args:
        snapshot_date: The date to use as the partition key in BigQuery.
        log: LoggerAdapter with run_id for correlated logging.

    Returns:
        Summary dict with counts and any errors.
    """
    # Initialize clients
    data_api = YouTubeDataAPI(
        api_key=YOUTUBE_API_KEY,
        uploads_playlist_id=UPLOADS_PLAYLIST_ID,
    )
    bq_writer = BigQueryWriter(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    # Step 1: Fetch all video IDs
    video_ids = data_api.get_all_video_ids()
    log.info(f"Fetched {len(video_ids)} video IDs from uploads playlist")

    # Step 2: Fetch video details (metadata + public stats)
    video_details = data_api.get_video_details(video_ids)
    log.info(f"Fetched details for {len(video_details)} videos")

    # Step 3: Write to BigQuery — Data API tables
    metadata_count = bq_writer.write_video_metadata(video_details, snapshot_date)
    log.info(f"Wrote video_metadata — {metadata_count} rows")
    stats_count = bq_writer.write_daily_video_stats(video_details, snapshot_date)
    log.info(f"Wrote daily_video_stats — {stats_count} rows")

    # Step 4: Analytics API (requires OAuth2)
    analytics_count = 0
    traffic_count = 0
    analytics_errors: list[str] = []

    # Analytics path runs under a separate try/except so an OAuth/auth failure
    # does not kill the Data API snapshot. Trade-off: failures here are silent
    # in the HTTP response (200 with empty rows). The Cloud Monitoring alert
    # `youtube-analytics-failure` is what catches this. It watches for two log strings:
    # `Wrote daily_video_analytics — 0 rows` and `Analytics API failed entirely`.
    # If you change those log lines below, update the log-based metric filter
    # in `setup/6_setup_monitoring.sh` to match.
    try:
        analytics_date = snapshot_date - timedelta(days=ANALYTICS_LOOKBACK_DAYS)
        analytics_count, traffic_count, analytics_errors, repaired = _run_analytics(
            video_ids, analytics_date, snapshot_date, bq_writer
        )
        if repaired:
            log.info(f"Repaired {len(repaired)} gap dates: {', '.join(repaired)}")
        log.info(f"Wrote daily_video_analytics — {analytics_count} rows")
        log.info(f"Wrote daily_traffic_sources — {traffic_count} rows")
        if analytics_errors:
            log.warning(
                f"Analytics API had {len(analytics_errors)} partial errors"
            )
    except ImportError:
        log.info("Analytics API module not available — skipping")
    except Exception as e:
        log.warning(f"Analytics API failed entirely: {redact(str(e))}")
        analytics_errors.append(f"Analytics API: {str(e)}")

    # Build summary
    shorts_count = sum(1 for v in video_details if v["video_type"] == "short")
    full_length_count = len(video_details) - shorts_count

    return {
        "snapshot_date": str(snapshot_date),
        "videos_processed": len(video_details),
        "shorts": shorts_count,
        "full_length": full_length_count,
        "rows_inserted": {
            "video_metadata": metadata_count,
            "daily_video_stats": stats_count,
            "daily_video_analytics": analytics_count,
            "daily_traffic_sources": traffic_count,
        },
        "analytics_errors": analytics_errors,
    }


def _run_analytics(
    video_ids: list[str],
    analytics_date: date,
    snapshot_date: date,
    bq_writer: BigQueryWriter,
) -> tuple[int, int, list[str], list[str]]:
    """Run the Analytics API portion of the pipeline.

    Separated to allow graceful failure if OAuth2 is not configured yet.

    Args:
        video_ids: List of video IDs to fetch analytics for.
        analytics_date: The date to query from Analytics API.
        snapshot_date: The BigQuery partition date.
        bq_writer: BigQuery writer instance.

    Returns:
        Tuple of (analytics_rows, traffic_rows, error_messages).
    """
    from youtube_analytics_api import YouTubeAnalyticsAPI

    analytics_api = YouTubeAnalyticsAPI(project_id=PROJECT_ID)

    # Fetch per-video analytics
    video_analytics, analytics_errors = analytics_api.get_video_analytics(
        video_ids, analytics_date
    )
    analytics_count = bq_writer.write_daily_video_analytics(
        video_analytics, snapshot_date, analytics_date
    )

    # Fetch traffic sources
    traffic_data, traffic_errors = analytics_api.get_traffic_sources(
        video_ids, analytics_date
    )
    traffic_count = bq_writer.write_daily_traffic_sources(
        traffic_data, snapshot_date, analytics_date
    )

    repaired = _repair_gaps(analytics_api, bq_writer, video_ids, analytics_date, snapshot_date)

    all_errors = analytics_errors + traffic_errors
    return analytics_count, traffic_count, all_errors, repaired


def _repair_gaps(
    analytics_api: Any,
    bq_writer: BigQueryWriter,
    video_ids: list[str],
    analytics_date: date,
    snapshot_date: date,
) -> list[str]:
    """Re-query activity dates that are still missing from the analytics table.

    Without this, a single empty API response leaves a permanent hole: the run
    writes nothing and no later run ever looks back. Every confirmed hole in this
    warehouse turned out to be recoverable by simply asking again later.

    Returns:
        The activity dates repaired, as ISO strings.
    """
    earliest = analytics_date - timedelta(days=GAP_LOOKBACK_DAYS)
    try:
        missing = bq_writer.find_missing_activity_dates(
            "daily_video_analytics", earliest, analytics_date, MAX_GAP_REPAIRS_PER_RUN
        )
    except Exception as e:
        logger.warning(f"Gap detection failed, skipping repair: {redact(str(e))}")
        return []

    repaired: list[str] = []
    for gap_date in missing:
        rows, _ = analytics_api.get_video_analytics(video_ids, gap_date)
        if not rows:
            logger.info(f"Gap at activity_date={gap_date} still returns no data")
            continue
        bq_writer.write_daily_video_analytics(
            rows, snapshot_date, gap_date, load_source="gap_repair"
        )
        logger.info(f"Repaired gap at activity_date={gap_date} with {len(rows)} rows")
        repaired.append(str(gap_date))
    return repaired
