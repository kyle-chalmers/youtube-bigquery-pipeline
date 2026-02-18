-- ═══════════════════════════════════════════════════════════════
-- YouTube Analytics Pipeline — Sample Queries
-- Run these in BigQuery Console or via: bq query --use_legacy_sql=false
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. Latest snapshot overview ──────────────────────────────
-- Quick health check: what does today's data look like?
-- Returns a single row with the most recent snapshot date, total video count
-- broken down by shorts vs full-length, and aggregate views/likes across all
-- videos. Use this to verify the pipeline ran and ingested all expected videos.
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
-- Ranks all videos by cumulative view count and returns the top 10 with their
-- engagement stats (likes, comments). Useful for identifying your best-performing
-- content and comparing engagement levels across shorts and full-length videos.
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
-- Shows which videos gained the most views in the last week. Requires 7+ days
-- of snapshots to produce meaningful deltas — before that, views_7d_ago will
-- be 0 and views_gained will equal current_views. Great for spotting content
-- that's picking up momentum from the algorithm or external shares.
WITH latest AS (
    SELECT MAX(snapshot_date) AS max_date
    FROM `youtube_analytics.daily_video_stats`
),
current_stats AS (
    SELECT video_id, view_count
    FROM `youtube_analytics.daily_video_stats`
    WHERE snapshot_date = (SELECT max_date FROM latest)
),
prior_stats AS (
    SELECT video_id, view_count
    FROM `youtube_analytics.daily_video_stats`
    WHERE snapshot_date = DATE_SUB((SELECT max_date FROM latest), INTERVAL 7 DAY)
)
SELECT
    m.title,
    m.video_type,
    c.view_count AS current_views,
    IFNULL(p.view_count, 0) AS views_7d_ago,
    c.view_count - IFNULL(p.view_count, 0) AS views_gained
FROM current_stats c
CROSS JOIN latest
JOIN `youtube_analytics.video_metadata` m
    ON c.video_id = m.video_id
    AND m.snapshot_date = latest.max_date
LEFT JOIN prior_stats p ON c.video_id = p.video_id
ORDER BY views_gained DESC
LIMIT 10;


-- ─── 4. Shorts vs full-length performance comparison ─────────
-- Compares average views, likes, comments, and engagement ratio (views_per_like)
-- between shorts and full-length videos. A lower views_per_like means viewers
-- are more likely to engage — full-length viewers typically like at a much higher
-- rate than shorts viewers, who tend to be passive swipe-through traffic.
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
-- Tracks total cumulative views and likes per snapshot day, plus the day-over-day
-- view delta using a window function. Shows whether the channel is accelerating
-- or plateauing. The daily_view_delta is null for the earliest snapshot since
-- there's no prior day to compare against.
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
-- Breaks down views by traffic source (Shorts feed, YouTube Search, External
-- URLs, Subscribers, etc.) with watch minutes and percentage of total views.
-- Key insight: compare watch minutes per view across sources — search traffic
-- typically delivers the highest engagement per view, while Shorts feed drives
-- volume with lower depth.
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
-- Ranks videos by net subscriber impact (gained minus lost). Reveals which
-- content converts viewers into subscribers. Full-length videos with high watch
-- minutes tend to drive more subscriptions than shorts, making this useful for
-- deciding what content format to invest in for channel growth.
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
-- Ranks videos by total estimated watch minutes, with retention metrics.
-- avg_view_pct shows what fraction of the video viewers watch on average, and
-- avg_view_duration_sec shows the absolute time watched. High watch minutes
-- with high avg_view_pct signals strong content; high watch minutes with low
-- avg_view_pct may indicate a long video carried by volume rather than retention.
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
-- Lists recently published videos sorted by publish date, with current views
-- and likes. Useful for tracking early performance of new uploads and comparing
-- how quickly different content formats gain traction after publishing.
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
