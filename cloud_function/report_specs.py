"""Schema registry for every YouTube Reporting API report type this pipeline ingests.

Two registries and one table of grains, instead of 19 hand-written schemas:

- DIMENSIONS: every dimension column any channel or playlist report can carry. All are
  STRING except `date`, which lands as `report_date DATE`.
- METRIC_TYPES: every metric column and its BigQuery type. Counts are INT64; durations,
  rates, percentages and CTRs are FLOAT64.
- SPECS: per report type, which dimensions form its grain and which metrics it carries.
  Column names come from the Reporting API reference (channel_reports) and were checked
  against live CSV headers for the three report types that already had data on 2026-09-05.

The ids here are the ones YouTube offers THIS channel (reportTypes.list on 2026-09-05).
The docs page shows `_a2` for sharing_service, annotations and end_screens; the channel
gets `_a1` with the same columns. Annotations are deliberately absent (retired 2019).

`setup/generate_reporting_ddl.py` renders `sql/reporting_tables.sql` from SPECS, and
tests/test_report_specs.py asserts the committed DDL matches, so specs and DDL cannot drift.
A live CSV column that appears in neither registry raises SchemaDriftError in the parser.
"""

from __future__ import annotations

from dataclasses import dataclass

TABLE_PREFIX = "reporting_"
LEDGER_TABLE = "reporting_ingest_ledger"

# Dimension columns. `date` is the report day (Pacific) and becomes report_date DATE.
DIMENSIONS: frozenset[str] = frozenset({
    "date", "channel_id", "video_id", "playlist_id",
    "live_or_on_demand", "subscribed_status", "country_code", "province_code",
    "playback_location_type", "playback_location_detail",
    "traffic_source_type", "traffic_source_detail",
    "device_type", "operating_system",
    "age_group", "gender",
    "sharing_service",
    "card_type", "card_id",
    "end_screen_element_type", "end_screen_element_id",
    "subtitle_language", "subtitle_language_autotranslated",
})

METRIC_TYPES: dict[str, str] = {
    # views and engagement (counts)
    "views": "INT64", "engaged_views": "INT64", "red_views": "INT64",
    "comments": "INT64", "likes": "INT64", "dislikes": "INT64", "shares": "INT64",
    "videos_added_to_playlists": "INT64", "videos_removed_from_playlists": "INT64",
    "subscribers_gained": "INT64", "subscribers_lost": "INT64",
    # watch time and duration
    "watch_time_minutes": "FLOAT64", "red_watch_time_minutes": "FLOAT64",
    "average_view_duration_seconds": "FLOAT64", "average_view_duration_percentage": "FLOAT64",
    # annotations (always zero on this channel; carried because channel_basic_a3 emits them)
    "annotation_impressions": "INT64", "annotation_clickable_impressions": "INT64",
    "annotation_closable_impressions": "INT64", "annotation_clicks": "INT64",
    "annotation_closes": "INT64",
    "annotation_click_through_rate": "FLOAT64", "annotation_close_rate": "FLOAT64",
    # cards
    "card_impressions": "INT64", "card_clicks": "INT64",
    "card_teaser_impressions": "INT64", "card_teaser_clicks": "INT64",
    "card_click_rate": "FLOAT64", "card_teaser_click_rate": "FLOAT64",
    # end screens
    "end_screen_element_impressions": "INT64", "end_screen_element_clicks": "INT64",
    "end_screen_element_click_rate": "FLOAT64",
    # reach
    "video_thumbnail_impressions": "INT64", "video_thumbnail_impressions_ctr": "FLOAT64",
    # demographics
    "views_percentage": "FLOAT64",
    # playlists
    "playlist_starts": "INT64", "playlist_saves_added": "INT64", "playlist_saves_removed": "INT64",
}

# Provenance columns appended to every raw table, in this order.
PROVENANCE_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("report_id", "STRING", True),
    ("report_create_time", "TIMESTAMP", True),
    ("job_id", "STRING", True),
    ("load_source", "STRING", True),
    ("ingested_at", "TIMESTAMP", True),
)


@dataclass(frozen=True)
class ReportSpec:
    report_type: str
    dimensions: tuple[str, ...]   # the grain, in CSV order; always starts with "date"
    metrics: tuple[str, ...]

    @property
    def table(self) -> str:
        return TABLE_PREFIX + self.report_type

    @property
    def grain_columns(self) -> tuple[str, ...]:
        """Grain as table column names (date becomes report_date)."""
        return tuple("report_date" if d == "date" else d for d in self.dimensions)

    @property
    def columns(self) -> tuple[str, ...]:
        """All data columns in table order: grain, metrics."""
        return self.grain_columns + self.metrics

    @property
    def csv_columns(self) -> frozenset[str]:
        return frozenset(self.dimensions) | frozenset(self.metrics)

    def column_type(self, column: str) -> str:
        if column == "report_date":
            return "DATE"
        if column in DIMENSIONS:
            return "STRING"
        return METRIC_TYPES[column]


_BASIC = ("date", "channel_id", "video_id", "live_or_on_demand", "subscribed_status", "country_code")
_PLAYLIST = ("date", "channel_id", "playlist_id", "video_id", "live_or_on_demand", "subscribed_status", "country_code")
_ACTIVITY = ("engaged_views", "views", "watch_time_minutes", "average_view_duration_seconds",
             "average_view_duration_percentage", "red_views", "red_watch_time_minutes")
_ANNOTATION_METRICS = ("annotation_click_through_rate", "annotation_close_rate", "annotation_impressions",
                       "annotation_clickable_impressions", "annotation_closable_impressions",
                       "annotation_clicks", "annotation_closes")
_CARD_METRICS = ("card_click_rate", "card_teaser_click_rate", "card_impressions",
                 "card_teaser_impressions", "card_clicks", "card_teaser_clicks")
_PLAYLIST_METRICS = ("engaged_views", "views", "watch_time_minutes", "average_view_duration_seconds",
                     "playlist_starts", "playlist_saves_added", "playlist_saves_removed")


def _spec(report_type: str, dims: tuple[str, ...], metrics: tuple[str, ...]) -> ReportSpec:
    unknown_d = set(dims) - DIMENSIONS
    unknown_m = set(metrics) - set(METRIC_TYPES)
    if unknown_d or unknown_m:
        raise ValueError(f"{report_type}: unregistered columns {sorted(unknown_d | unknown_m)}")
    return ReportSpec(report_type, dims, metrics)


SPECS: dict[str, ReportSpec] = {s.report_type: s for s in [
    _spec("channel_basic_a3", _BASIC,
          ("engaged_views", "views", "comments", "likes", "dislikes", "videos_added_to_playlists",
           "videos_removed_from_playlists", "shares", "watch_time_minutes", "average_view_duration_seconds",
           "average_view_duration_percentage") + _ANNOTATION_METRICS + _CARD_METRICS
          + ("subscribers_gained", "subscribers_lost", "red_views", "red_watch_time_minutes")),
    _spec("channel_province_a3", _BASIC + ("province_code",),
          ("engaged_views", "views", "watch_time_minutes", "average_view_duration_seconds",
           "average_view_duration_percentage") + _ANNOTATION_METRICS + _CARD_METRICS
          + ("red_views", "red_watch_time_minutes")),
    _spec("channel_playback_location_a3", _BASIC + ("playback_location_type", "playback_location_detail"), _ACTIVITY),
    _spec("channel_traffic_source_a3", _BASIC + ("traffic_source_type", "traffic_source_detail"), _ACTIVITY),
    _spec("channel_device_os_a3", _BASIC + ("device_type", "operating_system"), _ACTIVITY),
    _spec("channel_subtitles_a3", _BASIC + ("subtitle_language", "subtitle_language_autotranslated"), _ACTIVITY),
    _spec("channel_combined_a3", _BASIC + ("playback_location_type", "traffic_source_type", "device_type", "operating_system"), _ACTIVITY),
    _spec("channel_demographics_a1", _BASIC + ("age_group", "gender"), ("views_percentage",)),
    _spec("channel_sharing_service_a1", _BASIC + ("sharing_service",), ("shares",)),
    _spec("channel_cards_a1", _BASIC + ("card_type", "card_id"), _CARD_METRICS),
    _spec("channel_end_screens_a1", _BASIC + ("end_screen_element_type", "end_screen_element_id"),
          ("end_screen_element_clicks", "end_screen_element_impressions", "end_screen_element_click_rate")),
    _spec("channel_reach_basic_a1", ("date", "channel_id", "video_id"),
          ("video_thumbnail_impressions", "video_thumbnail_impressions_ctr")),
    _spec("channel_reach_combined_a1",
          ("date", "channel_id", "video_id", "traffic_source_type", "traffic_source_detail", "operating_system", "device_type"),
          ("video_thumbnail_impressions", "video_thumbnail_impressions_ctr")),
    _spec("playlist_basic_a2", _PLAYLIST, _PLAYLIST_METRICS),
    _spec("playlist_province_a2", _PLAYLIST + ("province_code",), _PLAYLIST_METRICS),
    _spec("playlist_playback_location_a2", _PLAYLIST + ("playback_location_type", "playback_location_detail"), _PLAYLIST_METRICS),
    _spec("playlist_traffic_source_a2", _PLAYLIST + ("traffic_source_type", "traffic_source_detail"), _PLAYLIST_METRICS),
    _spec("playlist_device_os_a2", _PLAYLIST + ("device_type", "operating_system"), _PLAYLIST_METRICS),
    _spec("playlist_combined_a2", _PLAYLIST + ("playback_location_type", "traffic_source_type", "device_type", "operating_system"), _PLAYLIST_METRICS),
]}
