-- What exists in production after the 2026-09-06 promotion, and what changed in the tables
-- that existed before. Paste ONE tagged block at a time into the BigQuery console. Every
-- query is read-only. Written against `youtube_analytics`; replace with
-- `youtube_analytics_staging.` to inspect staging.

-- ---------------------------------------------------------------------------
-- --objects
-- Every table, view and snapshot in the dataset with its type, creation time and row count.
-- Expected: 4 original tables, 4 snapshots (expire 2026-09-13), 19 reporting_* tables,
-- reporting_ingest_ledger, 12 views (and any _v1_archive tables from earlier work).
-- ---------------------------------------------------------------------------
SELECT t.table_name, t.table_type, DATE(t.creation_time) AS created,
       s.row_count, ROUND(s.size_bytes / 1e6, 2) AS mb
FROM `youtube_analytics.INFORMATION_SCHEMA.TABLES` t
LEFT JOIN `youtube_analytics.__TABLES__` s ON s.table_id = t.table_name
ORDER BY t.table_type, t.table_name;

-- ---------------------------------------------------------------------------
-- --snapshots
-- The pre-promotion snapshots of the four original tables and when they expire.
-- Expected: 4 rows, expiration 7 days after 2026-09-06.
-- ---------------------------------------------------------------------------
SELECT table_name, option_value AS expires
FROM `youtube_analytics.INFORMATION_SCHEMA.TABLE_OPTIONS`
WHERE option_name = 'expiration_timestamp' AND table_name LIKE '%_snapshot_%'
ORDER BY table_name;

-- ---------------------------------------------------------------------------
-- --reporting_coverage
-- One row per Reporting report type: days loaded, first and newest day, rows in the table,
-- and anything failed or in conflict. Expected: 19 types, failed = 0, conflicts = 0,
-- table_rows = ledger_rows for every type.
-- ---------------------------------------------------------------------------
WITH ledger AS (
  SELECT report_type,
         COUNTIF(status = 'loaded') AS days_loaded,
         COUNTIF(status = 'header_only') AS days_empty,
         COUNTIF(status = 'failed') AS failed,
         COUNTIF(status = 'header_only_conflict') AS conflicts,
         MIN(IF(status IN ('loaded', 'header_only'), report_date, NULL)) AS first_day,
         MAX(IF(status IN ('loaded', 'header_only'), report_date, NULL)) AS newest_day,
         SUM(IF(status = 'loaded', row_count, 0)) AS ledger_rows
  FROM `youtube_analytics.reporting_ingest_ledger` GROUP BY 1
),
tables AS (
  SELECT REGEXP_REPLACE(table_id, '^reporting_', '') AS report_type, row_count AS table_rows
  FROM `youtube_analytics.__TABLES__` WHERE table_id LIKE 'reporting_%' AND table_id != 'reporting_ingest_ledger'
)
SELECT t.report_type, l.days_loaded, l.days_empty, l.first_day, l.newest_day,
       t.table_rows, l.ledger_rows, t.table_rows - IFNULL(l.ledger_rows, 0) AS rows_diff,
       l.failed, l.conflicts
FROM tables t LEFT JOIN ledger l USING (report_type)
ORDER BY t.report_type;

-- ---------------------------------------------------------------------------
-- --ledger_problems
-- Any ledger row that needs a human. Expected: 0 rows.
-- ---------------------------------------------------------------------------
SELECT report_type, report_date, status, error, ingested_at
FROM `youtube_analytics.reporting_ingest_ledger`
WHERE status IN ('failed', 'header_only_conflict')
ORDER BY ingested_at DESC;

-- ---------------------------------------------------------------------------
-- --views_populated
-- Row count and newest day in each growth view. Expected: every view with a populated source
-- has rows; the 16 report types created 2026-09-05 have data from 2026-08-06.
-- ---------------------------------------------------------------------------
SELECT 'video_current' AS v, COUNT(*) AS rows_n, NULL AS newest FROM `youtube_analytics.video_current`
UNION ALL SELECT 'traffic_source_type_lookup', COUNT(*), NULL FROM `youtube_analytics.traffic_source_type_lookup`
UNION ALL SELECT 'video_daily_funnel', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_daily_funnel`
UNION ALL SELECT 'video_audience_growth', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_audience_growth`
UNION ALL SELECT 'video_traffic_detail_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_traffic_detail_daily`
UNION ALL SELECT 'video_ctr_by_surface_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_ctr_by_surface_daily`
UNION ALL SELECT 'channel_device_mix_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.channel_device_mix_daily`
UNION ALL SELECT 'video_end_screen_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_end_screen_daily`
UNION ALL SELECT 'video_cards_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.video_cards_daily`
UNION ALL SELECT 'channel_sharing_daily', COUNT(*), MAX(report_date) FROM `youtube_analytics.channel_sharing_daily`
UNION ALL SELECT 'channel_demographics', COUNT(*), MAX(report_date) FROM `youtube_analytics.channel_demographics`
UNION ALL SELECT 'channel_daily_summary', COUNT(*), MAX(report_date) FROM `youtube_analytics.channel_daily_summary`
ORDER BY v;

-- ---------------------------------------------------------------------------
-- --original_tables_recent_writes
-- The four original tables, last 14 days, by partition and provenance. This is where the
-- session's writes to existing tables show: 2026-08-11 (recovery_20260905, re-fetched from
-- the API), 2026-08-31 traffic (recovery_20260906, copied from staging), snapshot 2026-09-05
-- (copied from staging; metadata and stats carry no load_source), and 2026-09-06 onward from
-- the refactored function at 00:10. Expected: one row per day per table with no missing day.
-- ---------------------------------------------------------------------------
SELECT 'video_metadata' AS t, snapshot_date AS day, NULL AS load_source, COUNT(*) AS rows_n
FROM `youtube_analytics.video_metadata` WHERE snapshot_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 14 DAY) GROUP BY 1, 2
UNION ALL SELECT 'daily_video_stats', snapshot_date, NULL, COUNT(*)
FROM `youtube_analytics.daily_video_stats` WHERE snapshot_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 14 DAY) GROUP BY 1, 2
UNION ALL SELECT 'daily_video_analytics', activity_date, load_source, COUNT(*)
FROM `youtube_analytics.daily_video_analytics` WHERE activity_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 20 DAY) GROUP BY 1, 2, 3
UNION ALL SELECT 'daily_traffic_sources', activity_date, load_source, COUNT(*)
FROM `youtube_analytics.daily_traffic_sources` WHERE activity_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 20 DAY) GROUP BY 1, 2, 3
ORDER BY t, day DESC;

-- ---------------------------------------------------------------------------
-- --original_tables_touched_this_session
-- Every partition in the two activity tables whose provenance is not the nightly cron,
-- with row counts, so the session's recoveries are visible in one place.
-- Expected: recovery_20260829 (7 days), recovery_20260905 (2026-08-11), recovery_20260906
-- (2026-08-31 traffic), gap_repair for anything the self-healing filled, backfill_* history.
-- ---------------------------------------------------------------------------
SELECT 'daily_video_analytics' AS t, load_source, COUNT(DISTINCT activity_date) AS days, MIN(activity_date) AS first_day, MAX(activity_date) AS last_day, COUNT(*) AS rows_n
FROM `youtube_analytics.daily_video_analytics` WHERE load_source != 'cron' GROUP BY 1, 2
UNION ALL SELECT 'daily_traffic_sources', load_source, COUNT(DISTINCT activity_date), MIN(activity_date), MAX(activity_date), COUNT(*)
FROM `youtube_analytics.daily_traffic_sources` WHERE load_source != 'cron' GROUP BY 1, 2
ORDER BY t, last_day DESC;

-- ---------------------------------------------------------------------------
-- --original_tables_integrity
-- Grain uniqueness and freshness of the four original tables after all of the above.
-- Expected: dup = 0 everywhere; newest snapshot = today (Phoenix); newest activity = today
-- minus the lookback (6 days) at most.
-- ---------------------------------------------------------------------------
SELECT 'video_metadata' AS t, COUNT(*) AS rows_n, COUNT(*) - COUNT(DISTINCT CONCAT(snapshot_date, '|', video_id)) AS dup, MAX(snapshot_date) AS newest FROM `youtube_analytics.video_metadata`
UNION ALL SELECT 'daily_video_stats', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(snapshot_date, '|', video_id)), MAX(snapshot_date) FROM `youtube_analytics.daily_video_stats`
UNION ALL SELECT 'daily_video_analytics', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(activity_date, '|', video_id)), MAX(activity_date) FROM `youtube_analytics.daily_video_analytics`
UNION ALL SELECT 'daily_traffic_sources', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(activity_date, '|', video_id, '|', traffic_source_type)), MAX(activity_date) FROM `youtube_analytics.daily_traffic_sources`
ORDER BY t;
