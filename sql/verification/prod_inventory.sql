-- What exists in production after the 2026-09-06 promotion, and what changed in the tables
-- that existed before. Paste ONE query at a time; the comment above each says what it is for. Every
-- query is read-only. Written against `youtube_analytics`; replace with
-- `youtube_analytics_staging.` to inspect staging.

--every_table_view_and_snapshot_with_row_counts
SELECT t.table_name, t.table_type, DATE(t.creation_time) AS created,
       s.row_count, ROUND(s.size_bytes / 1e6, 2) AS mb
FROM `youtube_analytics.INFORMATION_SCHEMA.TABLES` t
LEFT JOIN `youtube_analytics.__TABLES__` s ON s.table_id = t.table_name
ORDER BY t.table_type, t.table_name;

--pre_promotion_snapshots_and_expiry
SELECT table_name, option_value AS expires
FROM `youtube_analytics.INFORMATION_SCHEMA.TABLE_OPTIONS`
WHERE option_name = 'expiration_timestamp' AND table_name LIKE '%_snapshot_%'
ORDER BY table_name;

--reporting_days_loaded_per_report_type_vs_ledger
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

--ledger_rows_needing_a_human
SELECT report_type, report_date, status, error, ingested_at
FROM `youtube_analytics.reporting_ingest_ledger`
WHERE status IN ('failed', 'header_only_conflict')
ORDER BY ingested_at DESC;

--row_count_and_newest_day_per_view
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

--original_tables_last_14_days_by_partition_and_load_source
SELECT 'video_metadata' AS t, snapshot_date AS day, NULL AS load_source, COUNT(*) AS rows_n
FROM `youtube_analytics.video_metadata` WHERE snapshot_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 14 DAY) GROUP BY 1, 2
UNION ALL SELECT 'daily_video_stats', snapshot_date, NULL, COUNT(*)
FROM `youtube_analytics.daily_video_stats` WHERE snapshot_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 14 DAY) GROUP BY 1, 2
UNION ALL SELECT 'daily_video_analytics', activity_date, load_source, COUNT(*)
FROM `youtube_analytics.daily_video_analytics` WHERE activity_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 20 DAY) GROUP BY 1, 2, 3
UNION ALL SELECT 'daily_traffic_sources', activity_date, load_source, COUNT(*)
FROM `youtube_analytics.daily_traffic_sources` WHERE activity_date >= DATE_SUB(CURRENT_DATE('America/Phoenix'), INTERVAL 20 DAY) GROUP BY 1, 2, 3
ORDER BY t, day DESC;

--original_table_partitions_not_written_by_cron
SELECT 'daily_video_analytics' AS t, load_source, COUNT(DISTINCT activity_date) AS days, MIN(activity_date) AS first_day, MAX(activity_date) AS last_day, COUNT(*) AS rows_n
FROM `youtube_analytics.daily_video_analytics` WHERE load_source != 'cron' GROUP BY 1, 2
UNION ALL SELECT 'daily_traffic_sources', load_source, COUNT(DISTINCT activity_date), MIN(activity_date), MAX(activity_date), COUNT(*)
FROM `youtube_analytics.daily_traffic_sources` WHERE load_source != 'cron' GROUP BY 1, 2
ORDER BY t, last_day DESC;

--original_tables_grain_uniqueness_and_freshness
SELECT 'video_metadata' AS t, COUNT(*) AS rows_n, COUNT(*) - COUNT(DISTINCT CONCAT(snapshot_date, '|', video_id)) AS dup, MAX(snapshot_date) AS newest FROM `youtube_analytics.video_metadata`
UNION ALL SELECT 'daily_video_stats', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(snapshot_date, '|', video_id)), MAX(snapshot_date) FROM `youtube_analytics.daily_video_stats`
UNION ALL SELECT 'daily_video_analytics', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(activity_date, '|', video_id)), MAX(activity_date) FROM `youtube_analytics.daily_video_analytics`
UNION ALL SELECT 'daily_traffic_sources', COUNT(*), COUNT(*) - COUNT(DISTINCT CONCAT(activity_date, '|', video_id, '|', traffic_source_type)), MAX(activity_date) FROM `youtube_analytics.daily_traffic_sources`
ORDER BY t;
