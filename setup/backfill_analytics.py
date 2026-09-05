"""Backfill historical analytics data from the YouTube Analytics API.

Queries the Analytics API for each day in the date range and writes
to daily_video_analytics and daily_traffic_sources in BigQuery.

Usage:
    python3 setup/backfill_analytics.py --start 2025-10-16 --end 2026-02-17
"""

import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery
from googleapiclient.discovery import build

import _bootstrap  # noqa: F401  (adds cloud_function/ to sys.path)

# isort: split   (everything below needs _bootstrap to have run first)
from bigquery_writer import BigQueryWriter
from oauth_credentials import load_oauth_credentials
from retry import with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


PROJECT_ID = _bootstrap.resolve_project()
DATASET_ID = os.environ.get("BQ_DATASET", "youtube_analytics")
# Retry policy is the shared one (429 and the 5xx family). The attempt count is this
# script's own decision: 5, one more than the Cloud Function, because a backfill runs
# hundreds of calls back to back and a lost day here is a manual re-run.
BACKFILL_MAX_RETRIES = 5


def build_analytics_client():
    """Build the YouTube Analytics API client from the shared Secret Manager loader."""
    return build("youtubeAnalytics", "v2", credentials=load_oauth_credentials(PROJECT_ID))


def api_call_with_retry(fn, max_retries: int = BACKFILL_MAX_RETRIES):
    """Execute an API call with exponential backoff (shared policy, backfill attempt count)."""
    return with_retry(fn, max_retries=max_retries)


# The unfiltered "Top videos" report caps at 200 rows and cannot be paged past it:
# startIndex silently returns 0 rows unfiltered, HTTP 400 at maxResults=200, and
# HTTP 500 on the filtered path. Adding filters=video== moves the request onto the
# uncapped "Basic user activity statistics" report. Full citations in
# cloud_function/youtube_analytics_api.py.
#
# Backfill-specific limit worth knowing (channel_reports): the traffic sources report
# "returns an error if the product of # of queried videos X # of days in date range
# exceeds 50,000". This script queries one day at a time, so it is far clear of that,
# but do not widen the date range while filtering many videos.
RESULT_CAP = 200
SHARD_SIZE = 100

METRICS = ("estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
           "subscribersGained,subscribersLost,shares")


def query_videos(analytics, date_str: str, video_ids=None) -> list:
    """One video-dimension call, optionally restricted to a set of video ids."""
    params = dict(ids="channel==MINE", startDate=date_str, endDate=date_str,
                  dimensions="video", metrics=METRICS,
                  sort="-estimatedMinutesWatched", maxResults=RESULT_CAP)
    if video_ids:
        params["filters"] = "video==" + ",".join(video_ids)
    return api_call_with_retry(
        lambda: analytics.reports().query(**params).execute()).get("rows", [])


def fetch_video_analytics(analytics, query_date: date, video_ids=None) -> list[dict[str, Any]]:
    """Fetch per-video analytics for a single day, working around the 200-row cap."""
    date_str = str(query_date)
    try:
        api_rows = query_videos(analytics, date_str)
        if len(api_rows) >= RESULT_CAP and video_ids:
            logger.error(f"  {date_str} hit the {RESULT_CAP}-row cap; re-fetching in shards")
            merged = {}
            for i in range(0, len(video_ids), SHARD_SIZE):
                for row in query_videos(analytics, date_str, video_ids[i:i + SHARD_SIZE]):
                    merged[row[0]] = row
            api_rows = list(merged.values())
            logger.info(f"  sharded fetch recovered {len(api_rows)} rows for {date_str}")
    except Exception as e:
        logger.error(f"  Analytics query failed for {date_str}: {e}")
        return []

    rows = []
    for row in api_rows:
        rows.append({
            "video_id": row[0],
            "estimated_minutes_watched": row[1],
            "average_view_duration_seconds": row[2],
            "average_view_percentage": row[3],
            "subscribers_gained": row[4],
            "subscribers_lost": row[5],
            "shares": row[6],
            "impressions": None,
            "impression_ctr": None,
            "annotation_click_through_rate": None,
            "card_click_rate": None,
        })
    return rows


def fetch_traffic_sources(analytics, video_ids: list[str], query_date: date) -> list[dict[str, Any]]:
    """Fetch traffic source data for all videos for a single day."""
    date_str = str(query_date)
    all_rows = []

    for video_id in video_ids:
        try:
            response = api_call_with_retry(
                lambda vid=video_id: analytics.reports().query(
                    ids="channel==MINE",
                    startDate=date_str,
                    endDate=date_str,
                    dimensions="insightTrafficSourceType",
                    metrics="views,estimatedMinutesWatched",
                    filters=f"video=={vid}",
                ).execute()
            )
            for row in response.get("rows", []):
                all_rows.append({
                    "video_id": video_id,
                    "traffic_source_type": row[0],
                    "views": row[1],
                    "estimated_minutes_watched": row[2],
                })
        except Exception as e:
            logger.warning(f"  Traffic sources failed for {video_id}: {e}")

    return all_rows


def write_rows(writer: BigQueryWriter, table_name: str, rows: list[dict],
               activity_date: date, run_date: date, load_source: str) -> int:
    """Replace one activity day through the shared BigQueryWriter.

    This used to be a hand copy of cloud_function/bigquery_writer._delete_and_insert
    with a docstring begging future authors to keep the two in sync. The two
    load-bearing properties (DELETE keyed on activity_date, and only after rows are in
    hand) now live in exactly one place and are pinned by tests/test_bigquery_writer.py.
    """
    if table_name == "daily_video_analytics":
        return writer.write_daily_video_analytics(rows, run_date, activity_date, load_source)
    if table_name == "daily_traffic_sources":
        return writer.write_daily_traffic_sources(rows, run_date, activity_date, load_source)
    raise ValueError(f"backfill does not write {table_name}")


def main():
    parser = argparse.ArgumentParser(description="Backfill YouTube analytics data")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--load-source", dest="load_source", default=None,
                        help="Provenance tag for the written rows "
                             "(default: backfill_YYYYMMDD of today)")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    total_days = (end_date - start_date).days + 1

    run_date = datetime.now(ZoneInfo(os.environ.get("PIPELINE_TZ", "America/Phoenix"))).date()
    load_source = args.load_source or f"backfill_{run_date:%Y%m%d}"

    logger.info(f"Backfilling {total_days} days: {start_date} to {end_date}")
    logger.info(f"Rows will be tagged load_source={load_source}, snapshot_date={run_date}")

    analytics = build_analytics_client()
    bq_client = bigquery.Client(project=PROJECT_ID)
    writer = BigQueryWriter(project_id=PROJECT_ID, dataset_id=DATASET_ID)

    # Get video IDs from the most recent video_metadata snapshot
    query = f"SELECT DISTINCT video_id FROM `{PROJECT_ID}.{DATASET_ID}.video_metadata` WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `{PROJECT_ID}.{DATASET_ID}.video_metadata`)"
    video_ids = [row.video_id for row in bq_client.query(query).result()]
    logger.info(f"Found {len(video_ids)} videos to backfill")

    total_analytics = 0
    total_traffic = 0

    current_date = start_date
    day_num = 0
    while current_date <= end_date:
        day_num += 1
        logger.info(f"[{day_num}/{total_days}] Processing {current_date}...")

        # Fetch and write analytics
        analytics_rows = fetch_video_analytics(analytics, current_date, video_ids)
        a_count = write_rows(writer, "daily_video_analytics", analytics_rows,
                             current_date, run_date, load_source)
        total_analytics += a_count

        # Fetch and write traffic sources
        traffic_rows = fetch_traffic_sources(analytics, video_ids, current_date)
        t_count = write_rows(writer, "daily_traffic_sources", traffic_rows,
                             current_date, run_date, load_source)
        total_traffic += t_count

        logger.info(f"  → {a_count} analytics rows, {t_count} traffic rows")

        current_date += timedelta(days=1)

        # Small delay to avoid rate limits
        time.sleep(0.5)

    logger.info(f"Backfill complete: {total_analytics} analytics rows, {total_traffic} traffic rows across {total_days} days")


if __name__ == "__main__":
    main()
