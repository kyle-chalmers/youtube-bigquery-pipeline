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
        self, analytics: list[dict[str, Any]], snapshot_date: date,
        activity_date: date, load_source: str = "cron"
    ) -> int:
        """Write daily video analytics from the Analytics API.

        Keyed on activity_date, not snapshot_date. These rows describe a day of
        viewer activity, and that day is what a re-run must replace. Keying the
        delete on snapshot_date would erase every row collected on the same day,
        including recovered history backfilled under today's date.

        Args:
            analytics: List of analytics dicts per video.
            snapshot_date: The day this data was collected.
            activity_date: The day the activity happened. The idempotency key.
            load_source: Provenance tag, e.g. "cron" or "recovery_20260829".

        Returns:
            Number of rows written.
        """
        for row in analytics:
            row["snapshot_date"] = str(snapshot_date)
            row["load_source"] = load_source
        return self._delete_and_insert(
            "daily_video_analytics", analytics, activity_date,
            partition_column="activity_date",
        )

    def write_daily_traffic_sources(
        self, traffic: list[dict[str, Any]], snapshot_date: date,
        activity_date: date, load_source: str = "cron"
    ) -> int:
        """Write daily traffic source data.

        Keyed on activity_date. See write_daily_video_analytics for why.

        Args:
            traffic: List of traffic source dicts.
            snapshot_date: The day this data was collected.
            activity_date: The day the activity happened. The idempotency key.
            load_source: Provenance tag.

        Returns:
            Number of rows written.
        """
        for row in traffic:
            row["snapshot_date"] = str(snapshot_date)
            row["load_source"] = load_source
        return self._delete_and_insert(
            "daily_traffic_sources", traffic, activity_date,
            partition_column="activity_date",
        )

    def find_missing_activity_dates(
        self, table_name: str, earliest: date, latest: date, limit: int = 5
    ) -> list[date]:
        """Return activity dates in [earliest, latest] that have no rows at all.

        The pipeline used to fetch one date per run and never look back, so any day
        the Analytics API was not yet ready became a permanent hole. Confirmed holes
        at activity 2026-07-03, 07-04, 07-14 and 08-11 were all recoverable months
        later, which means a re-query is all that was ever needed.

        Args:
            table_name: Table to check.
            earliest: Oldest activity date to consider.
            latest: Newest activity date to consider.
            limit: Cap on dates returned, so one run cannot fan out unboundedly.

        Returns:
            Missing dates, oldest first.
        """
        query = f"""
            SELECT missing_date
            FROM UNNEST(GENERATE_DATE_ARRAY(@earliest, @latest)) AS missing_date
            WHERE missing_date NOT IN (
                SELECT DISTINCT activity_date
                FROM `{self.dataset_ref}.{table_name}`
                WHERE activity_date BETWEEN @earliest AND @latest
            )
            ORDER BY missing_date
            LIMIT {int(limit)}
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("earliest", "DATE", str(earliest)),
                bigquery.ScalarQueryParameter("latest", "DATE", str(latest)),
            ]
        )
        return [row[0] for row in self.client.query(query, job_config=job_config).result()]

    def _delete_and_insert(
        self,
        table_name: str,
        rows: list[dict[str, Any]],
        partition_value: date,
        partition_column: str = "snapshot_date",
    ) -> int:
        """Idempotent write: replace the rows for one partition value.

        Uses batch loading (not streaming insert) to avoid eventual consistency
        issues with BigQuery's streaming buffer.

        The DELETE runs only after we know there are rows to replace it with.
        Deleting first cost this warehouse three days of history: on 2026-05-25 a
        backfill deleted partitions it then wrote activity-dated rows into, and the
        collection-dated rows already there were destroyed. An empty API response
        is not a licence to erase a populated partition.

        Args:
            table_name: BigQuery table name (without project/dataset prefix).
            rows: List of row dicts to insert.
            partition_value: The value to delete on.
            partition_column: Column the delete keys on. Analytics tables key on
                activity_date; Data API snapshot tables key on snapshot_date.

        Returns:
            Number of rows inserted.
        """
        table_ref = f"{self.dataset_ref}.{table_name}"

        if not rows:
            logger.warning(
                f"No rows to write into {table_name} for {partition_column}="
                f"{partition_value}; leaving the existing partition untouched"
            )
            return 0

        # Stamp the partition column, then delete, then insert.
        for row in rows:
            row[partition_column] = str(partition_value)

        delete_query = (
            f"DELETE FROM `{table_ref}` WHERE {partition_column} = @partition_value"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "partition_value", "DATE", str(partition_value)
                )
            ]
        )
        self.client.query(delete_query, job_config=job_config).result()
        logger.info(
            f"Deleted existing rows from {table_name} for "
            f"{partition_column}={partition_value}"
        )

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
