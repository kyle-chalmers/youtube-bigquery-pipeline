-- video_daily_funnel: the impressions -> clicks -> views -> watch time funnel per video per day.
-- Grain: (report_date, video_id), 1 row per video per Pacific day that has EITHER reach or
-- activity data. A metric is 0 when the source report for that day exists and simply omits
-- the video (the source drops zero rows), and NULL only when no report for that day has been
-- loaded on that side (reach data starts 2026-07-31, activity 2026-06-15). A day's report
-- exists when the raw table has rows for it OR the ledger holds a loaded or header-only
-- report for it (a header-only report proves a zero day and leaves no raw rows). Cardinality: reach_basic (1:1) FULL OUTER JOIN basic_a3 aggregated to
-- video-day (1:1), then LEFT JOIN video_current (n:1). Row count = distinct (day, video)
-- across both sources; asserted by phase3_views.sql --grain_checks and --no_fanout.
-- Timezone: report_date is a Pacific-time day (same as activity_date elsewhere).
-- Denominators and formulas (ratios are never summed, always recomputed from totals):
--   clicks = ROUND(impressions * ctr); ctr is a 0..1 fraction; clicks are NOT views (views
--     include traffic that had no impression, e.g. external links, search suggestions). The
--     source occasionally reports ctr above 1 on a 1-impression row (observed once: 2 clicks on
--     1 impression), so clicks can exceed impressions on such rows.
--   avg_view_duration_seconds = watch_time_minutes * 60 / engaged_views. This is what YouTube
--     Studio shows as "Average view duration" for long-form AND Shorts (Studio spot-check
--     2026-09-02, four videos: Studio 2:44 / 5:31 / 1:31 / 0:18 vs this column 168 / 332 / 93 /
--     20 s; the views-denominator column gave 64 / 222 / 53 / 10 s, so it is NOT Studio's number).
--   avg_view_duration_over_views_seconds = watch_time_minutes * 60 / views, kept for the
--     pre-2026-08-24 history: until YouTube unified view counting on 2026-08-24 (views count
--     from the first frame for every format), long-form views and engaged views were the same
--     number on this channel (ratio 0.998), so both definitions coincided and the source's own
--     per-row average_view_duration_seconds matched either. From 2026-08-24 the source column
--     matches neither within 1 s on about a third of rows, and it reports 0 whenever
--     engaged_views = 0 even with positive watch time. Studio's definition is the one above.
--     Sources: support.google.com/youtube/answer/12220281 ("calculated from engaged views"),
--     blog.youtube/inside-youtube/engaged-views-youtube-explained (2026-08-24 unification),
--     developers.google.com/youtube/analytics/revision_history (2026-08-27 entry).
--   avg_view_percentage = avg_view_duration_over_views_seconds / duration_seconds * 100 (the
--     source's own scale, not Studio's "average percentage viewed"); can exceed
--     100 on looped playback of any video type (observed up to 497 on a full-length video),
--     exactly as in the source.
--   engaged_start_share = engaged_views / views. Since 2025-03-31 for Shorts and 2026-08-24
--     for every format, a `view` counts from the first frame (start, replay, autoplay, hover);
--     engaged_views counts viewers who clicked to watch or stayed past the initial seconds. A
--     hook-quality proxy for long-form too now, NOT Studio's "swiped away" figure. On this
--     channel long-form engaged/view fell from 0.998 to 0.64 at the 2026-08-24 change.
--   subscribers_* here are the VIDEO-attributed rows only; channel-level subscriber rows
--     (NULL video_id) are in video_audience_growth and channel_daily_summary.
-- Sources: reporting_channel_reach_basic_a1, reporting_channel_basic_a3, reporting_ingest_ledger
-- (day existence only), video_current.
CREATE OR REPLACE VIEW `${BQ_DATASET}.video_daily_funnel` AS
WITH reach AS (
  SELECT report_date, video_id,
         SUM(video_thumbnail_impressions) AS impressions,
         SAFE_DIVIDE(SUM(video_thumbnail_impressions * video_thumbnail_impressions_ctr), SUM(video_thumbnail_impressions)) AS ctr
  FROM `${BQ_DATASET}.reporting_channel_reach_basic_a1`
  WHERE video_id IS NOT NULL
  GROUP BY 1, 2
),
reach_days AS (
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_channel_reach_basic_a1`
  UNION DISTINCT
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_ingest_ledger`
  WHERE report_type = 'channel_reach_basic_a1' AND status IN ('loaded', 'header_only')
),
activity_days AS (
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_channel_basic_a3`
  UNION DISTINCT
  SELECT DISTINCT report_date FROM `${BQ_DATASET}.reporting_ingest_ledger`
  WHERE report_type = 'channel_basic_a3' AND status IN ('loaded', 'header_only')
),
activity AS (
  SELECT report_date, video_id,
         SUM(views) AS views, SUM(engaged_views) AS engaged_views,
         SUM(watch_time_minutes) AS watch_time_minutes,
         SUM(likes) AS likes, SUM(comments) AS comments, SUM(shares) AS shares,
         SUM(subscribers_gained) AS subscribers_gained, SUM(subscribers_lost) AS subscribers_lost,
         SUM(IF(subscribed_status = 'not_subscribed', views, 0)) AS views_from_non_subscribers
  FROM `${BQ_DATASET}.reporting_channel_basic_a3`
  WHERE video_id IS NOT NULL
  GROUP BY 1, 2
)
SELECT k.report_date, k.video_id,
       v.title, v.video_type, v.published_at, v.duration_seconds,
       IF(r.report_date IS NULL AND rd.report_date IS NOT NULL, 0, r.impressions) AS impressions,
       r.ctr,
       IF(r.report_date IS NULL AND rd.report_date IS NOT NULL, 0, CAST(ROUND(r.impressions * r.ctr) AS INT64)) AS clicks,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.views) AS views,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.engaged_views) AS engaged_views,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.watch_time_minutes) AS watch_time_minutes,
       SAFE_DIVIDE(a.watch_time_minutes * 60, a.engaged_views) AS avg_view_duration_seconds,
       SAFE_DIVIDE(a.watch_time_minutes * 60, a.views) AS avg_view_duration_over_views_seconds,
       SAFE_DIVIDE(a.watch_time_minutes * 60, a.views) / NULLIF(v.duration_seconds, 0) * 100 AS avg_view_percentage,
       SAFE_DIVIDE(a.engaged_views, a.views) AS engaged_start_share,
       SAFE_DIVIDE(a.views_from_non_subscribers, a.views) AS non_subscriber_view_share,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.likes) AS likes,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.comments) AS comments,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.shares) AS shares,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_gained) AS subscribers_gained,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_lost) AS subscribers_lost,
       IF(a.report_date IS NULL AND ad.report_date IS NOT NULL, 0, a.subscribers_gained - a.subscribers_lost) AS net_subscribers,
       SAFE_DIVIDE(a.subscribers_gained, a.views) * 1000 AS subscribers_gained_per_1k_views
FROM (SELECT report_date, video_id FROM reach UNION DISTINCT SELECT report_date, video_id FROM activity) k
LEFT JOIN reach r USING (report_date, video_id)
LEFT JOIN activity a USING (report_date, video_id)
LEFT JOIN reach_days rd USING (report_date)
LEFT JOIN activity_days ad USING (report_date)
LEFT JOIN `${BQ_DATASET}.video_current` v USING (video_id);
