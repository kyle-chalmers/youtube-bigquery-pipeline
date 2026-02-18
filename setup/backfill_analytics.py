"""Backfill historical analytics data from the YouTube Analytics API.

Queries the Analytics API for each day in the date range and writes
to daily_video_analytics and daily_traffic_sources in BigQuery.

Usage:
    python3 setup/backfill_analytics.py --start 2025-10-16 --end 2026-02-17
"""

import argparse
import io
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from google.cloud import bigquery, secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ID = "primeval-node-478707-e9"
DATASET_ID = "youtube_analytics"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_secret(secret_id: str) -> str:
    """Read a secret from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def build_analytics_client():
    """Build YouTube Analytics API client using OAuth credentials from Secret Manager."""
    credentials = Credentials(
        token=None,
        refresh_token=get_secret("youtube-oauth-refresh-token"),
        client_id=get_secret("youtube-oauth-client-id"),
        client_secret=get_secret("youtube-oauth-client-secret"),
        token_uri=TOKEN_URI,
    )
    return build("youtubeAnalytics", "v2", credentials=credentials)


def api_call_with_retry(fn, max_retries: int = 5):
    """Execute API call with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 503) and attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(f"Rate limited ({e.resp.status}), retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def fetch_video_analytics(analytics, query_date: date) -> list[dict[str, Any]]:
    """Fetch per-video analytics for a single day."""
    date_str = str(query_date)
    try:
        response = api_call_with_retry(
            lambda: analytics.reports().query(
                ids="channel==MINE",
                startDate=date_str,
                endDate=date_str,
                dimensions="video",
                metrics="estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,shares",
                sort="-estimatedMinutesWatched",
                maxResults=200,
            ).execute()
        )
    except Exception as e:
        logger.error(f"  Analytics query failed for {date_str}: {e}")
        return []

    rows = []
    for row in response.get("rows", []):
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


def write_to_bigquery(bq_client: bigquery.Client, table_name: str, rows: list[dict], snapshot_date: date) -> int:
    """Delete existing rows for the date, then batch insert new ones."""
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"

    # Delete existing
    delete_query = f"DELETE FROM `{table_ref}` WHERE snapshot_date = @snapshot_date"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("snapshot_date", "DATE", str(snapshot_date))]
    )
    bq_client.query(delete_query, job_config=job_config).result()

    if not rows:
        return 0

    for row in rows:
        row["snapshot_date"] = str(snapshot_date)

    json_data = "\n".join(json.dumps(r) for r in rows)
    load_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    load_job = bq_client.load_table_from_file(
        io.BytesIO(json_data.encode()), table_ref, job_config=load_config
    )
    load_job.result()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Backfill YouTube analytics data")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    total_days = (end_date - start_date).days + 1

    logger.info(f"Backfilling {total_days} days: {start_date} to {end_date}")

    analytics = build_analytics_client()
    bq_client = bigquery.Client(project=PROJECT_ID)

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
        analytics_rows = fetch_video_analytics(analytics, current_date)
        a_count = write_to_bigquery(bq_client, "daily_video_analytics", analytics_rows, current_date)
        total_analytics += a_count

        # Fetch and write traffic sources
        traffic_rows = fetch_traffic_sources(analytics, video_ids, current_date)
        t_count = write_to_bigquery(bq_client, "daily_traffic_sources", traffic_rows, current_date)
        total_traffic += t_count

        logger.info(f"  → {a_count} analytics rows, {t_count} traffic rows")

        current_date += timedelta(days=1)

        # Small delay to avoid rate limits
        time.sleep(0.5)

    logger.info(f"Backfill complete: {total_analytics} analytics rows, {total_traffic} traffic rows across {total_days} days")


if __name__ == "__main__":
    main()
