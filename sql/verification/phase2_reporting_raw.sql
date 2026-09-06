-- Phase 2 verification: the Reporting API raw tables are what they claim.
--
-- Paste ONE tagged block at a time into the BigQuery console (More > Query settings >
-- Data location = us-central1 for the EXECUTE IMMEDIATE blocks). Plain blocks reference
-- youtube_analytics directly; replace `youtube_analytics.` with `youtube_analytics_staging.`
-- to run them against staging. Every query is read-only and partition-scoped. Expected
-- results are in the comment above each block; a different result means stop and look.
--
-- Source semantics observed in this channel's data (2026-09-05), so nobody writes a QC rule
-- against them: average_view_duration_percentage is 0..100 and EXCEEDS 100 on looped Shorts
-- (up to thousands; it recomputes exactly as avd_seconds / duration); video_thumbnail_impressions_ctr
-- is a 0..1 fraction; likes and dislikes can be negative (net of removals); traffic_source_type
-- is a numeric code (5 search, 7 suggested, 3 browse, 9 external, 24 Shorts feed, 17 notifications,
-- 14/18 playlists...); channel_basic_a3 carries channel-level subscriber rows with NULL video_id
-- that hold about a third of all subscribers gained; basic_a3 and traffic_source_a3 disagree on
-- daily views by up to 11% on days generated at different times.
--
-- Grain reminder: reporting_channel_basic_a3 is one row per (report_date, channel_id,
-- video_id, live_or_on_demand, subscribed_status, country_code). Sum over the dimensions
-- to get a video-day; never average a ratio column. report_date is a Pacific-time day,
-- the same convention as daily_traffic_sources.activity_date.

-- ---------------------------------------------------------------------------
-- --grain_unique / --one_report_per_day / --ledger_matches_table
-- These three structural checks cover ALL 19 raw tables and live in the generated file
-- sql/verification/reporting_structural_checks.sql (tags --grain_unique_all,
-- --one_report_per_day_all, --ledger_matches_tables_all). They are generated from the
-- same registry as the DDL so a new report type cannot be left unverified.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- --ledger_unique
-- Expected: 0 rows. One ledger row per report_id; one loaded row per (job, day).
-- ---------------------------------------------------------------------------
SELECT 'duplicate_report_id' AS problem, report_id AS key, COUNT(*) AS n
FROM `youtube_analytics.reporting_ingest_ledger` GROUP BY 1,2 HAVING n > 1
UNION ALL
SELECT 'two_loaded_for_one_day', CONCAT(job_id, ' ', CAST(report_date AS STRING)), COUNT(*)
FROM `youtube_analytics.reporting_ingest_ledger` WHERE status = 'loaded' GROUP BY 1,2 HAVING COUNT(*) > 1;

-- ---------------------------------------------------------------------------
-- --coverage_calendar
-- The last 10 days per report type with ledger status and row count. Gaps, superseded
-- days and multi-generation days show up at a glance. Read it; nothing to assert.
-- ---------------------------------------------------------------------------
SELECT report_type, report_date, statuses, loaded_rows, generations
FROM (
  SELECT report_type, report_date,
         STRING_AGG(status ORDER BY report_create_time) AS statuses,
         MAX(IF(status = 'loaded', row_count, NULL)) AS loaded_rows,
         COUNT(*) AS generations,
         ROW_NUMBER() OVER (PARTITION BY report_type ORDER BY report_date DESC) AS rn
  FROM `youtube_analytics.reporting_ingest_ledger`
  WHERE report_date >= DATE_SUB(CURRENT_DATE('America/Los_Angeles'), INTERVAL 70 DAY)
  GROUP BY 1,2
)
WHERE rn <= 10
ORDER BY 1, 2 DESC;

-- ---------------------------------------------------------------------------
-- --latency
-- How long after a report day YouTube generated the report, per type, and how many days
-- have more than one generation. Expected about 2 days (48 h) for the first generation of a
-- day; the backfill reports of a new job carry hundreds of hours because they are generated
-- at job creation.
-- ---------------------------------------------------------------------------
SELECT report_type,
       APPROX_QUANTILES(TIMESTAMP_DIFF(report_create_time, TIMESTAMP(report_date, 'America/Los_Angeles'), HOUR), 4) AS hours_quartiles,
       COUNT(DISTINCT report_date) AS days,
       COUNT(DISTINCT IF(generations > 1, report_date, NULL)) AS days_with_regeneration,
       COUNT(*) AS reports
FROM (SELECT *, COUNT(*) OVER (PARTITION BY job_id, report_date) AS generations
      FROM `youtube_analytics.reporting_ingest_ledger`)
GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- --nulls_by_column
-- Null share per column in the traffic-source table: shows the anonymisation threshold
-- biting on traffic_source_detail (expected roughly half NULL on a channel this size).
-- ---------------------------------------------------------------------------
SELECT COUNT(*) AS rows_total,
       ROUND(COUNTIF(video_id IS NULL) / COUNT(*), 3) AS video_id_null_share,
       ROUND(COUNTIF(traffic_source_detail IS NULL) / COUNT(*), 3) AS detail_null_share,
       ROUND(COUNTIF(country_code = 'ZZ') / COUNT(*), 3) AS country_zz_share
FROM `youtube_analytics.reporting_channel_traffic_source_a3`;

-- ---------------------------------------------------------------------------
-- --reconcile_views_by_video_day
-- Cross-source check, FULL OUTER JOIN on (day, video_id) over the days BOTH sources hold
-- (Reporting retention expires old days; the Analytics table has its own gaps, and a day
-- missing on one side is reported separately as days_only_in_reporting /
-- days_only_in_analytics, not counted as a per-row mismatch). Reporting traffic-source
-- views summed to video-day vs the existing Analytics-API traffic table summed the same
-- way. Analytics rows with zero views are excluded from the unmatched count (the Analytics
-- API returns them for videos with watch time but no views).
--
-- Expected (thresholds set 2026-09-05 from the observed data, then tightened on review):
-- total_views_diff_share within 0.01, row_mismatch_share within 0.03, and
-- days_only_in_reporting = days_only_in_analytics = 0. Observed on first load against
-- production: totals 0.14% apart, 1.7% of shared video-days differ per row, of which 15 rows
-- are the known 2026-08-11 partial day in daily_traffic_sources. A 3% per-row threshold trips
-- on roughly three such partial days. The two sources are different systems: the Analytics
-- table holds a per-video query answered about five days after the day; the Reporting table
-- holds the newest generation of a bulk report YouTube may have corrected later.
-- engaged_views is also compared; on this channel `views` is the column that matches the
-- Analytics table (7.5% of rows differ on engaged_views vs 1.7% on views).
-- ---------------------------------------------------------------------------
WITH rd AS (SELECT DISTINCT report_date AS d FROM `youtube_analytics.reporting_channel_traffic_source_a3`),
     ad AS (SELECT DISTINCT activity_date AS d FROM `youtube_analytics.daily_traffic_sources`),
     shared AS (SELECT d FROM rd JOIN ad USING (d)),
     win AS (SELECT MIN(d) AS lo, MAX(d) AS hi, COUNT(*) AS shared_days FROM shared),
     side_days AS (
       SELECT COUNTIF(ad.d IS NULL) AS days_only_in_reporting, COUNTIF(rd.d IS NULL) AS days_only_in_analytics
       FROM rd FULL OUTER JOIN ad USING (d), win
       WHERE COALESCE(rd.d, ad.d) BETWEEN win.lo AND win.hi
     ),
r AS (
  SELECT report_date AS d, video_id, SUM(views) AS views, SUM(engaged_views) AS engaged_views
  FROM `youtube_analytics.reporting_channel_traffic_source_a3`
  WHERE report_date IN (SELECT d FROM shared) AND video_id IS NOT NULL GROUP BY 1,2
),
a AS (
  SELECT activity_date AS d, video_id, SUM(views) AS views
  FROM `youtube_analytics.daily_traffic_sources`
  WHERE activity_date IN (SELECT d FROM shared) GROUP BY 1,2 HAVING views > 0
),
j AS (
  SELECT COALESCE(r.d, a.d) AS d, COALESCE(r.video_id, a.video_id) AS video_id,
         r.views AS reporting_views, r.engaged_views AS reporting_engaged, a.views AS analytics_views,
         r.d IS NULL OR a.d IS NULL AS unmatched,
         ABS(IFNULL(r.views, 0) - IFNULL(a.views, 0)) > GREATEST(5, 0.05 * IFNULL(a.views, 0)) AS views_off,
         ABS(IFNULL(r.engaged_views, 0) - IFNULL(a.views, 0)) > GREATEST(5, 0.05 * IFNULL(a.views, 0)) AS engaged_off
  FROM r FULL OUTER JOIN a USING (d, video_id)
),
agg AS (
  SELECT COUNT(*) AS joined_rows, COUNTIF(unmatched) AS unmatched_keys,
         SUM(reporting_views) AS reporting_views_total, SUM(analytics_views) AS analytics_views_total,
         COUNTIF(unmatched OR views_off) AS mismatched_rows, COUNTIF(unmatched OR engaged_off) AS mismatched_rows_engaged
  FROM j
),
counts AS (SELECT (SELECT COUNT(*) FROM r) AS reporting_rows, (SELECT COUNT(*) FROM a) AS analytics_rows)
SELECT win.lo AS window_start, win.hi AS window_end, win.shared_days,
       side_days.days_only_in_reporting, side_days.days_only_in_analytics,
       counts.reporting_rows, counts.analytics_rows,
       agg.joined_rows, agg.unmatched_keys, agg.reporting_views_total, agg.analytics_views_total,
       ROUND(ABS(agg.reporting_views_total - agg.analytics_views_total) / agg.analytics_views_total, 4) AS total_views_diff_share,
       ROUND(agg.mismatched_rows / agg.joined_rows, 4) AS row_mismatch_share,
       ROUND(agg.mismatched_rows_engaged / agg.joined_rows, 4) AS row_mismatch_share_engaged
FROM win, side_days, counts, agg;

-- ---------------------------------------------------------------------------
-- --reconcile_views_by_channel_day
-- Same comparison at channel-day grain over the shared window, full outer join. Expected:
-- one_sided is NULL on every row and |diff_views| is a few percent of the day at most. Note
-- the two Reporting reports themselves (basic_a3 vs traffic_source_a3) differ on 12 of 35
-- days by up to 11% because they were generated at different times; treat basic_a3 as the
-- source of truth for views in Phase 3.
-- ---------------------------------------------------------------------------
WITH r AS (SELECT report_date AS d, SUM(views) AS reporting_views, SUM(engaged_views) AS reporting_engaged
           FROM `youtube_analytics.reporting_channel_traffic_source_a3` GROUP BY 1),
     a AS (SELECT activity_date AS d, SUM(views) AS analytics_views FROM `youtube_analytics.daily_traffic_sources` GROUP BY 1),
     win AS (SELECT MIN(d) AS lo, MAX(d) AS hi FROM (SELECT d FROM r JOIN a USING (d)))
SELECT COALESCE(r.d, a.d) AS d, reporting_views, reporting_engaged, analytics_views,
       reporting_views - analytics_views AS diff_views,
       CASE WHEN r.d IS NULL THEN 'analytics_only' WHEN a.d IS NULL THEN 'reporting_only' END AS one_sided
FROM r FULL OUTER JOIN a USING (d), win
WHERE COALESCE(r.d, a.d) BETWEEN win.lo AND win.hi
ORDER BY d DESC LIMIT 60;

-- ---------------------------------------------------------------------------
-- --reconcile_subs
-- Subscribers gained per video-day: Reporting basic_a3 summed over its dimensions vs the
-- Analytics table, over the days BOTH sources hold (Reporting retention expired the June
-- days; those are not mismatches). Full outer join; expected 0 rows off by more than 1.
-- Note the Reporting table also carries channel-level subscriber rows with no video_id,
-- which the Analytics table cannot have; they are excluded here and are real data.
-- ---------------------------------------------------------------------------
WITH shared AS (
  SELECT d FROM (SELECT DISTINCT report_date AS d FROM `youtube_analytics.reporting_channel_basic_a3`)
  JOIN (SELECT DISTINCT activity_date AS d FROM `youtube_analytics.daily_video_analytics`) USING (d)
),
r AS (SELECT report_date AS d, video_id, SUM(subscribers_gained) AS subs
      FROM `youtube_analytics.reporting_channel_basic_a3`
      WHERE video_id IS NOT NULL AND report_date IN (SELECT d FROM shared) GROUP BY 1,2),
a AS (SELECT activity_date AS d, video_id, SUM(subscribers_gained) AS subs
      FROM `youtube_analytics.daily_video_analytics` WHERE activity_date IN (SELECT d FROM shared) GROUP BY 1,2)
SELECT COALESCE(r.d, a.d) AS d, COALESCE(r.video_id, a.video_id) AS video_id, r.subs AS reporting_subs, a.subs AS analytics_subs
FROM r FULL OUTER JOIN a USING (d, video_id)
WHERE ABS(IFNULL(r.subs, 0) - IFNULL(a.subs, 0)) > 1
ORDER BY 1 DESC, 2 LIMIT 50;

-- ---------------------------------------------------------------------------
-- --analytics_table_gaps_revealed
-- Days the Reporting traffic report has but the old Analytics traffic table lacks, inside
-- the Reporting window. These are holes in daily_traffic_sources (traffic gaps did not
-- self-heal before Phase 4). Expected: read it; on first load it showed 2026-08-11.
-- ---------------------------------------------------------------------------
SELECT r.d AS missing_in_daily_traffic_sources, COUNT(DISTINCT r.video_id) AS videos, SUM(r.views) AS views
FROM (SELECT report_date AS d, video_id, SUM(views) AS views FROM `youtube_analytics.reporting_channel_traffic_source_a3` GROUP BY 1,2) r
LEFT JOIN (SELECT DISTINCT activity_date AS d FROM `youtube_analytics.daily_traffic_sources`) a USING (d)
WHERE a.d IS NULL AND r.d < DATE_SUB(CURRENT_DATE('America/Los_Angeles'), INTERVAL 5 DAY)
GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- --analytics_partial_days
-- Days where the old Analytics traffic table exists but is missing one or more videos that
-- the Reporting traffic report shows with views (floor: 1 video; a handful of single-video
-- days with 1 to 3 views are normal generation-timing noise, 15 videos is a partial day). This is the partial-day failure: a per-video
-- Analytics API call failed, the day was written anyway, and date-level gap detection saw
-- it as complete. Expected: read it; on first load 2026-08-11 showed 15 videos / 173 views
-- missing. Phase 4 item 1 (incomplete-day ledger) closes the cause; the rows can be
-- recovered with setup/backfill_analytics.py --start 2026-08-11 --end 2026-08-11.
-- ---------------------------------------------------------------------------
WITH shared AS (
  SELECT d FROM (SELECT DISTINCT report_date AS d FROM `youtube_analytics.reporting_channel_traffic_source_a3`)
  JOIN (SELECT DISTINCT activity_date AS d FROM `youtube_analytics.daily_traffic_sources`) USING (d)
),
r AS (SELECT report_date AS d, video_id, SUM(views) AS views FROM `youtube_analytics.reporting_channel_traffic_source_a3`
      WHERE video_id IS NOT NULL AND report_date IN (SELECT d FROM shared) GROUP BY 1,2 HAVING views > 0),
a AS (SELECT DISTINCT activity_date AS d, video_id FROM `youtube_analytics.daily_traffic_sources` WHERE activity_date IN (SELECT d FROM shared))
SELECT r.d AS activity_date, COUNT(*) AS videos_missing_in_daily_traffic_sources, SUM(r.views) AS views_missing
FROM r LEFT JOIN a USING (d, video_id)
WHERE a.video_id IS NULL
GROUP BY 1 HAVING COUNT(*) >= 1
ORDER BY views_missing DESC;

-- ---------------------------------------------------------------------------
-- --prod_untouched
-- The four original tables, totals and whole-table fingerprints. Capture before the
-- Reporting function is promoted and after its first runs; must be identical apart from
-- the partitions the daily pipeline itself keeps writing.
-- ---------------------------------------------------------------------------
SELECT 'video_metadata' AS table_name, COUNT(*) AS rows_total, MAX(snapshot_date) AS latest,
       BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) AS fingerprint FROM `youtube_analytics.video_metadata` t
UNION ALL SELECT 'daily_video_stats', COUNT(*), MAX(snapshot_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `youtube_analytics.daily_video_stats` t
UNION ALL SELECT 'daily_video_analytics', COUNT(*), MAX(activity_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `youtube_analytics.daily_video_analytics` t
UNION ALL SELECT 'daily_traffic_sources', COUNT(*), MAX(activity_date), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(t))) FROM `youtube_analytics.daily_traffic_sources` t
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- --what_changed
-- Partitions written to the Reporting tables in the last N days, by load_source, so
-- before every promotion you can see exactly which days moved and why.
-- ---------------------------------------------------------------------------
SELECT report_type, load_source, status, MIN(report_date) AS first_day, MAX(report_date) AS last_day,
       COUNT(*) AS reports, SUM(row_count) AS rows_loaded
FROM `youtube_analytics.reporting_ingest_ledger`
WHERE ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY 1,2,3 ORDER BY 1,2,3;
