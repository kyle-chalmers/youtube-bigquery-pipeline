-- video_ctr_by_surface_daily: impressions and CTR per video per day per traffic surface and
-- device. YouTube's help centre says CTR must be read per surface: search shows fewer
-- impressions at higher CTR, the home feed the reverse, so one blended CTR misleads.
-- Grain: (report_date, video_id, traffic_source_type, device_type), summed over
-- traffic_source_detail and operating_system. Cardinality: reach_combined aggregated (1:1),
-- LEFT JOIN lookup (n:1), LEFT JOIN video_current (n:1). Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Formula: ctr = SUM(impressions * ctr) / SUM(impressions), never an average of CTRs.
-- Device codes: 100 unknown, 101 computer, 102 TV, 103 game console, 104 mobile, 105 tablet.
-- Source: reporting_channel_reach_combined_a1 (job created 2026-09-05; data from about
-- 2026-09-07, backfilled to about 2026-08-06).
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_ctr_by_surface_daily` AS
WITH r AS (
  SELECT report_date, video_id, traffic_source_type, device_type,
         SUM(video_thumbnail_impressions) AS impressions,
         SAFE_DIVIDE(SUM(video_thumbnail_impressions * video_thumbnail_impressions_ctr), SUM(video_thumbnail_impressions)) AS ctr,
         CAST(ROUND(SUM(video_thumbnail_impressions * video_thumbnail_impressions_ctr)) AS INT64) AS clicks
  FROM `${BQ_DATASET}.reporting_channel_reach_combined_a1`
  GROUP BY 1, 2, 3, 4
)
SELECT r.report_date, r.video_id, v.title, v.video_type,
       r.traffic_source_type, l.name AS traffic_source_name, l.surface,
       r.device_type,
       CASE r.device_type WHEN '100' THEN 'unknown' WHEN '101' THEN 'computer' WHEN '102' THEN 'tv'
                          WHEN '103' THEN 'game_console' WHEN '104' THEN 'mobile' WHEN '105' THEN 'tablet'
                          ELSE CONCAT('code_', r.device_type) END AS device_name,
       r.impressions, r.ctr, r.clicks
FROM r
LEFT JOIN `${BQ_DATASET}.traffic_source_type_lookup` l ON l.code = r.traffic_source_type
LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = r.video_id;
