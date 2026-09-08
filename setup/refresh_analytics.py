"""Manual/staging invocation of the trailing-N-day Analytics refresh.

Calls cloud_function/analytics_refresh.py directly rather than duplicating fetch logic
the way setup/backfill_analytics.py does — see that script's write_rows docstring for
why that duplication was a mistake worth not repeating.

Usage:
    python3 setup/refresh_analytics.py --dataset youtube_analytics_staging --trailing-days 30

This is also the tool for the timed staging rehearsal that sets
setup/12_deploy_refresh_function.sh's --timeout — run it once, end to end, and time it
before deploying anything to prod.
"""

import argparse
import logging
import os
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from google.cloud import bigquery

import _bootstrap  # noqa: F401  (adds cloud_function/ to sys.path)

# isort: split   (everything below needs _bootstrap to have run first)
from analytics_refresh import DEFAULT_MIN_ROW_RATIO, refresh_trailing_days
from bigquery_writer import BigQueryWriter
from youtube_analytics_api import YouTubeAnalyticsAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ID = _bootstrap.resolve_project()


def main():
    parser = argparse.ArgumentParser(description="Refresh the trailing N days of Analytics data")
    parser.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "youtube_analytics"))
    parser.add_argument("--trailing-days", type=int, default=30)
    parser.add_argument(
        "--lookback-days", type=int,
        default=int(os.environ.get("ANALYTICS_LOOKBACK_DAYS", "5")),
        help="Must match whatever the daily function is deployed with, not the code default.",
    )
    parser.add_argument("--run-date", default=None, help="Override today (YYYY-MM-DD), for rehearsal")
    parser.add_argument("--load-source", default=None)
    parser.add_argument("--min-row-ratio", type=float, default=DEFAULT_MIN_ROW_RATIO)
    args = parser.parse_args()

    run_date = (
        datetime.strptime(args.run_date, "%Y-%m-%d").date()
        if args.run_date
        else datetime.now(ZoneInfo(os.environ.get("PIPELINE_TZ", "America/Phoenix"))).date()
    )
    refresh_run_id = str(uuid.uuid4())[:8]
    load_source = args.load_source or f"refresh_{run_date:%Y%m%d}"

    logger.info(
        f"Refreshing trailing {args.trailing_days} days ending {args.trailing_days} days "
        f"before {run_date} (lookback_days={args.lookback_days}), dataset={args.dataset}, "
        f"load_source={load_source}, refresh_run_id={refresh_run_id}"
    )

    bq_client = bigquery.Client(project=PROJECT_ID)
    query = (
        f"SELECT DISTINCT video_id FROM `{PROJECT_ID}.{args.dataset}.video_metadata` "
        f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM "
        f"`{PROJECT_ID}.{args.dataset}.video_metadata`)"
    )
    video_ids = [row.video_id for row in bq_client.query(query).result()]
    logger.info(f"Found {len(video_ids)} videos to refresh")

    analytics_api = YouTubeAnalyticsAPI(project_id=PROJECT_ID)
    bq_writer = BigQueryWriter(project_id=PROJECT_ID, dataset_id=args.dataset)

    started = time.monotonic()
    outcomes = refresh_trailing_days(
        video_ids=video_ids,
        analytics_api=analytics_api,
        bq_writer=bq_writer,
        run_date=run_date,
        refresh_run_id=refresh_run_id,
        lookback_days=args.lookback_days,
        trailing_days=args.trailing_days,
        load_source=load_source,
        min_row_ratio=args.min_row_ratio,
    )
    elapsed = time.monotonic() - started

    counts = {"written": 0, "skipped_empty": 0, "skipped_error": 0, "skipped_low_count": 0}
    for per_table in outcomes.values():
        for outcome in per_table.values():
            counts[outcome] = counts.get(outcome, 0) + 1

    logger.info(
        f"Refresh complete in {elapsed:.1f}s — days={len(outcomes)} written={counts['written']} "
        f"skipped_empty={counts['skipped_empty']} skipped_error={counts['skipped_error']} "
        f"skipped_low_count={counts['skipped_low_count']}"
    )
    logger.info(
        "Use this elapsed time (with real headroom) to set --timeout in "
        "setup/12_deploy_refresh_function.sh — do not deploy on an untested guess."
    )


if __name__ == "__main__":
    main()
