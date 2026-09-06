-- video_end_screen_daily: do end screens do anything? Impressions, clicks and click rate per
-- video per day per end-screen element type.
-- Grain: (report_date, video_id, end_screen_element_type), summed over element id and the
-- basic dimensions. Cardinality: end_screens aggregated to the grain FIRST, then LEFT JOIN
-- video_current (n:1) and LEFT JOIN basic_a3 aggregated to video-day (n:1) for the views
-- denominator. `elements` counts distinct element ids with anonymised (NULL) ids as one bucket.
-- Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Formulas: click_rate = clicks / impressions, recomputed; the source's per-row rate is not
-- averaged. clicks_per_1k_views = clicks / the video-day's views (from basic_a3) * 1000.
-- Element type codes: 501 video, 502 playlist, 503 website, 504 channel, 505 subscribe,
-- 506 associated website, 507 crowdfunding, 508 merchandise, 509 recent upload, 510 best for
-- viewer; anything else keeps its code.
-- Source: reporting_channel_end_screens_a1 (data from about 2026-09-07).
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_end_screen_daily` AS
WITH e AS (
  SELECT report_date, video_id, end_screen_element_type,
         COUNT(DISTINCT IFNULL(end_screen_element_id, '<anonymous>')) AS elements,
         SUM(end_screen_element_impressions) AS impressions,
         SUM(end_screen_element_clicks) AS clicks
  FROM `${BQ_DATASET}.reporting_channel_end_screens_a1`
  GROUP BY 1, 2, 3
),
b AS (
  SELECT report_date, video_id, SUM(views) AS views
  FROM `${BQ_DATASET}.reporting_channel_basic_a3` WHERE video_id IS NOT NULL GROUP BY 1, 2
)
SELECT e.report_date, e.video_id, v.title, v.video_type, e.end_screen_element_type,
       CASE e.end_screen_element_type WHEN '501' THEN 'video' WHEN '502' THEN 'playlist' WHEN '503' THEN 'website'
            WHEN '504' THEN 'channel' WHEN '505' THEN 'subscribe' WHEN '507' THEN 'crowdfunding' WHEN '508' THEN 'merchandise'
            WHEN '506' THEN 'associated_website' WHEN '509' THEN 'recent_upload' WHEN '510' THEN 'best_for_viewer'
            ELSE CONCAT('code_', e.end_screen_element_type) END AS element_type_name,
       e.elements, e.impressions, e.clicks,
       SAFE_DIVIDE(e.clicks, e.impressions) AS click_rate,
       b.views,
       SAFE_DIVIDE(e.clicks, b.views) * 1000 AS clicks_per_1k_views
FROM e
LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = e.video_id
LEFT JOIN b ON b.report_date = e.report_date AND b.video_id = e.video_id;
