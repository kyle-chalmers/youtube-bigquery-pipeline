"""YouTube BigQuery Pipeline — Cloud Function Entry Point.

Orchestrates daily snapshot of YouTube analytics data into BigQuery.
Triggered by Cloud Scheduler via HTTP.
"""

import logging
import os
import uuid
from datetime import date, timedelta

import functions_framework

from bigquery_writer import BigQueryWriter
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
PROJECT_ID = os.environ.get("GCP_PROJECT", "primeval-node-478707-e9")
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCkRi29nXFxNBuPhjseoB6AQ")
UPLOADS_PLAYLIST_ID = os.environ.get("UPLOADS_PLAYLIST_ID", "UUkRi29nXFxNBuPhjseoB6AQ")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
ANALYTICS_LOOKBACK_DAYS = int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "3"))

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

        snapshot_date = date.today()
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
        log.exception(f"Pipeline failed — {e}")
        return {"error": str(e)}, 500


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

    try:
        analytics_date = snapshot_date - timedelta(days=ANALYTICS_LOOKBACK_DAYS)
        analytics_count, traffic_count, analytics_errors = _run_analytics(
            video_ids, analytics_date, snapshot_date, bq_writer
        )
        log.info(f"Wrote daily_video_analytics — {analytics_count} rows")
        log.info(f"Wrote daily_traffic_sources — {traffic_count} rows")
        if analytics_errors:
            log.warning(
                f"Analytics API had {len(analytics_errors)} partial errors"
            )
    except ImportError:
        log.info("Analytics API module not available — skipping")
    except Exception as e:
        log.warning(f"Analytics API failed entirely: {e}")
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
) -> tuple[int, int, list[str]]:
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
    analytics_count = bq_writer.write_daily_video_analytics(video_analytics, snapshot_date)

    # Fetch traffic sources
    traffic_data, traffic_errors = analytics_api.get_traffic_sources(
        video_ids, analytics_date
    )
    traffic_count = bq_writer.write_daily_traffic_sources(traffic_data, snapshot_date)

    all_errors = analytics_errors + traffic_errors
    return analytics_count, traffic_count, all_errors
