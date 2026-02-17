"""YouTube Analytics API v2 client for fetching watch time, engagement, and traffic data."""

import logging
import time
from datetime import date
from typing import Any

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Secret Manager secret names
SECRET_CLIENT_ID = "youtube-oauth-client-id"
SECRET_CLIENT_SECRET = "youtube-oauth-client-secret"
SECRET_REFRESH_TOKEN = "youtube-oauth-refresh-token"

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]


class YouTubeAnalyticsAPI:
    """Client for YouTube Analytics API v2 using OAuth2 credentials from Secret Manager."""

    def __init__(self, project_id: str) -> None:
        """Initialize by loading OAuth2 credentials from Secret Manager.

        Args:
            project_id: GCP project ID for Secret Manager access.
        """
        credentials = self._load_credentials(project_id)
        self.analytics = build("youtubeAnalytics", "v2", credentials=credentials)

    def _load_credentials(self, project_id: str) -> Credentials:
        """Load OAuth2 credentials from Secret Manager.

        Args:
            project_id: GCP project ID.

        Returns:
            Credentials instance that auto-refreshes using the stored refresh token.
        """
        client_id = self._get_secret(project_id, SECRET_CLIENT_ID)
        client_secret = self._get_secret(project_id, SECRET_CLIENT_SECRET)
        refresh_token = self._get_secret(project_id, SECRET_REFRESH_TOKEN)

        return Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )

    @staticmethod
    def _get_secret(project_id: str, secret_id: str) -> str:
        """Read a secret value from Secret Manager.

        Args:
            project_id: GCP project ID.
            secret_id: Name of the secret.

        Returns:
            Secret value as string.
        """
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")

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
            response = self._api_call_with_retry(
                lambda: self.analytics.reports()
                .query(
                    ids="channel==MINE",
                    startDate=date_str,
                    endDate=date_str,
                    dimensions="video",
                    metrics="estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,shares",
                    sort="-estimatedMinutesWatched",
                    maxResults=200,
                )
                .execute()
            )
        except Exception as e:
            logger.error(f"Analytics API query failed: {e}")
            return [], [f"Analytics query failed: {str(e)}"]

        # Parse response rows
        video_id_set = set(video_ids)
        rows: list[dict[str, Any]] = []

        for row in response.get("rows", []):
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
                    # Impressions/CTR populated from traffic source data
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

    @staticmethod
    def _api_call_with_retry(
        callable_fn: Any, max_retries: int = 3
    ) -> dict[str, Any]:
        """Execute an API call with exponential backoff on rate limits.

        Args:
            callable_fn: Zero-argument callable that executes the API call.
            max_retries: Maximum number of retry attempts.

        Returns:
            API response dict.

        Raises:
            HttpError: If the call fails after all retries.
        """
        for attempt in range(max_retries + 1):
            try:
                return callable_fn()
            except HttpError as e:
                if e.resp.status == 429 and attempt < max_retries:
                    wait = 2**attempt
                    logger.warning(
                        f"Rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Unreachable")
