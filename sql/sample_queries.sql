-- ═══════════════════════════════════════════════════════════════
-- YouTube Analytics Pipeline — Sample Queries
-- Run these in BigQuery Console or via: bq query --use_legacy_sql=false
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. Latest snapshot overview ──────────────────────────────
-- Quick health check: what does today's data look like?
SELECT
    m.snapshot_date,
    COUNT(*) AS total_videos,
    COUNTIF(m.video_type = 'short') AS shorts,
    COUNTIF(m.video_type = 'full_length') AS full_length,
    SUM(s.view_count) AS total_views,
    SUM(s.like_count) AS total_likes
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s
    USING (video_id, snapshot_date)
WHERE m.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`
)
GROUP BY m.snapshot_date;


-- ─── 2. Top 10 videos by views (latest snapshot) ─────────────
SELECT
    m.title,
    m.video_type,
    m.duration_formatted,
    s.view_count,
    s.like_count,
    s.comment_count
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s
    USING (video_id, snapshot_date)
WHERE m.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`
)
ORDER BY s.view_count DESC
LIMIT 10;


-- ─── 3. Daily view growth per video (7-day delta) ────────────
-- Compare today's views to 7 days ago to find trending content.
WITH latest AS (
    SELECT MAX(snapshot_date) AS max_date
    FROM `youtube_analytics.daily_video_stats`
),
current_stats AS (
    SELECT video_id, view_count
    FROM `youtube_analytics.daily_video_stats`, latest
    WHERE snapshot_date = latest.max_date
),
prior_stats AS (
    SELECT video_id, view_count
    FROM `youtube_analytics.daily_video_stats`, latest
    WHERE snapshot_date = DATE_SUB(latest.max_date, INTERVAL 7 DAY)
)
SELECT
    m.title,
    m.video_type,
    c.view_count AS current_views,
    IFNULL(p.view_count, 0) AS views_7d_ago,
    c.view_count - IFNULL(p.view_count, 0) AS views_gained,
FROM current_stats c
JOIN `youtube_analytics.video_metadata` m
    ON c.video_id = m.video_id
    AND m.snapshot_date = (SELECT max_date FROM latest)
LEFT JOIN prior_stats p ON c.video_id = p.video_id
ORDER BY views_gained DESC
LIMIT 10;


-- ─── 4. Shorts vs full-length performance comparison ─────────
SELECT
    m.video_type,
    COUNT(*) AS video_count,
    ROUND(AVG(s.view_count), 0) AS avg_views,
    ROUND(AVG(s.like_count), 0) AS avg_likes,
    ROUND(AVG(s.comment_count), 1) AS avg_comments,
    ROUND(AVG(s.view_count) / NULLIF(AVG(s.like_count), 0), 1) AS views_per_like
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s
    USING (video_id, snapshot_date)
WHERE m.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`
)
GROUP BY m.video_type;


-- ─── 5. Week-over-week channel growth ────────────────────────
-- Requires at least 2 weeks of snapshots.
SELECT
    snapshot_date,
    SUM(view_count) AS total_views,
    SUM(like_count) AS total_likes,
    SUM(view_count) - LAG(SUM(view_count)) OVER (ORDER BY snapshot_date) AS daily_view_delta
FROM `youtube_analytics.daily_video_stats`
GROUP BY snapshot_date
ORDER BY snapshot_date DESC
LIMIT 14;


-- ─── 6. Traffic source breakdown (requires Analytics API) ────
-- Shows where views come from across the entire channel.
SELECT
    traffic_source_type,
    SUM(views) AS total_views,
    ROUND(SUM(estimated_minutes_watched), 1) AS total_watch_minutes,
    ROUND(100.0 * SUM(views) / SUM(SUM(views)) OVER (), 1) AS pct_of_views
FROM `youtube_analytics.daily_traffic_sources`
WHERE snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.daily_traffic_sources`
)
GROUP BY traffic_source_type
ORDER BY total_views DESC;


-- ─── 7. Subscriber impact by video (requires Analytics API) ──
-- Which videos are driving subscriber growth?
SELECT
    m.title,
    m.video_type,
    a.subscribers_gained,
    a.subscribers_lost,
    a.subscribers_gained - a.subscribers_lost AS net_subscribers,
    ROUND(a.estimated_minutes_watched, 1) AS watch_minutes
FROM `youtube_analytics.daily_video_analytics` a
JOIN `youtube_analytics.video_metadata` m
    USING (video_id)
WHERE a.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.daily_video_analytics`
)
AND m.snapshot_date = a.snapshot_date
ORDER BY net_subscribers DESC
LIMIT 10;


-- ─── 8. Watch time leaders (requires Analytics API) ──────────
SELECT
    m.title,
    m.video_type,
    m.duration_formatted,
    ROUND(a.estimated_minutes_watched, 1) AS watch_minutes,
    ROUND(a.average_view_percentage, 1) AS avg_view_pct,
    ROUND(a.average_view_duration_seconds, 0) AS avg_view_duration_sec
FROM `youtube_analytics.daily_video_analytics` a
JOIN `youtube_analytics.video_metadata` m
    USING (video_id)
WHERE a.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.daily_video_analytics`
)
AND m.snapshot_date = a.snapshot_date
ORDER BY a.estimated_minutes_watched DESC
LIMIT 10;


-- ─── 9. New videos published in the last 30 days ─────────────
SELECT
    m.title,
    m.video_type,
    m.published_at,
    m.duration_formatted,
    s.view_count,
    s.like_count
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s
    USING (video_id, snapshot_date)
WHERE m.snapshot_date = (
    SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`
)
AND m.published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
ORDER BY m.published_at DESC;
