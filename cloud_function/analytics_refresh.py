"""Trailing-N-day Analytics API refresh.

YouTube revises Analytics numbers (e.g. invalid-traffic removal) for up to ~30 days
after the fact. The Reporting API pipeline (reporting_loader.py) already picks these
revisions up automatically by diffing report createTime. The Analytics API tables this
module refreshes (daily_video_analytics, daily_traffic_sources) have no equivalent —
once an activity_date is loaded by the daily cron, it is never re-read. This module is
the fix: re-fetch and overwrite the trailing window on a schedule (see refresh_main.py).

SOFT-GUARD INVARIANT — read before changing either this module's schedule or
cloud_function/main.py's gap-repair schedule:

This module assumes it never runs concurrently with the daily function's
`_repair_gaps` (main.py). Both write via DELETE+INSERT to the same activity_date
partitions on daily_video_analytics/daily_traffic_sources, and their windows overlap by
design (gap repair covers roughly the last GAP_LOOKBACK_DAYS before the daily lookback
boundary; this refresh covers a trailing window ending at the same boundary). If the two
ever executed against the same partition at the same time, the result could be
duplicated rows.

There is deliberately no code-level lock here — Kyle's call, to keep the architecture
simple for a residual risk this cheap to detect. Safety comes entirely from:
  1. Scheduling this job far from the daily 00:10 America/Phoenix run (weekly, e.g.
     Sunday 03:00 Phoenix — see the scheduler invocation in setup/12_deploy_refresh_function.sh).
  2. MAX_RETRY_ATTEMPTS=0 on this job's Cloud Scheduler entry (setup/5_create_scheduler.sh),
     so a slow run that misses its attempt deadline does NOT get retried into a second,
     overlapping invocation — Cloud Run does not cancel the first one just because
     Scheduler gave up waiting on it.
Anyone changing either schedule must re-verify the separation holds. A violation would
show up as duplicate (activity_date, video_id) rows in daily_video_analytics or
daily_traffic_sources — see the duplicate-row check in the Phase 4 refresh plan's
verification SQL.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from bigquery_writer import BigQueryWriter, ReplaceRefused
from log_safety import redact

logger = logging.getLogger(__name__)

# Log-string prefix a skipped/refused day is reported under. refresh_main.py's monitoring
# alert matches this substring; if you reword it, update setup/6_setup_monitoring.sh in
# the same commit (pinned by tests/test_analytics_refresh.py).
INCOMPLETE_LOG = "refresh_incomplete"

# Refuse a replacement if the freshly fetched row count is below this fraction of what's
# currently in the partition. Guards against the documented single-metric-zeroing
# failure mode and the 200-row cap/shard logic both returning fewer rows than expected
# with an empty error list — refusing only on non-empty fetch errors would miss this.
# NEEDS KYLE'S SIGN-OFF: this number is a proposal, not a validated business rule.
# Override via REFRESH_MIN_ROW_RATIO in refresh_main.py before trusting it in prod.
DEFAULT_MIN_ROW_RATIO = 0.5


def compute_refresh_window(
    run_date: date, trailing_days: int, lookback_days: int
) -> tuple[date, date]:
    """Inclusive (start, end) activity-date window to refresh.

    Ends `lookback_days` before run_date — the same freshness boundary the daily
    function trusts (its ANALYTICS_LOOKBACK_DAYS) — so this never re-queries the
    availability dead zone that's empty because YouTube hasn't published it yet, not
    because it needs revising. `lookback_days` has no default here: it's always passed
    in from whatever the daily function is actually deployed with (see refresh_main.py's
    ANALYTICS_LOOKBACK_DAYS env var), so the two can't silently drift apart.
    """
    end = run_date - timedelta(days=lookback_days)
    start = end - timedelta(days=trailing_days - 1)
    return start, end


def refresh_trailing_days(
    video_ids: list[str],
    analytics_api: Any,
    bq_writer: BigQueryWriter,
    run_date: date,
    refresh_run_id: str,
    lookback_days: int,
    trailing_days: int = 30,
    load_source: str | None = None,
    min_row_ratio: float = DEFAULT_MIN_ROW_RATIO,
    log: logging.LoggerAdapter | logging.Logger = logger,
) -> dict[str, dict[str, str]]:
    """Re-fetch and replace the trailing `trailing_days` of Analytics data.

    Args:
        video_ids: The current video universe (from the latest video_metadata
            snapshot — see refresh_main.py). A video no longer in that snapshot (fully
            removed from the channel) silently drops out of refresh coverage even
            though its historical rows still exist; this matches the existing
            setup/backfill_analytics.py behavior.
        analytics_api: A YouTubeAnalyticsAPI instance (or test double) exposing
            get_video_analytics(video_ids, date) and
            get_traffic_sources_range(video_ids, start_date, end_date) — NOT
            get_traffic_sources, which is one call per video per day and does not scale
            to a 30-day window (measured live at ~30 minutes for this channel's 204
            videos, versus under a second for the range call). See
            get_traffic_sources_range's docstring in youtube_analytics_api.py.
        bq_writer: BigQueryWriter instance.
        run_date: The day this refresh run happened, in PIPELINE_TZ.
        refresh_run_id: Correlates every row this run touches; threaded through to
            BigQueryWriter.archive_and_replace.
        lookback_days: Passed straight to compute_refresh_window — always the deployed
            ANALYTICS_LOOKBACK_DAYS, not a value invented for this module.
        trailing_days: How many activity dates back to cover.
        load_source: Provenance tag; defaults to f"refresh_{run_date:%Y%m%d}".
        min_row_ratio: See DEFAULT_MIN_ROW_RATIO.
        log: LoggerAdapter (carries refresh_run_id) or the bare module logger.

    Returns:
        A per-(activity_date ISO string, table_name) outcome map, e.g.
        {"2026-08-10": {"daily_video_analytics": "written",
                        "daily_traffic_sources": "skipped_error"}}
        Outcomes: "written", "skipped_empty", "skipped_error", "skipped_low_count".
        This is what an operator needs after a 30-day run — a single aggregate count
        would hide which specific days, if any, didn't refresh cleanly.
    """
    load_source = load_source or f"refresh_{run_date:%Y%m%d}"
    start, end = compute_refresh_window(run_date, trailing_days, lookback_days)
    log.info(
        f"Refresh window {start} to {end} ({trailing_days} days, "
        f"lookback_days={lookback_days}, load_source={load_source})"
    )

    # One range call (or a handful, if sharded by video count) covers every day's
    # traffic sources at once — see get_traffic_sources_range's docstring for why this
    # replaced a per-day loop. traffic_shard_errors is shard-level, not per-day: a
    # failed shard can leave some videos silently missing from EVERY day in the window,
    # not just the day it "looks like" it affected, so every day is treated as errored
    # for daily_traffic_sources when it's non-empty (see the loop below).
    traffic_by_date, traffic_shard_errors = analytics_api.get_traffic_sources_range(
        video_ids, start, end
    )
    if traffic_shard_errors:
        # get_traffic_sources_range redacts when it logs a shard failure at the source,
        # but the strings it returns to us are raw exception text — redact again here
        # before this joins and logs them, the same discipline every other exception-to-
        # log path in this codebase follows (see log_safety.py's docstring for why).
        log.warning(
            f"Traffic range fetch had {len(traffic_shard_errors)} shard-level error(s); "
            f"every day in the window will be skipped for daily_traffic_sources this "
            f"run: {redact('; '.join(traffic_shard_errors))}"
        )

    outcomes: dict[str, dict[str, str]] = {}
    current = start
    while current <= end:
        outcomes[str(current)] = {
            "daily_video_analytics": _refresh_one_table(
                bq_writer=bq_writer,
                table_name="daily_video_analytics",
                archive_table="daily_video_analytics_refresh_archive",
                fetch_result=analytics_api.get_video_analytics(video_ids, current),
                activity_date=current,
                run_date=run_date,
                load_source=load_source,
                refresh_run_id=refresh_run_id,
                min_row_ratio=min_row_ratio,
                log=log,
            ),
            "daily_traffic_sources": _refresh_one_table(
                bq_writer=bq_writer,
                table_name="daily_traffic_sources",
                archive_table="daily_traffic_sources_refresh_archive",
                fetch_result=(traffic_by_date.get(current, []), traffic_shard_errors),
                activity_date=current,
                run_date=run_date,
                load_source=load_source,
                refresh_run_id=refresh_run_id,
                min_row_ratio=min_row_ratio,
                log=log,
            ),
        }
        current += timedelta(days=1)

    return outcomes


def _refresh_one_table(
    bq_writer: BigQueryWriter,
    table_name: str,
    archive_table: str,
    fetch_result: tuple[list[dict[str, Any]], list[str]],
    activity_date: date,
    run_date: date,
    load_source: str,
    refresh_run_id: str,
    min_row_ratio: float,
    log: logging.LoggerAdapter | logging.Logger,
) -> str:
    """Refuse-or-replace one (table, activity_date). Returns an outcome string.

    Order matters and is deliberate:
      1. Any per-video fetch error -> refuse. A partial result would delete a complete
         day and replace it with an incomplete one — real data loss, not just staleness.
      2. Empty result with no errors -> leave untouched, log severity depends on
         whether the day was previously populated (see count_rows_for_activity_date's
         docstring).
      3. Otherwise -> archive_and_replace, which does its own row-count-ratio check and
         raises ReplaceRefused if the new count looks suspiciously low.
    Never call archive_and_replace with an empty or error-tainted fetch result.
    """
    rows, errors = fetch_result

    if errors:
        log.warning(
            f"{INCOMPLETE_LOG} — {table_name} activity_date={activity_date} had "
            f"{len(errors)} per-video fetch errors; leaving the existing partition "
            f"untouched, next scheduled run will retry"
        )
        return "skipped_error"

    if not rows:
        existing_count = bq_writer.count_rows_for_activity_date(table_name, activity_date)
        if existing_count > 0:
            log.warning(
                f"{INCOMPLETE_LOG} — {table_name} activity_date={activity_date} "
                f"previously had {existing_count} rows, refresh fetched 0 with no "
                f"errors; leaving untouched (documented single-metric-zeroing failure "
                f"mode, not treated as a real revision-to-zero)"
            )
        else:
            log.info(
                f"{table_name} activity_date={activity_date} fetched 0 rows; no prior "
                f"data either, no change"
            )
        return "skipped_empty"

    try:
        bq_writer.archive_and_replace(
            table_name=table_name,
            archive_table=archive_table,
            new_rows=rows,
            activity_date=activity_date,
            snapshot_date=run_date,
            load_source=load_source,
            refresh_run_id=refresh_run_id,
            min_row_ratio=min_row_ratio,
        )
    except ReplaceRefused as e:
        log.warning(
            f"{INCOMPLETE_LOG} — {table_name} activity_date={activity_date} refused: {e}"
        )
        return "skipped_low_count"

    log.info(f"{table_name} activity_date={activity_date} replaced with {len(rows)} rows")
    return "written"
