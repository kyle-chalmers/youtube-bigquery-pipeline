"""YouTube Analytics API v2 client for fetching watch time, engagement, and traffic data."""

import logging
from datetime import date
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from oauth_credentials import load_oauth_credentials
from retry import RETRYABLE_STATUSES as _SHARED_RETRYABLE
from retry import with_retry

logger = logging.getLogger(__name__)


# The unfiltered video query is the "Top videos" report, which is a capped top-N
# report rather than a paginated list. From the report spec at
# developers.google.com/youtube/analytics/channel_reports (section "Top videos"):
#   "These reports require you to set the maxResults parameter to an integer value
#    of 200 or less. ... these reports also require you to specify a value for the
#    sort request parameter."
# That single note explains every observation: 200 works, 250/500/1000 are 400s, and
# omitting maxResults is a 400 because it is required here.
#
# startIndex is documented generically on reports.query as "a pagination mechanism",
# with no caveat, and it is unusable on this API. Probed live 2026-08-29:
#   unfiltered, maxResults=10,  startIndex=11 -> 0 rows, HTTP 200   (silent truncation)
#   unfiltered, maxResults=200, startIndex=11 -> HTTP 400
#   filtered,   maxResults=200, startIndex=11 -> HTTP 500
# The 500 is the nastiest: _api_call_with_retry now retries 5xx, so a startIndex loop
# would hammer a permanent error. Do not reintroduce startIndex on either path.
#
# The escape is not paging, it is changing which report we hit. Adding
# filters=video==a,b,c moves the request off "Top videos" onto "Basic user activity
# statistics" with the filter promoted to a grouping dimension, which reports.query
# blesses explicitly:
#   "When specifying multiple values for the same filter, you can also add that
#    filter to the list of dimensions ... This is true even if the filter is not
#    listed as a supported dimension for a particular report."
# That report carries no row cap, no maxResults requirement and no sort requirement.
# Confirmed live: the filtered call accepts maxResults=1000 while the unfiltered call
# rejects it, so the two are governed by different rules.
#
# The filter itself caps at 500 ids, documented on reports.query under "Specifying
# multiple values for a filter" ("The parameter value can specify up to 500 IDs") and
# enforced exactly: 500 ids is HTTP 200, 501 is HTTP 400. This is a filter limit in
# its own right, unrelated to the 500-item limit on Analytics groups.
#
# SHARD_SIZE stays below RESULT_CAP as belt and braces: a shard of N videos yields at
# most N rows, so a shard under 200 can never trip a 200-row cap even if one applied.
RESULT_CAP = 200
SHARD_SIZE = 100
MAX_FILTER_IDS = 500


class YouTubeAnalyticsAPI:
    """Client for YouTube Analytics API v2 using OAuth2 credentials from Secret Manager."""

    def __init__(self, project_id: str) -> None:
        """Initialize by loading OAuth2 credentials from Secret Manager.

        Args:
            project_id: GCP project ID for Secret Manager access.
        """
        credentials = load_oauth_credentials(project_id)
        self.analytics = build("youtubeAnalytics", "v2", credentials=credentials)

    # An empty response here has more than one cause, and they are not distinguishable
    # from the response itself. A Google engineer enumerated them on
    # issuetracker.google.com/issues/552694602 (2026-08-26): the caller does not own
    # the video; a privacy/anonymization threshold suppressed low-traffic data; or
    # "specific metrics (like averageViewPercentage or engagedViews) can cause the
    # query to return empty rows if there is a backend processing issue with that
    # specific metric."
    #
    # That last one matters here: a single misbehaving metric can zero out this whole
    # six-metric response while the two-metric traffic query keeps working. Probing on
    # 2026-08-29 showed the gaps that day were caused by querying the availability edge
    # rather than by a metric, but that ruled the metric cause out for that day only,
    # not in general. Both are real, which is why the fix is a self-healing re-query in
    # main.py rather than anything that assumes a single cause.
    def get_video_analytics(
        self, video_ids: list[str], analytics_date: date
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch per-video analytics for a given date.

        Makes a single API call with dimensions=video to get all videos at once.

        Args:
            video_ids: List of video IDs (used to filter results).
            analytics_date: The date to query (typically today - 3 days).

        Returns:
            Tuple of (analytics_rows, error_messages).
        """
        errors: list[str] = []
        date_str = str(analytics_date)

        try:
            api_rows = self._fetch_video_rows(video_ids, date_str)
        except Exception as e:
            logger.error(f"Analytics API query failed: {e}")
            return [], [f"Analytics query failed: {str(e)}"]

        # Parse response rows
        video_id_set = set(video_ids)
        rows: list[dict[str, Any]] = []

        for row in api_rows:
            vid = row[0]
            if vid not in video_id_set:
                continue

            rows.append(
                {
                    "video_id": vid,
                    "estimated_minutes_watched": row[1],
                    "average_view_duration_seconds": row[2],
                    "average_view_percentage": row[3],
                    "subscribers_gained": row[4],
                    "subscribers_lost": row[5],
                    "shares": row[6],
                    # Always NULL — see "Known Limitations" in CLAUDE.md.
                    # Thumbnail impressions/CTR are not exposed by the public
                    # YouTube Analytics API v2 (Studio UI uses an internal one).
                    # Annotations were retired by YouTube in 2019.
                    # Card metrics require per-video calls with filters=video==X
                    # and are all-zero on this channel (no cards used).
                    "impressions": None,
                    "impression_ctr": None,
                    "annotation_click_through_rate": None,
                    "card_click_rate": None,
                }
            )

        logger.info(f"Got analytics for {len(rows)} videos (date: {date_str})")
        return rows, errors

    def get_traffic_sources(
        self, video_ids: list[str], analytics_date: date
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch traffic source breakdown per video.

        Per-video calls required since the traffic source dimension needs a video filter.

        Args:
            video_ids: List of video IDs.
            analytics_date: The date to query.

        Returns:
            Tuple of (traffic_rows, error_messages).
        """
        errors: list[str] = []
        all_rows: list[dict[str, Any]] = []
        date_str = str(analytics_date)

        for video_id in video_ids:
            try:
                response = self._api_call_with_retry(
                    lambda vid=video_id: self.analytics.reports()
                    .query(
                        ids="channel==MINE",
                        startDate=date_str,
                        endDate=date_str,
                        dimensions="insightTrafficSourceType",
                        metrics="views,estimatedMinutesWatched",
                        filters=f"video=={vid}",
                    )
                    .execute()
                )

                for row in response.get("rows", []):
                    all_rows.append(
                        {
                            "video_id": video_id,
                            "traffic_source_type": row[0],
                            "views": row[1],
                            "estimated_minutes_watched": row[2],
                        }
                    )

            except Exception as e:
                logger.warning(f"Traffic sources failed for {video_id}: {e}")
                errors.append(f"{video_id}: {str(e)}")

        logger.info(
            f"Got traffic sources: {len(all_rows)} rows for {len(video_ids)} videos"
        )
        return all_rows, errors

    # 429 is rate limiting; 500/502/503/504 are transient server faults. Both are
    # worth retrying. Retrying only 429 silently dropped a video's traffic on the
    # first HTTP 500, which is how two videos went missing during the 2026-08-29
    # recovery until the retry was widened.
    RETRYABLE_STATUSES = _SHARED_RETRYABLE

    METRICS = (
        "estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
        "subscribersGained,subscribersLost,shares"
    )

    def _query_videos(
        self, date_str: str, video_ids: list[str] | None = None
    ) -> list[list[Any]]:
        """One video-dimension call, optionally restricted to a set of video ids."""
        params: dict[str, Any] = dict(
            ids="channel==MINE",
            startDate=date_str,
            endDate=date_str,
            dimensions="video",
            metrics=self.METRICS,
            sort="-estimatedMinutesWatched",
            maxResults=RESULT_CAP,
        )
        if video_ids:
            params["filters"] = "video==" + ",".join(video_ids)
        response = self._api_call_with_retry(
            lambda: self.analytics.reports().query(**params).execute()
        )
        return response.get("rows", [])

    def _fetch_video_rows(self, video_ids: list[str], date_str: str) -> list[list[Any]]:
        """Fetch every video row for a date, working around the 200-row report cap.

        Fast path is a single unfiltered call. If it comes back exactly at the cap,
        the report has silently dropped the tail, so re-fetch in shards instead.

        Args:
            video_ids: The channel's videos, used to build shards.
            date_str: The activity date to query.

        Returns:
            Rows for the date, deduplicated by video id.
        """
        rows = self._query_videos(date_str)
        if len(rows) < RESULT_CAP:
            return rows

        logger.error(
            f"Video report hit the {RESULT_CAP}-row cap for {date_str}; the report is "
            f"truncated. Re-fetching in shards of {SHARD_SIZE} across "
            f"{len(video_ids)} videos."
        )

        merged: dict[str, list[Any]] = {}
        for i in range(0, len(video_ids), SHARD_SIZE):
            shard = video_ids[i : i + SHARD_SIZE]
            shard_rows = self._query_videos(date_str, shard)
            if len(shard_rows) >= RESULT_CAP:
                logger.error(
                    f"Shard of {len(shard)} videos also hit the cap for {date_str}. "
                    f"Lower SHARD_SIZE."
                )
            for row in shard_rows:
                merged[row[0]] = row

        logger.info(
            f"Sharded fetch recovered {len(merged)} rows for {date_str} "
            f"(unsharded call returned {len(rows)})"
        )
        return list(merged.values())

    @staticmethod
    def _api_call_with_retry(
        callable_fn: Any, max_retries: int = 3
    ) -> dict[str, Any]:
        """Execute an API call with exponential backoff on transient failures.

        Delegates to the shared with_retry so the Analytics and Reporting clients and
        the backfill script cannot drift apart on which statuses are retried. The
        attempt count stays a caller decision: the Cloud Function uses 3.
        """
        return with_retry(callable_fn, max_retries=max_retries)
