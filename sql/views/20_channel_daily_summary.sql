-- channel_daily_summary: one row per Pacific day for the whole channel, with 7-day and
-- 28-day rolling windows, split so long-form and Shorts can be read separately.
-- Grain: report_date (1 row per day). Cardinality: basic_a3 is aggregated to (day, video)
-- first, LEFT JOINed to video_current (n:1) for the type split, then aggregated to the day;
-- reach_basic is aggregated to the day; both LEFT JOIN a day spine built from the raw days and
-- the ledger (loaded or header-only reports). Row count = distinct days across the sources
-- and the ledger. Asserted by --grain_checks and --no_fanout.
-- Timezone: report_date is a Pacific-time day.
-- Definitions: views and watch time come from channel_basic_a3 (the source of truth for
-- views; the traffic-source report can differ by generation timing). subscribers_gained and
-- subscribers_lost INCLUDE the channel-level rows (NULL video_id). impressions and clicks
-- come from reach_basic (impressions are counted on YouTube surfaces only). ctr is recomputed
-- from totals. A day whose report exists (raw rows or a loaded/header-only ledger row) but has
-- no rows on one side reads as 0 on that side; NULL means no report for that day on that side.
-- Rolling windows are CALENDAR windows (RANGE over the day number, trailing 7 and 28 days
-- including the current day); a missing day contributes nothing, and days_in_7d /
-- days_in_28d say how many days actually fed each sum so a partial window is visible.
-- shorts_views + long_form_views + unclassified_views = views; unclassified_views are views
-- on videos absent from the latest metadata snapshot (deleted, private, or unlisted).
-- Sources: reporting_channel_basic_a3, reporting_channel_reach_basic_a1, reporting_ingest_ledger
-- (day existence only), video_current.
CREATE OR REPLACE VIEW `${BQ_DATASET}.channel_daily_summary` AS
WITH per_video AS (
  SELECT report_date, video_id,
         SUM(views) AS views, SUM(engaged_views) AS engaged_views, SUM(watch_time_minutes) AS watch_time_minutes,
         SUM(IF(subscribed_status = 'not_subscribed', views, 0)) AS views_from_non_subscribers,
         SUM(subscribers_gained) AS subscribers_gained, SUM(subscribers_lost) AS subscribers_lost,
         SUM(likes) AS likes, SUM(comments) AS comments, SUM(shares) AS shares
  FROM `${BQ_DATASET}.reporting_channel_basic_a3`
  GROUP BY 1, 2
),
act AS (
  SELECT p.report_date,
         SUM(p.views) AS views, SUM(p.engaged_views) AS engaged_views, SUM(p.watch_time_minutes) AS watch_time_minutes,
         SUM(IF(v.video_type = 'short', p.views, 0)) AS shorts_views,
         SUM(IF(v.video_type = 'full_length', p.views, 0)) AS long_form_views,
         SUM(IF(v.video_type = 'full_length', p.watch_time_minutes, 0)) AS long_form_watch_time_minutes,
         SUM(IF(v.video_id IS NULL, p.views, 0)) AS unclassified_views,
         SUM(p.views_from_non_subscribers) AS views_from_non_subscribers,
         SUM(p.subscribers_gained) AS subscribers_gained, SUM(p.subscribers_lost) AS subscribers_lost,
         SUM(IF(p.video_id IS NULL, p.subscribers_gained, 0)) AS subscribers_gained_channel_level,
         SUM(p.likes) AS likes, SUM(p.comments) AS comments, SUM(p.shares) AS shares,
         COUNT(DISTINCT IF(p.views > 0, p.video_id, NULL)) AS videos_with_views
  FROM per_video p
  LEFT JOIN `${BQ_DATASET}.video_current` v ON v.video_id = p.video_id
  GROUP BY 1
),
reach AS (
  SELECT report_date, SUM(video_thumbnail_impressions) AS impressions,
         SAFE_DIVIDE(SUM(video_thumbnail_impressions * video_thumbnail_impressions_ctr), SUM(video_thumbnail_impressions)) AS ctr
  FROM `${BQ_DATASET}.reporting_channel_reach_basic_a1` GROUP BY 1
),
act_days AS (
  SELECT report_date FROM act
  UNION DISTINCT
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_ingest_ledger`
  WHERE report_type = 'channel_basic_a3' AND status IN ('loaded', 'header_only')
),
reach_days AS (
  SELECT report_date FROM reach
  UNION DISTINCT
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_ingest_ledger`
  WHERE report_type = 'channel_reach_basic_a1' AND status IN ('loaded', 'header_only')
),
spine AS (SELECT report_date FROM act_days UNION DISTINCT SELECT report_date FROM reach_days),
d AS (
  SELECT s.report_date,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.views) AS views,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.engaged_views) AS engaged_views,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.watch_time_minutes) AS watch_time_minutes,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.shorts_views) AS shorts_views,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.long_form_views) AS long_form_views,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.long_form_watch_time_minutes) AS long_form_watch_time_minutes,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.unclassified_views) AS unclassified_views,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.views_from_non_subscribers) AS views_from_non_subscribers,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_gained) AS subscribers_gained,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_lost) AS subscribers_lost,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_gained_channel_level) AS subscribers_gained_channel_level,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.likes) AS likes,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.comments) AS comments,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.shares) AS shares,
         IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.videos_with_views) AS videos_with_views,
         IF(r.report_date IS NULL AND rd.report_date IS NOT NULL, 0, r.impressions) AS impressions,
         r.ctr,
         IF(r.report_date IS NULL AND rd.report_date IS NOT NULL, 0, CAST(ROUND(r.impressions * r.ctr) AS INT64)) AS clicks
  FROM spine s
  LEFT JOIN act a USING (report_date)
  LEFT JOIN reach r USING (report_date)
  LEFT JOIN act_days ad USING (report_date)
  LEFT JOIN reach_days rd USING (report_date)
)
SELECT *,
       subscribers_gained - subscribers_lost AS net_subscribers,
       SAFE_DIVIDE(views_from_non_subscribers, views) AS non_subscriber_view_share,
       SAFE_DIVIDE(engaged_views, views) AS engaged_start_share,
       SUM(views) OVER w7 AS views_7d,
       COUNT(views) OVER w7 AS days_in_7d,
       SUM(views) OVER w28 AS views_28d,
       COUNT(views) OVER w28 AS days_in_28d,
       SUM(watch_time_minutes) OVER w28 / 60 AS watch_hours_28d,
       SUM(subscribers_gained - subscribers_lost) OVER w28 AS net_subscribers_28d,
       SUM(impressions) OVER w28 AS impressions_28d,
       COUNT(impressions) OVER w28 AS days_with_impressions_in_28d
FROM d
WINDOW w7 AS (ORDER BY UNIX_DATE(report_date) RANGE BETWEEN 6 PRECEDING AND CURRENT ROW),
       w28 AS (ORDER BY UNIX_DATE(report_date) RANGE BETWEEN 27 PRECEDING AND CURRENT ROW);
