-- ═══════════════════════════════════════════════════════════════
-- IMPORTANT, changed 2026-08-29.
-- daily_video_analytics and daily_traffic_sources are now keyed on activity_date
-- (the day the views happened). snapshot_date still exists but means "the day we
-- collected it", so counting DISTINCT snapshot_date on those two tables no longer
-- answers "how many days do we have". Recovered rows all share one snapshot_date.
-- video_metadata and daily_video_stats are unchanged: they are true daily
-- snapshots and remain keyed on snapshot_date.
-- ═══════════════════════════════════════════════════════════════

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
--finding_1_coverage
SELECT
    'daily_video_analytics' AS table_name,
    MIN(activity_date) AS earliest_date,
    MAX(activity_date) AS latest_date,
    COUNT(DISTINCT activity_date) AS days_with_data,
    DATE_DIFF(MAX(activity_date), MIN(activity_date), DAY) + 1 AS expected_days,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.daily_video_analytics`
UNION ALL
SELECT
    'daily_traffic_sources' AS table_name,
    MIN(activity_date) AS earliest_date,
    MAX(activity_date) AS latest_date,
    COUNT(DISTINCT activity_date) AS days_with_data,
    DATE_DIFF(MAX(activity_date), MIN(activity_date), DAY) + 1 AS expected_days,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT video_id) AS distinct_videos
FROM `youtube_analytics.daily_traffic_sources`;


-- ─── 2. Daily row counts — spot gaps and anomalies ────────────
-- Shows row counts per day for each analytics table side by side. Days with
-- significantly fewer rows than their neighbors may indicate partial failures
-- during backfill. Missing dates in the sequence indicate complete gaps.
--finding_3_daily
SELECT
    COALESCE(a.activity_date, t.activity_date) AS activity_date,
    a.analytics_rows,
    a.analytics_videos,
    t.traffic_rows,
    t.traffic_videos
FROM (
    SELECT
        activity_date,
        COUNT(*) AS analytics_rows,
        COUNT(DISTINCT video_id) AS analytics_videos
    FROM `youtube_analytics.daily_video_analytics`
    GROUP BY activity_date
) a
FULL OUTER JOIN (
    SELECT
        activity_date,
        COUNT(*) AS traffic_rows,
        COUNT(DISTINCT video_id) AS traffic_videos
    FROM `youtube_analytics.daily_traffic_sources`
    GROUP BY activity_date
) t USING (activity_date)
ORDER BY activity_date;


-- ─── 3. Data API tables coverage ──────────────────────────────
-- Verifies the Data API tables (video_metadata, daily_video_stats) have
-- snapshots for each expected day. These tables only capture cumulative totals
-- at the time of each pipeline run, so they won't have historical data from
-- before the pipeline was first deployed.
--finding_3_snapshots
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
--finding_2_join
-- Matches the shipped sample queries: the last metadata row we ever saw PER VIDEO,
-- not the latest snapshot. A video that left the channel keeps the watch time it
-- legitimately earned. Only videos that never appeared in metadata at all remain
-- unjoinable, and those are genuinely unrecoverable.
WITH latest_metadata AS (
    SELECT DISTINCT video_id FROM `youtube_analytics.video_metadata`
)
SELECT
    'in_analytics_not_metadata' AS issue,
    a.video_id,
    a.activity_date,
    a.estimated_minutes_watched AS watch_minutes
FROM `youtube_analytics.daily_video_analytics` a
LEFT JOIN latest_metadata m USING (video_id)
WHERE m.video_id IS NULL
ORDER BY a.activity_date DESC
LIMIT 20;


-- ═══════════════════════════════════════════════════════════════
-- Backfill Insight Queries (daily_video_analytics + daily_traffic_sources)
-- Run these after the backfill completes to surface peak performance days.
-- ═══════════════════════════════════════════════════════════════

-- ─── 5. Best days for subscriber growth ─────────────────────────
-- Aggregates net subscribers gained across all videos per ACTIVITY day.
-- Grouped on activity_date, not snapshot_date: since the 2026-08-29 cutover
-- snapshot_date is only the collection stamp, and every recovered row shares
-- one, so grouping by it silently collapses 313 real days into 308. Surfaces the
-- days where the channel grew fastest — useful for correlating with uploads,
-- external mentions, or algorithm boosts.
--analysis_subscribers
SELECT
    activity_date,
    SUM(subscribers_gained) AS total_gained,
    SUM(subscribers_lost) AS total_lost,
    SUM(subscribers_gained) - SUM(subscribers_lost) AS net_subscribers
FROM `youtube_analytics.daily_video_analytics`
GROUP BY activity_date
ORDER BY net_subscribers DESC
LIMIT 10;


-- ─── 6. Best days for views (Analytics API) ────────────────────
-- Sums views across all traffic sources per day. Unlike the Data API (which
-- only stores cumulative totals), these are actual per-day view counts from
-- the Analytics API — the true measure of daily viewership.
--analysis_views
SELECT
    activity_date,
    SUM(views) AS total_views,
    ROUND(SUM(estimated_minutes_watched), 1) AS total_watch_minutes,
    ROUND(SUM(estimated_minutes_watched) / NULLIF(SUM(views), 0), 2) AS avg_minutes_per_view
FROM `youtube_analytics.daily_traffic_sources`
GROUP BY activity_date
ORDER BY total_views DESC
LIMIT 10;


-- ─── 7. Best days for total traffic (watch minutes) ────────────
-- Ranks days by total estimated watch minutes across all sources. Watch time
-- is YouTube's primary ranking signal, so days with high watch minutes indicate
-- strong algorithmic performance or viral moments.
--analysis_watchtime
SELECT
    activity_date,
    ROUND(SUM(estimated_minutes_watched), 1) AS total_watch_minutes,
    SUM(views) AS total_views,
    ROUND(SUM(estimated_minutes_watched) / NULLIF(SUM(views), 0), 2) AS avg_minutes_per_view
FROM `youtube_analytics.daily_traffic_sources`
GROUP BY activity_date
ORDER BY total_watch_minutes DESC
LIMIT 10;


-- ─── 8. Best day per traffic source ────────────────────────────
-- For each traffic source type, finds the single day with the most views.
-- Reveals peak performance per discovery channel — e.g., when did the Shorts
-- feed drive the most views? When did YouTube Search peak? Helpful for
-- understanding which sources have spiked and when.
--analysis_sources
SELECT
    traffic_source_type,
    activity_date AS peak_date,
    views AS peak_views,
    ROUND(estimated_minutes_watched, 1) AS peak_watch_minutes
FROM (
    SELECT
        traffic_source_type,
        activity_date,
        SUM(views) AS views,
        SUM(estimated_minutes_watched) AS estimated_minutes_watched,
        ROW_NUMBER() OVER (
            PARTITION BY traffic_source_type
            ORDER BY SUM(views) DESC
        ) AS rn
    FROM `youtube_analytics.daily_traffic_sources`
    GROUP BY traffic_source_type, activity_date
)
WHERE rn = 1
ORDER BY peak_views DESC;


-- ═══════════════════════════════════════════════════════════════
-- POST-MIGRATION CHECKS (added 2026-08-29, after the activity_date cutover)
-- Run these together after any pipeline run. Each one restates a defect the
-- audit found and shows whether it has come back.
-- ═══════════════════════════════════════════════════════════════

-- ─── 9. Gap list: which activity days are missing ──────────────
-- The single most important check.
-- The calendar stops 6 days back, one day of slack beyond the 5-day
-- ANALYTICS_LOOKBACK_DAYS, because anything newer has not been collected yet and
-- would read as a false alarm. Raise this number if the lookback is raised.
-- CURRENT_DATE is pinned to America/Phoenix on purpose. Bare CURRENT_DATE() is
-- UTC, which after 5pm Phoenix rolls to tomorrow and reports a day the pipeline
-- was never supposed to have yet. That is the same UTC-naive mistake the pipeline
-- itself had in date.today(), and it produced a phantom gap the first time this
-- query was run.
-- EXPECT: only 2025-10-22 and 2025-10-23, both genuine zero-activity days.
--finding_3_gaps
WITH cal AS (
    SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(
        '2025-10-16',
        DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 6 DAY))) AS d
),
a AS (SELECT DISTINCT activity_date FROM `youtube_analytics.daily_video_analytics`),
t AS (SELECT DISTINCT activity_date FROM `youtube_analytics.daily_traffic_sources`)
SELECT
    cal.d AS missing_activity_date,
    a.activity_date IS NULL AS no_analytics,
    t.activity_date IS NULL AS no_traffic,
    CASE WHEN cal.d IN ('2025-10-22', '2025-10-23')
         THEN 'EXPECTED: zero-activity day'
         ELSE '*** INVESTIGATE ***' END AS verdict
FROM cal
LEFT JOIN a ON a.activity_date = cal.d
LEFT JOIN t ON t.activity_date = cal.d
WHERE a.activity_date IS NULL OR t.activity_date IS NULL
ORDER BY cal.d;


-- ─── 10. Duplicate collisions ──────────────────────────────────
-- The defect that started the audit: the same activity day stored twice under
-- two snapshot_dates. Filtered and unfiltered counts sit side by side so the
-- filter is visible in the result, not hidden in a comment.
-- EXPECT: zero rows.
--finding_1_duplicates
SELECT
    'daily_video_analytics' AS table_name,
    a.activity_date,
    COUNT(*) AS collisions_all,
    COUNTIF(a.estimated_minutes_watched > 0) AS collisions_nonzero_watch,
    COUNTIF(a.estimated_minutes_watched = 0) AS collisions_zero_watch,
    COUNT(DISTINCT a.video_id) AS videos_affected
FROM `youtube_analytics.daily_video_analytics` a
JOIN `youtube_analytics.daily_video_analytics` b
    ON  b.video_id = a.video_id
    AND b.activity_date = a.activity_date
    AND b.snapshot_date <> a.snapshot_date
GROUP BY 1, 2
UNION ALL
SELECT
    'daily_traffic_sources',
    a.activity_date,
    COUNT(*),
    COUNTIF(a.estimated_minutes_watched > 0),
    COUNTIF(a.estimated_minutes_watched = 0),
    COUNT(DISTINCT a.video_id)
FROM `youtube_analytics.daily_traffic_sources` a
JOIN `youtube_analytics.daily_traffic_sources` b
    ON  b.video_id = a.video_id
    AND b.activity_date = a.activity_date
    AND b.traffic_source_type = a.traffic_source_type
    AND b.snapshot_date <> a.snapshot_date
GROUP BY 1, 2
ORDER BY 1, 2;


-- ─── 11. Provenance breakdown ──────────────────────────────────
-- Which process wrote each row. Recovered and gap-repaired rows share a
-- snapshot_date with ordinary cron rows, so load_source is the only reliable
-- way to tell them apart. A run that erases recovery rows shows up here first.
-- EXPECT: recovery_20260829 holds 262 analytics rows and does not shrink.
--finding_6_provenance
SELECT
    load_source,
    COUNT(*) AS total_rows,
    COUNT(DISTINCT activity_date) AS activity_days,
    MIN(activity_date) AS first_day,
    MAX(activity_date) AS last_day
FROM `youtube_analytics.daily_video_analytics`
GROUP BY load_source
ORDER BY load_source;


-- ─── 12. Lookback health ───────────────────────────────────────
-- The root cause of the missing days: the pipeline used to query exactly 3 days
-- back, which is the precise edge of Analytics API availability, so a one-day
-- slip returned nothing. Lookback is now 5.
-- EXPECT: days_behind_today around 5, and never 0 to 2.
--finding_3a_lookback
SELECT
    MAX(activity_date) AS newest_activity_day,
    DATE_DIFF(CURRENT_DATE('America/Phoenix'), MAX(activity_date), DAY) AS days_behind_today,
    CASE WHEN DATE_DIFF(CURRENT_DATE('America/Phoenix'), MAX(activity_date), DAY) <= 2
         THEN '*** TOO CLOSE TO THE EDGE ***'
         ELSE 'healthy margin' END AS verdict
FROM `youtube_analytics.daily_video_analytics`;


-- ─── 13. Conservation against the pre-migration backup ─────────
-- Proves the migration neither invented nor lost rows. The archive tables are
-- the originals, renamed. Differences must be explained entirely by the 63 and
-- 174 deleted duplicates and the 262 and 327 recovered rows.
--migration_conservation
SELECT
    'analytics' AS table_name,
    (SELECT COUNT(*) FROM `youtube_analytics.daily_video_analytics_v1_archive`) AS archive_rows,
    (SELECT COUNT(*) FROM `youtube_analytics.daily_video_analytics`) AS live_rows,
    (SELECT COUNT(*) FROM `youtube_analytics.daily_video_analytics` WHERE load_source = 'recovery_20260829') AS recovered,
    (SELECT COUNT(*) FROM `youtube_analytics.daily_video_analytics` WHERE load_source = 'gap_repair') AS gap_repaired
UNION ALL
SELECT
    'traffic',
    (SELECT COUNT(*) FROM `youtube_analytics.daily_traffic_sources_v1_archive`),
    (SELECT COUNT(*) FROM `youtube_analytics.daily_traffic_sources`),
    (SELECT COUNT(*) FROM `youtube_analytics.daily_traffic_sources` WHERE load_source = 'recovery_20260829'),
    (SELECT COUNT(*) FROM `youtube_analytics.daily_traffic_sources` WHERE load_source = 'gap_repair');


-- ─── 14. Schema guardrails ─────────────────────────────────────
-- The first migration attempt silently dropped every NOT NULL constraint,
-- because CREATE TABLE AS SELECT does not carry them across.
-- EXPECT: four NOT NULL columns on analytics, five on traffic.
--migration_schema
SELECT table_name, column_name, data_type, is_nullable
FROM `youtube_analytics.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ('daily_video_analytics', 'daily_traffic_sources')
  AND is_nullable = 'NO'
ORDER BY table_name, ordinal_position;
