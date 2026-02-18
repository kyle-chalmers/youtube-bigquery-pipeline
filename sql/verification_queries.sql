-- ═══════════════════════════════════════════════════════════════
-- YouTube Analytics Pipeline — Verification Queries
-- Run these after backfill or daily pipeline execution to confirm
-- data integrity. Use: bq query --use_legacy_sql=false
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. Analytics API coverage summary ────────────────────────
-- Confirms how many days of historical analytics data exist per table.
-- Use after running the backfill script to verify data was ingested for each
-- expected day. Compare days_with_data to expected_days — if they don't match,
-- some days are missing. The row/video counts per day help spot anomalies
-- (e.g., a sudden drop in rows may mean API errors during backfill).
SELECT
    'daily_video_analytics' AS table_name,
    MIN(snapshot_date) AS earliest_date,
    MAX(snapshot_date) AS latest_date,
    COUNT(DISTINCT snapshot_date) AS days_with_data,
    DATE_DIFF(MAX(snapshot_date), MIN(snapshot_date), DAY) + 1 AS expected_days,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.daily_video_analytics`
UNION ALL
SELECT
    'daily_traffic_sources' AS table_name,
    MIN(snapshot_date) AS earliest_date,
    MAX(snapshot_date) AS latest_date,
    COUNT(DISTINCT snapshot_date) AS days_with_data,
    DATE_DIFF(MAX(snapshot_date), MIN(snapshot_date), DAY) + 1 AS expected_days,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.daily_traffic_sources`;


-- ─── 2. Daily row counts — spot gaps and anomalies ────────────
-- Shows row counts per day for each analytics table side by side. Days with
-- significantly fewer rows than their neighbors may indicate partial failures
-- during backfill. Missing dates in the sequence indicate complete gaps.
SELECT
    COALESCE(a.snapshot_date, t.snapshot_date) AS snapshot_date,
    a.analytics_rows,
    a.analytics_videos,
    t.traffic_rows,
    t.traffic_videos
FROM (
    SELECT
        snapshot_date,
        COUNT(*) AS analytics_rows,
        COUNT(DISTINCT video_id) AS analytics_videos
    FROM `youtube_analytics.daily_video_analytics`
    GROUP BY snapshot_date
) a
FULL OUTER JOIN (
    SELECT
        snapshot_date,
        COUNT(*) AS traffic_rows,
        COUNT(DISTINCT video_id) AS traffic_videos
    FROM `youtube_analytics.daily_traffic_sources`
    GROUP BY snapshot_date
) t USING (snapshot_date)
ORDER BY snapshot_date;


-- ─── 3. Data API tables coverage ──────────────────────────────
-- Verifies the Data API tables (video_metadata, daily_video_stats) have
-- snapshots for each expected day. These tables only capture cumulative totals
-- at the time of each pipeline run, so they won't have historical data from
-- before the pipeline was first deployed.
SELECT
    'video_metadata' AS table_name,
    MIN(snapshot_date) AS earliest_date,
    MAX(snapshot_date) AS latest_date,
    COUNT(DISTINCT snapshot_date) AS days_with_data,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.video_metadata`
UNION ALL
SELECT
    'daily_video_stats' AS table_name,
    MIN(snapshot_date) AS earliest_date,
    MAX(snapshot_date) AS latest_date,
    COUNT(DISTINCT snapshot_date) AS days_with_data,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.daily_video_stats`;


-- ─── 4. Cross-table consistency check ─────────────────────────
-- Finds videos that appear in the analytics tables but are missing from
-- video_metadata (or vice versa). Mismatches may indicate videos that were
-- deleted/privated after being captured, or gaps in the Data API ingestion.
SELECT
    'in_analytics_not_metadata' AS issue,
    a.video_id,
    a.snapshot_date,
    a.estimated_minutes_watched AS watch_minutes
FROM `youtube_analytics.daily_video_analytics` a
LEFT JOIN `youtube_analytics.video_metadata` m
    ON a.video_id = m.video_id
    AND a.snapshot_date = m.snapshot_date
WHERE m.video_id IS NULL
ORDER BY a.snapshot_date DESC
LIMIT 20;
