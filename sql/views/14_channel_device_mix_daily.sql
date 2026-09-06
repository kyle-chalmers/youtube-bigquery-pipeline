-- channel_device_mix_daily: share of views and watch time by device type per day, so the
-- decision "format for TV or phone" rests on data. Also split by video_type because Shorts
-- skew mobile.
-- Grain: (report_date, video_type, device_type). Cardinality: device_os aggregated to
-- (day, video, device) first, LEFT JOIN video_current (n:1) for video_type, then aggregated
-- again to the grain. Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Denominators: *_share are that row's views (or watch time) over the day's total for the
-- same video_type. Shares within a (report_date, video_type) sum to 1.
-- operating_system is summed away here; an OS split is deferred until the source has data
-- (the plan listed "OS share"; the column exists in the raw table for a later view).
-- Source: reporting_channel_device_os_a3 (data from about 2026-09-07).
CREATE OR REPLACE VIEW `${BQ_DATASET}.channel_device_mix_daily` AS
WITH per_video AS (
  SELECT report_date, video_id, device_type, SUM(views) AS views, SUM(watch_time_minutes) AS watch_time_minutes
  FROM `${BQ_DATASET}.reporting_channel_device_os_a3`
  GROUP BY 1, 2, 3
),
d AS (
  SELECT p.report_date, IFNULL(v.video_type, 'unknown') AS video_type, p.device_type,
         SUM(p.views) AS views, SUM(p.watch_time_minutes) AS watch_time_minutes
  FROM per_video p
  LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = p.video_id
  GROUP BY 1, 2, 3
)
SELECT report_date, video_type, device_type,
       CASE device_type WHEN '100' THEN 'unknown' WHEN '101' THEN 'computer' WHEN '102' THEN 'tv'
                        WHEN '103' THEN 'game_console' WHEN '104' THEN 'mobile' WHEN '105' THEN 'tablet'
                        ELSE CONCAT('code_', device_type) END AS device_name,
       views, watch_time_minutes,
       SAFE_DIVIDE(views, SUM(views) OVER (PARTITION BY report_date, video_type)) AS view_share,
       SAFE_DIVIDE(watch_time_minutes, SUM(watch_time_minutes) OVER (PARTITION BY report_date, video_type)) AS watch_time_share
FROM d;
