-- video_traffic_detail_daily: where each video's views came from, with the detail YouTube
-- exposes: search terms (type 5), the suggesting video (type 7 and 32, resolved to a title when
-- it is one of this channel's own videos), external URLs (type 9), browse surfaces (type 3).
-- Grain: (report_date, video_id, traffic_source_type, traffic_source_detail), the report's
-- native grain summed over subscribed_status, country and live/on-demand. Cardinality:
-- traffic_source_a3 aggregated (1:1), LEFT JOIN lookup (n:1), LEFT JOIN video_current twice
-- (n:1 each). Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Nulls: traffic_source_detail is NULL under YouTube's anonymisation threshold; on this
-- channel only about 1 in 200 search rows carried a visible search term, so search-term
-- analysis is thin by design. referrer_is_own_video is NULL when detail is not a video id.
-- Source: reporting_channel_traffic_source_a3, traffic_source_type_lookup, video_current.
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_traffic_detail_daily` AS
WITH t AS (
  SELECT report_date, video_id, traffic_source_type, traffic_source_detail,
         SUM(views) AS views, SUM(engaged_views) AS engaged_views, SUM(watch_time_minutes) AS watch_time_minutes
  FROM `${BQ_DATASET}.reporting_channel_traffic_source_a3`
  WHERE video_id IS NOT NULL
  GROUP BY 1, 2, 3, 4
)
SELECT t.report_date, t.video_id, v.title, v.video_type,
       t.traffic_source_type, l.name AS traffic_source_name, l.surface, l.analytics_name,
       t.traffic_source_detail,
       CASE WHEN l.detail_is_video_id AND t.traffic_source_detail IS NOT NULL THEN ref.video_id IS NOT NULL END AS referrer_is_own_video,
       ref.title AS referrer_title,
       t.views, t.engaged_views, t.watch_time_minutes
FROM t
LEFT JOIN `${BQ_DATASET}.traffic_source_type_lookup` l ON l.code = t.traffic_source_type
LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = t.video_id
LEFT JOIN `${BQ_DATASET}.video_current` ref
       ON l.detail_is_video_id AND ref.video_id = t.traffic_source_detail;
