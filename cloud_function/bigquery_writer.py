"""BigQuery writer for YouTube analytics pipeline.

Handles idempotent writes to all 4 tables using DELETE + batch load pattern.
"""

import io
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from google.cloud import bigquery

from log_safety import redact

logger = logging.getLogger(__name__)

# Used only by archive_and_replace (the trailing-30-day refresh job). Retry policy
# mirrors partition_replacer.py's StagedTransactionalReplacer, which solves the same
# "stage then transact" problem for the Reporting tables — same transient-error
# vocabulary, not a shared import, since that module is coupled to the reporting ledger
# and this one isn't.
_REFRESH_TRANSIENT_MARKERS = (
    "concurrent", "transaction is aborted", "abort", "backenderror",
    "internalerror", "ratelimitexceeded",
)
_REFRESH_TRANSIENT_RETRIES = 3
_REFRESH_TRANSIENT_BACKOFF_SECONDS = (5, 10, 20)


class ReplaceRefused(RuntimeError):
    """archive_and_replace's transaction refused to commit; nothing was written."""


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

    def count_rows_for_activity_date(self, table_name: str, activity_date: date) -> int:
        """Row count for one activity_date partition.

        Used by the trailing-30-day refresh job to tell "this day was already empty"
        (expected, INFO) apart from "this day had rows and a re-fetch came back empty"
        (the documented single-metric-zeroing failure mode, WARNING) — both leave the
        partition untouched, but only one is worth flagging.
        """
        query = (
            f"SELECT COUNT(*) FROM `{self.dataset_ref}.{table_name}` "
            f"WHERE activity_date = @activity_date"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("activity_date", "DATE", str(activity_date))
            ]
        )
        return next(iter(self.client.query(query, job_config=job_config).result()))[0]

    def archive_and_replace(
        self,
        table_name: str,
        archive_table: str,
        new_rows: list[dict[str, Any]],
        activity_date: date,
        snapshot_date: date,
        load_source: str,
        refresh_run_id: str,
        min_row_ratio: float,
    ) -> int:
        """Atomically archive an activity_date partition, then replace it with new_rows.

        Used only by the trailing-30-day refresh job (cloud_function/analytics_refresh.py),
        never by the daily cron or gap repair — those keep the plain _delete_and_insert
        two-step, which is fine for them because they only ever touch empty or
        just-collected partitions, not partitions being revised out from under other
        readers. This method exists because refreshing a day that already has rows needs
        stronger guarantees: a crash between archiving and replacing must not lose data
        either way, and an API response that's merely smaller (not empty, not erroring)
        must not silently replace good data with worse data.

        Everything happens in one BigQuery script transaction: assert the new row count
        isn't suspiciously low relative to what's there now, archive the existing rows,
        delete them, insert the new ones. A failed assertion rolls back the whole thing.
        This is a smaller, purpose-built cousin of partition_replacer.py's
        StagedTransactionalReplacer (same stage-then-transact shape, same retry
        vocabulary) without that module's reporting-ledger coupling.

        Soft-guard note: this method makes ITS OWN operation atomic. It does not protect
        against a genuinely concurrent writer (e.g. the daily cron's gap repair touching
        the same activity_date at the same time) — that risk is accepted and mitigated by
        scheduling, not by a lock. See the module docstring in analytics_refresh.py.

        Args:
            table_name: Live table, e.g. "daily_video_analytics".
            archive_table: Where the pre-replace rows go, e.g.
                "daily_video_analytics_refresh_archive".
            new_rows: Freshly fetched rows for activity_date. Must be non-empty — the
                caller's own zero-row guard decides whether to call this at all.
            activity_date: The day being refreshed.
            snapshot_date: The day this refresh run happened (stamped like any other write).
            load_source: Provenance tag, e.g. "refresh_20260913".
            refresh_run_id: Correlates every row this invocation touches with the run
                that touched it, and is what the before/after diff query filters on.
            min_row_ratio: Refuse the replacement if the new row count is below this
                fraction of the existing row count for the day. This threshold is a
                business-rule decision, not a code default — callers must pass it
                explicitly (see REFRESH_MIN_ROW_RATIO in analytics_refresh.py).

        Returns:
            Number of rows written.

        Raises:
            ValueError: new_rows is empty.
            ReplaceRefused: the transaction's own assertions refused to commit.
        """
        if not new_rows:
            raise ValueError(
                "archive_and_replace requires non-empty new_rows; the zero-row guard "
                "belongs in the caller, not here"
            )

        table_ref = f"{self.dataset_ref}.{table_name}"
        archive_ref = f"{self.dataset_ref}.{archive_table}"

        for row in new_rows:
            row["activity_date"] = str(activity_date)
            row["snapshot_date"] = str(snapshot_date)
            row["load_source"] = load_source

        live_schema = self.client.get_table(table_ref).schema
        columns = [field.name for field in live_schema]
        col_list = ", ".join(columns)

        work_table = f"_refresh_{table_name}_{uuid.uuid4().hex[:8]}"
        work_ref = f"{self.dataset_ref}.{work_table}"

        try:
            self._stage_refresh_rows(work_ref, live_schema, new_rows)
            script = f"""
BEGIN
  BEGIN TRANSACTION;

  IF (SELECT COUNT(*) FROM `{work_ref}`) <
     @min_row_ratio * (SELECT COUNT(*) FROM `{table_ref}` WHERE activity_date = @activity_date)
  THEN
    RAISE USING MESSAGE = 'refused: new row count is below min_row_ratio of the existing count';
  END IF;

  INSERT INTO `{archive_ref}` ({col_list}, archived_at, refresh_run_id)
  SELECT {col_list}, CURRENT_TIMESTAMP(), @refresh_run_id
  FROM `{table_ref}` WHERE activity_date = @activity_date;

  DELETE FROM `{table_ref}` WHERE activity_date = @activity_date;

  INSERT INTO `{table_ref}` ({col_list})
  SELECT {col_list} FROM `{work_ref}`;

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;
"""
            params = [
                bigquery.ScalarQueryParameter("activity_date", "DATE", str(activity_date)),
                bigquery.ScalarQueryParameter("refresh_run_id", "STRING", refresh_run_id),
                bigquery.ScalarQueryParameter("min_row_ratio", "FLOAT64", min_row_ratio),
            ]
            self._run_transactional_script_with_retry(script, params)
        finally:
            self._drop_work_table(work_ref)

        logger.info(
            f"archive_and_replace: {table_name} activity_date={activity_date} "
            f"replaced with {len(new_rows)} rows (refresh_run_id={refresh_run_id})"
        )
        return len(new_rows)

    def _stage_refresh_rows(
        self, work_ref: str, schema: list[bigquery.SchemaField], rows: list[dict[str, Any]]
    ) -> None:
        """Load rows into a real, expiring work table with the live table's own schema.

        BigQuery CREATE TEMP TABLE cannot back a load job and DDL cannot run inside a
        transaction, so this is a real table — random-suffixed name, expiry set at
        creation so a crash before _drop_work_table runs still cleans up within an hour.
        An explicit schema (not NDJSON autodetect) matters here specifically because
        autodetect would type activity_date as STRING, which then fails to UNION/INSERT
        against the live table's DATE column.
        """
        table = bigquery.Table(work_ref, schema=schema)
        table.expires = datetime.now(timezone.utc) + timedelta(hours=1)
        self.client.create_table(table)
        json_data = "\n".join(json.dumps(row) for row in rows)
        load_job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=schema,
        )
        self.client.load_table_from_file(
            io.BytesIO(json_data.encode()), work_ref, job_config=load_job_config
        ).result()

    def _drop_work_table(self, work_ref: str) -> None:
        try:
            self.client.delete_table(work_ref, not_found_ok=True)
        except Exception as e:  # noqa: BLE001 - it expires within the hour regardless
            logger.warning(f"could not drop refresh work table {work_ref}: {redact(str(e))}")

    def _run_transactional_script_with_retry(
        self, script: str, params: list[Any]
    ) -> None:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        for attempt in range(_REFRESH_TRANSIENT_RETRIES + 1):
            try:
                self.client.query(script, job_config=job_config).result()
                return
            except Exception as e:  # noqa: BLE001 - classified below
                msg = str(e)
                low = msg.lower()
                if "refused:" in low:
                    raise ReplaceRefused(msg) from e
                if any(m in low for m in _REFRESH_TRANSIENT_MARKERS) and attempt < _REFRESH_TRANSIENT_RETRIES:
                    wait = _REFRESH_TRANSIENT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        f"transient BigQuery error in archive_and_replace, retrying in "
                        f"{wait}s (attempt {attempt + 1}/{_REFRESH_TRANSIENT_RETRIES}): "
                        f"{redact(msg[:200])}"
                    )
                    time.sleep(wait)
                    continue
                raise

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
