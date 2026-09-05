"""Golden tests for the pure parsing functions in youtube_data_api.

These functions are the schema of video_metadata in disguise: parse_duration decides
duration_seconds and duration_formatted, classify_video_type decides video_type.
"""

import pytest

from youtube_data_api import SHORTS_THRESHOLD_SECONDS, YouTubeDataAPI


@pytest.mark.parametrize(
    "iso, expected",
    [
        ("PT12M34S", (754, "12:34")),
        ("PT1H12M54S", (4374, "1:12:54")),
        ("PT45S", (45, "0:45")),
        ("PT3M", (180, "3:00")),
        ("PT2H", (7200, "2:00:00")),
        ("PT0S", (0, "0:00")),
        ("garbage", (0, "0:00")),  # unparseable falls back rather than raising
        # Pre-existing behaviour, pinned so it is a decision and not a surprise: the
        # regex requires the string to start with PT, so a day component (a 26-hour
        # stream, "P1DT2H3M") and a live stream in progress ("P0D") both parse to zero
        # and classify as "short". Fixing this is a Phase 4 candidate.
        ("P1DT2H3M", (0, "0:00")),
        ("P0D", (0, "0:00")),
    ],
)
def test_parse_duration(iso, expected):
    assert YouTubeDataAPI.parse_duration(iso) == expected


def test_shorts_threshold_is_180_and_inclusive():
    assert SHORTS_THRESHOLD_SECONDS == 180
    assert YouTubeDataAPI.classify_video_type(180) == "short"
    assert YouTubeDataAPI.classify_video_type(181) == "full_length"
    assert YouTubeDataAPI.classify_video_type(0) == "short"


def test_parse_video_item_golden():
    item = {
        "id": "abc123",
        "snippet": {
            "title": "Test video",
            "publishedAt": "2026-08-01T15:00:00Z",
            "tags": ["a", "b"],
            "categoryId": "28",
            "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"}},
        },
        "contentDetails": {"duration": "PT4M5S"},
        "statistics": {"viewCount": "10", "likeCount": "2", "commentCount": "1"},
    }
    api = YouTubeDataAPI.__new__(YouTubeDataAPI)
    row = api._parse_video_item(item)
    assert row["video_id"] == "abc123"
    assert row["duration_seconds"] == 245
    assert row["duration_formatted"] == "4:05"
    assert row["video_type"] == "full_length"
    assert row["view_count"] == 10
    assert row["like_count"] == 2
    assert row["comment_count"] == 1
    assert row["favorite_count"] == 0  # absent in the payload, must default not crash


def test_hidden_like_count_is_stored_as_zero_by_design():
    # When a creator hides likes, the API omits likeCount. The parser stores 0, which is
    # indistinguishable from zero likes. Documented rule, pinned; NULL is the alternative.
    item = {"id": "x", "snippet": {}, "contentDetails": {"duration": "PT1M"}, "statistics": {"viewCount": "5"}}
    api = YouTubeDataAPI.__new__(YouTubeDataAPI)
    row = api._parse_video_item(item)
    assert row["like_count"] == 0 and row["comment_count"] == 0 and row["view_count"] == 5
    assert row["tags"] == "" and row["thumbnail_url"] == ""
