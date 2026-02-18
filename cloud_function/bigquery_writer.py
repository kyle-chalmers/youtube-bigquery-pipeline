"""BigQuery writer for YouTube analytics pipeline.

Handles idempotent writes to all 4 tables using DELETE + batch load pattern.
"""

import io
import json
import logging
from datetime import date
from typing import Any

from google.cloud import bigquery

logger = logging.getLogger(__name__)


class BigQueryWriter:
    """Writes YouTube data to BigQuery tables with idempotent upserts."""

    def __init__(self, project_id: str, dataset_id: str) -> None:
        """Initialize BigQuery client.

        Args:
            project_id: GCP project ID.
            dataset_id: BigQuery dataset name.
        """
        self.client = bigquery.Client(project=project_id)
        self.dataset_ref = f"{project_id}.{dataset_id}"

    def write_video_metadata(
        self, videos: list[dict[str, Any]], snapshot_date: date
    ) -> int:
        """Write video metadata rows, replacing existing data for this snapshot_date.

        Args:
            videos: List of video detail dicts from YouTubeDataAPI.
            snapshot_date: The partition date.

        Returns:
            Number of rows written.
        """
        rows = [
            {
                "video_id": v["video_id"],
                "title": v["title"],
                "published_at": v["published_at"],
                "duration_seconds": v["duration_seconds"],
                "duration_formatted": v["duration_formatted"],
                "video_type": v["video_type"],
                "tags": v["tags"],
                "category_id": v["category_id"],
                "thumbnail_url": v["thumbnail_url"],
            }
            for v in videos
        ]
        return self._delete_and_insert("video_metadata", rows, snapshot_date)

    def write_daily_video_stats(
        self, videos: list[dict[str, Any]], snapshot_date: date
    ) -> int:
        """Write daily video stats, replacing existing data for this snapshot_date.

        Args:
            videos: List of video detail dicts from YouTubeDataAPI.
            snapshot_date: The partition date.

        Returns:
            Number of rows written.
        """
        rows = [
            {
                "video_id": v["video_id"],
                "view_count": v["view_count"],
                "like_count": v["like_count"],
                "comment_count": v["comment_count"],
                "favorite_count": v["favorite_count"],
            }
            for v in videos
        ]
        return self._delete_and_insert("daily_video_stats", rows, snapshot_date)

    def write_daily_video_analytics(
        self, analytics: list[dict[str, Any]], snapshot_date: date
    ) -> int:
        """Write daily video analytics from the Analytics API.

        Args:
            analytics: List of analytics dicts per video.
            snapshot_date: The partition date.

        Returns:
            Number of rows written.
        """
        return self._delete_and_insert("daily_video_analytics", analytics, snapshot_date)

    def write_daily_traffic_sources(
        self, traffic: list[dict[str, Any]], snapshot_date: date
    ) -> int:
        """Write daily traffic source data.

        Args:
            traffic: List of traffic source dicts.
            snapshot_date: The partition date.

        Returns:
            Number of rows written.
        """
        return self._delete_and_insert("daily_traffic_sources", traffic, snapshot_date)

    def _delete_and_insert(
        self, table_name: str, rows: list[dict[str, Any]], snapshot_date: date
    ) -> int:
        """Idempotent write: delete existing rows for snapshot_date, then batch insert.

        Uses batch loading (not streaming insert) to avoid eventual consistency
        issues with BigQuery's streaming buffer.

        Args:
            table_name: BigQuery table name (without project/dataset prefix).
            rows: List of row dicts to insert.
            snapshot_date: Date to delete before inserting.

        Returns:
            Number of rows inserted.
        """
        table_ref = f"{self.dataset_ref}.{table_name}"

        # Step 1: Delete existing rows for this snapshot_date
        delete_query = f"DELETE FROM `{table_ref}` WHERE snapshot_date = @snapshot_date"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("snapshot_date", "DATE", str(snapshot_date))
            ]
        )
        self.client.query(delete_query, job_config=job_config).result()
        logger.info(f"Deleted existing rows from {table_name} for {snapshot_date}")

        # Step 2: Batch insert new rows
        if not rows:
            logger.info(f"No rows to insert into {table_name}")
            return 0

        # Add snapshot_date to each row
        for row in rows:
            row["snapshot_date"] = str(snapshot_date)

        # Use batch load (not streaming) for consistency with the DELETE above
        json_data = "\n".join(json.dumps(row) for row in rows)
        load_job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        load_job = self.client.load_table_from_file(
            io.BytesIO(json_data.encode()),
            table_ref,
            job_config=load_job_config,
        )
        load_job.result()  # Wait for completion

        logger.info(f"Inserted {len(rows)} rows into {table_name}")
        return len(rows)
