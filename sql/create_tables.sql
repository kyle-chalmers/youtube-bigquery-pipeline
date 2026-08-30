-- YouTube BigQuery Pipeline — Table DDL
-- All tables partitioned by snapshot_date for cost-efficient querying.

-- Video metadata (slowly changing dimension — updated daily)
CREATE TABLE IF NOT EXISTS `youtube_analytics.video_metadata` (
    video_id STRING NOT NULL,
    title STRING,
    published_at TIMESTAMP,
    duration_seconds INT64,
    duration_formatted STRING,
    video_type STRING,
    tags STRING,
    category_id STRING,
    thumbnail_url STRING,
    snapshot_date DATE NOT NULL
)
PARTITION BY snapshot_date
OPTIONS (
    description = 'Video metadata snapshots, updated daily. video_type is full_length or short (<=180s).'
);

-- Daily video stats from Data API v3 (append-only)
CREATE TABLE IF NOT EXISTS `youtube_analytics.daily_video_stats` (
    snapshot_date DATE NOT NULL,
    video_id STRING NOT NULL,
    view_count INT64,
    like_count INT64,
    comment_count INT64,
    favorite_count INT64
)
PARTITION BY snapshot_date
OPTIONS (
    description = 'Daily public stats from YouTube Data API v3'
);

-- Daily video analytics from Analytics API v2
-- activity_date is the day the views happened; snapshot_date is the day the
-- pipeline collected them. They differ by ANALYTICS_LOOKBACK_DAYS on a normal run
-- and by months on a recovered row. Idempotent writes key on activity_date.
CREATE TABLE IF NOT EXISTS `youtube_analytics.daily_video_analytics` (
    activity_date DATE NOT NULL,
    snapshot_date DATE NOT NULL,
    video_id STRING NOT NULL,
    load_source STRING NOT NULL,
    estimated_minutes_watched FLOAT64,
    average_view_duration_seconds FLOAT64,
    average_view_percentage FLOAT64,
    impressions INT64,
    impression_ctr FLOAT64,
    subscribers_gained INT64,
    subscribers_lost INT64,
    shares INT64,
    annotation_click_through_rate FLOAT64,
    card_click_rate FLOAT64
)
PARTITION BY activity_date
CLUSTER BY video_id
OPTIONS (
    description = 'Daily analytics from YouTube Analytics API v2. Partitioned by activity_date (when it happened), not snapshot_date (when collected).'
);

-- Daily traffic sources from Analytics API v2. See daily_video_analytics above
-- for the activity_date / snapshot_date distinction.
CREATE TABLE IF NOT EXISTS `youtube_analytics.daily_traffic_sources` (
    activity_date DATE NOT NULL,
    snapshot_date DATE NOT NULL,
    video_id STRING NOT NULL,
    traffic_source_type STRING NOT NULL,
    load_source STRING NOT NULL,
    views INT64,
    estimated_minutes_watched FLOAT64
)
PARTITION BY activity_date
CLUSTER BY video_id
OPTIONS (
    description = 'Daily traffic source breakdown. Partitioned by activity_date (when it happened), not snapshot_date (when collected).'
);
