"""YouTube BigQuery Pipeline — Cloud Function Entry Point.

Orchestrates daily snapshot of YouTube analytics data into BigQuery.
Triggered by Cloud Scheduler via HTTP.
"""

import logging
import os
from datetime import date, timedelta

import functions_framework

from bigquery_writer import BigQueryWriter
from youtube_data_api import YouTubeDataAPI

# ─── Configuration ───────────────────────────────────────────────
PROJECT_ID = os.environ.get("GCP_PROJECT", "primeval-node-478707-e9")
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCkRi29nXFxNBuPhjseoB6AQ")
UPLOADS_PLAYLIST_ID = os.environ.get("UPLOADS_PLAYLIST_ID", "UUkRi29nXFxNBuPhjseoB6AQ")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
ANALYTICS_LOOKBACK_DAYS = int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "3"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@functions_framework.http
def main(request) -> tuple[dict, int]:
    """HTTP Cloud Function entry point.

    Returns:
        Tuple of (response_dict, status_code).
    """
    try:
        if not YOUTUBE_API_KEY:
            return {"error": "YOUTUBE_API_KEY not set"}, 500

        snapshot_date = date.today()
        logger.info(f"Starting pipeline run for snapshot_date={snapshot_date}")

        result = run_pipeline(snapshot_date)
        logger.info(f"Pipeline complete: {result}")
        return result, 200

    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")
        return {"error": str(e)}, 500


def run_pipeline(snapshot_date: date) -> dict:
    """Execute the full pipeline for a given snapshot date.

    Args:
        snapshot_date: The date to use as the partition key in BigQuery.

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
    logger.info(f"Found {len(video_ids)} videos")

    # Step 2: Fetch video details (metadata + public stats)
    video_details = data_api.get_video_details(video_ids)
    logger.info(f"Fetched details for {len(video_details)} videos")

    # Step 3: Write to BigQuery — Data API tables
    metadata_count = bq_writer.write_video_metadata(video_details, snapshot_date)
    stats_count = bq_writer.write_daily_video_stats(video_details, snapshot_date)

    # Step 4: Analytics API (requires OAuth2 — will be added in Phase 4)
    analytics_count = 0
    traffic_count = 0
    analytics_errors: list[str] = []

    try:
        analytics_date = snapshot_date - timedelta(days=ANALYTICS_LOOKBACK_DAYS)
        analytics_count, traffic_count, analytics_errors = _run_analytics(
            video_ids, analytics_date, snapshot_date, bq_writer
        )
    except ImportError:
        logger.info("Analytics API module not available — skipping analytics collection")
    except Exception as e:
        logger.warning(f"Analytics API failed entirely: {e}")
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
