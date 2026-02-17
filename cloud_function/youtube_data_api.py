"""YouTube Data API v3 client for fetching video metadata and public stats."""

import logging
import re
from typing import Any

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SHORTS_THRESHOLD_SECONDS = 180


class YouTubeDataAPI:
    """Client for YouTube Data API v3."""

    def __init__(self, api_key: str, uploads_playlist_id: str) -> None:
        """Initialize with API key and uploads playlist ID.

        Args:
            api_key: YouTube Data API v3 key.
            uploads_playlist_id: The uploads playlist ID (UC -> UU prefix).
        """
        self.youtube = build("youtube", "v3", developerKey=api_key)
        self.uploads_playlist_id = uploads_playlist_id

    def get_all_video_ids(self) -> list[str]:
        """Fetch all video IDs from the uploads playlist.

        Handles pagination (max 50 per page).

        Returns:
            List of video ID strings.
        """
        video_ids: list[str] = []
        next_page_token: str | None = None

        while True:
            request = self.youtube.playlistItems().list(
                part="contentDetails",
                playlistId=self.uploads_playlist_id,
                maxResults=50,
                pageToken=next_page_token,
            )
            response = request.execute()

            for item in response.get("items", []):
                video_id = item["contentDetails"]["videoId"]
                video_ids.append(video_id)

            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        logger.info(f"Found {len(video_ids)} videos in uploads playlist")
        return video_ids

    def get_video_details(self, video_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch full details for a list of video IDs.

        Batches into groups of 50 (API max per request).
        Fetches parts: snippet, contentDetails, statistics.

        Args:
            video_ids: List of YouTube video IDs.

        Returns:
            List of processed video detail dicts with keys matching
            the video_metadata and daily_video_stats schemas.
        """
        all_details: list[dict[str, Any]] = []

        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            request = self.youtube.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(batch),
            )
            response = request.execute()

            for item in response.get("items", []):
                details = self._parse_video_item(item)
                all_details.append(details)

            logger.info(f"Fetched details for batch {i // 50 + 1} ({len(batch)} videos)")

        return all_details

    def _parse_video_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Parse a single video API response item into a flat dict.

        Args:
            item: A single item from the videos.list API response.

        Returns:
            Dict with keys for both video_metadata and daily_video_stats tables.
        """
        snippet = item.get("snippet", {})
        content_details = item.get("contentDetails", {})
        statistics = item.get("statistics", {})

        duration_seconds, duration_formatted = self.parse_duration(
            content_details.get("duration", "PT0S")
        )

        tags = snippet.get("tags", [])
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (
            thumbnails.get("maxresdefault", {}).get("url")
            or thumbnails.get("high", {}).get("url")
            or thumbnails.get("default", {}).get("url", "")
        )

        return {
            "video_id": item["id"],
            "title": snippet.get("title", ""),
            "published_at": snippet.get("publishedAt", ""),
            "duration_seconds": duration_seconds,
            "duration_formatted": duration_formatted,
            "video_type": self.classify_video_type(duration_seconds),
            "tags": ",".join(tags) if tags else "",
            "category_id": snippet.get("categoryId", ""),
            "thumbnail_url": thumbnail_url,
            # Stats (for daily_video_stats table)
            "view_count": int(statistics.get("viewCount", 0)),
            "like_count": int(statistics.get("likeCount", 0)),
            "comment_count": int(statistics.get("commentCount", 0)),
            "favorite_count": int(statistics.get("favoriteCount", 0)),
        }

    @staticmethod
    def parse_duration(iso_duration: str) -> tuple[int, str]:
        """Parse ISO 8601 duration to seconds and formatted string.

        Args:
            iso_duration: Duration string like 'PT12M34S'.

        Returns:
            Tuple of (total_seconds, formatted_string).
            E.g., (754, '12:34') or (4374, '1:12:54').
        """
        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
        if not match:
            return (0, "0:00")

        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        total_seconds = hours * 3600 + minutes * 60 + seconds

        if hours > 0:
            formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            formatted = f"{minutes}:{seconds:02d}"

        return (total_seconds, formatted)

    @staticmethod
    def classify_video_type(duration_seconds: int) -> str:
        """Classify video as 'short' or 'full_length'.

        Args:
            duration_seconds: Video duration in seconds.

        Returns:
            'short' if <= 180 seconds, else 'full_length'.
        """
        return "short" if duration_seconds <= SHORTS_THRESHOLD_SECONDS else "full_length"
