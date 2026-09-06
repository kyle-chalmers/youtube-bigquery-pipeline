-- video_audience_growth: which videos bring new people in, per video per day, plus the
-- channel-level subscriber activity that is not attributable to any video.
-- Grain: (report_date, video_id) with video_id NULL for the channel-level line; 1 row per
-- (day, video) present in basic_a3. Cardinality: basic_a3 aggregated to video-day (1:1),
-- LEFT JOIN video_current (n:1). Asserted by --grain_checks.
-- Timezone: report_date is a Pacific-time day.
-- Denominators: non_subscriber_view_share = views from not_subscribed / views;
--   subscribers_gained_per_1k_views = gained / views * 1000 (NULL for the channel-level line).
-- The channel-level line (video_id IS NULL, is_channel_level = TRUE) held about a third of
-- all subscribers gained on this channel in the first five weeks of data; channel growth is
-- the sum over ALL rows, not over videos. YouTube's own "videos growing your audience" uses
-- new viewers, which the API does not expose; non_subscriber_view_share is the closest proxy.
-- Source: reporting_channel_basic_a3, video_current.
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_audience_growth` AS
WITH b AS (
  SELECT report_date, video_id,
         SUM(views) AS views,
         SUM(IF(subscribed_status = 'not_subscribed', views, 0)) AS views_from_non_subscribers,
         SUM(IF(subscribed_status = 'not_subscribed', watch_time_minutes, 0)) AS watch_time_minutes_from_non_subscribers,
         SUM(watch_time_minutes) AS watch_time_minutes,
         SUM(subscribers_gained) AS subscribers_gained,
         SUM(subscribers_lost) AS subscribers_lost
  FROM `${BQ_DATASET}.reporting_channel_basic_a3`
  GROUP BY 1, 2
)
SELECT b.report_date, b.video_id, b.video_id IS NULL AS is_channel_level,
       v.title, v.video_type, v.published_at,
       b.views, b.views_from_non_subscribers,
       SAFE_DIVIDE(b.views_from_non_subscribers, b.views) AS non_subscriber_view_share,
       b.watch_time_minutes_from_non_subscribers, b.watch_time_minutes,
       b.subscribers_gained, b.subscribers_lost,
       b.subscribers_gained - b.subscribers_lost AS net_subscribers,
       IF(b.video_id IS NULL, NULL, SAFE_DIVIDE(b.subscribers_gained, b.views) * 1000) AS subscribers_gained_per_1k_views
FROM b
LEFT JOIN `${BQ_DATASET}.video_current` v USING (video_id);
