-- Phase 3 verification: the growth views are honest.
--
-- Paste ONE tagged block at a time into the BigQuery console; replace `youtube_analytics.`
-- with `youtube_analytics_staging.` to run against staging. Every query is read-only.
-- Expected results are stated above each block. scripts/verify_views.sh runs them all.
--
-- Two rules every view follows, both asserted here: the view's row count equals its distinct
-- grain-key count (no fan-out through video_metadata or through a join), and every ratio
-- is recomputed from totals rather than averaged from the source's per-row ratio.

-- ---------------------------------------------------------------------------
-- --grain_checks
-- One row per view: rows, distinct grain keys, and the difference. Expected: dup = 0 on
-- every row. Views whose source tables are still empty (jobs created 2026-09-05) show 0 rows.
-- ---------------------------------------------------------------------------
SELECT 'video_current' AS view_name, COUNT(*) AS rows_n, COUNT(DISTINCT video_id) AS keys_n, COUNT(*) - COUNT(DISTINCT video_id) AS dup
FROM `youtube_analytics.video_current`
UNION ALL SELECT 'traffic_source_type_lookup', COUNT(*), COUNT(DISTINCT code), COUNT(*) - COUNT(DISTINCT code) FROM `youtube_analytics.traffic_source_type_lookup`
UNION ALL SELECT 'video_daily_funnel', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', video_id)), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', video_id)) FROM `youtube_analytics.video_daily_funnel`
UNION ALL SELECT 'video_audience_growth', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, '<channel>'))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, '<channel>'))) FROM `youtube_analytics.video_audience_growth`
UNION ALL SELECT 'video_traffic_detail_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', video_id, '|', traffic_source_type, '|', IFNULL(traffic_source_detail, '<null>'))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', video_id, '|', traffic_source_type, '|', IFNULL(traffic_source_detail, '<null>'))) FROM `youtube_analytics.video_traffic_detail_daily`
UNION ALL SELECT 'video_ctr_by_surface_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(traffic_source_type, ''), '|', IFNULL(device_type, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(traffic_source_type, ''), '|', IFNULL(device_type, ''))) FROM `youtube_analytics.video_ctr_by_surface_daily`
UNION ALL SELECT 'channel_device_mix_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', video_type, '|', IFNULL(device_type, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', video_type, '|', IFNULL(device_type, ''))) FROM `youtube_analytics.channel_device_mix_daily`
UNION ALL SELECT 'video_end_screen_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(end_screen_element_type, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(end_screen_element_type, ''))) FROM `youtube_analytics.video_end_screen_daily`
UNION ALL SELECT 'video_cards_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(card_type, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(video_id, ''), '|', IFNULL(card_type, ''))) FROM `youtube_analytics.video_cards_daily`
UNION ALL SELECT 'channel_sharing_daily', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(sharing_service, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(sharing_service, ''))) FROM `youtube_analytics.channel_sharing_daily`
UNION ALL SELECT 'channel_demographics', COUNT(*), COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(age_group, ''), '|', IFNULL(gender, ''))), COUNT(*) - COUNT(DISTINCT CONCAT(report_date, '|', IFNULL(age_group, ''), '|', IFNULL(gender, ''))) FROM `youtube_analytics.channel_demographics`
UNION ALL SELECT 'channel_daily_summary', COUNT(*), COUNT(DISTINCT report_date), COUNT(*) - COUNT(DISTINCT report_date) FROM `youtube_analytics.channel_daily_summary`
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- --no_fanout
-- The funnel's row count must equal the number of distinct (day, video) across its two
-- sources, and channel_daily_summary's row count the number of distinct days across its
-- sources plus the ledger's loaded or header-only days. Expected: diff = 0 on both rows.
-- ---------------------------------------------------------------------------
WITH f AS (SELECT COUNT(*) AS n FROM `youtube_analytics.video_daily_funnel`),
     fk AS (SELECT COUNT(*) AS n FROM (
        SELECT report_date, video_id FROM `youtube_analytics.reporting_channel_reach_basic_a1`
        UNION DISTINCT SELECT report_date, video_id FROM `youtube_analytics.reporting_channel_basic_a3` WHERE video_id IS NOT NULL)),
     s AS (SELECT COUNT(*) AS n FROM `youtube_analytics.channel_daily_summary`),
     sk AS (SELECT COUNT(*) AS n FROM (
        SELECT report_date FROM `youtube_analytics.reporting_channel_reach_basic_a1`
        UNION DISTINCT SELECT report_date FROM `youtube_analytics.reporting_channel_basic_a3`
        UNION DISTINCT SELECT report_date FROM `youtube_analytics.reporting_ingest_ledger`
        WHERE report_type IN ('channel_basic_a3', 'channel_reach_basic_a1') AND status IN ('loaded', 'header_only')))
SELECT 'video_daily_funnel' AS view_name, f.n AS view_rows, fk.n AS source_keys, f.n - fk.n AS diff FROM f, fk
UNION ALL SELECT 'channel_daily_summary', s.n, sk.n, s.n - sk.n FROM s, sk;

-- ---------------------------------------------------------------------------
-- --funnel_identity
-- clicks = ROUND(impressions * ctr) by construction; the checkable facts are that clicks never
-- exceed impressions on any row with more than 2 impressions (the source reports ctr above 1
-- on rare 1-impression rows), and that the sum of clicks is below the sum of views (views
-- include non-impression traffic).
-- clicks is exact because impressions * ctr in the source is an integer to floating-point
-- precision (observed max rounding error 7e-15), which is asserted here.
-- video_days_without_reach / _without_activity count NULLs, which after the fill rule are
-- only days with no loaded report on that side (reach coverage starts 2026-07-31).
-- Expected: clicks_over_impressions_gt2 = 0; clicks_formula_mismatch = 0; clicks_total < views_total;
-- max_click_rounding_error < 0.000001.
-- ---------------------------------------------------------------------------
SELECT COUNTIF(clicks > impressions AND impressions > 2) AS clicks_over_impressions_gt2,
       COUNTIF(impressions IS NOT NULL AND clicks != CAST(ROUND(impressions * ctr) AS INT64)) AS clicks_formula_mismatch,
       (SELECT MAX(ABS(video_thumbnail_impressions * video_thumbnail_impressions_ctr - ROUND(video_thumbnail_impressions * video_thumbnail_impressions_ctr)))
        FROM `youtube_analytics.reporting_channel_reach_basic_a1`) AS max_click_rounding_error,
       COUNTIF(clicks > impressions) AS clicks_over_impressions_any,
       SUM(clicks) AS clicks_total, SUM(views) AS views_total,
       COUNTIF(impressions IS NULL) AS video_days_without_reach,
       COUNTIF(views IS NULL) AS video_days_without_activity,
       COUNT(*) AS video_days
FROM `youtube_analytics.video_daily_funnel`;

-- ---------------------------------------------------------------------------
-- --avd_recompute_check
-- On video-days where the source has a single segment row, compare the view's two AVD columns
-- against the source's own per-row average_view_duration_seconds, by video type and by period.
-- YouTube unified view counting on 2026-08-24 (views count from the first frame for every
-- format; engaged views keep the old definition; Studio's AVD is watch time over engaged views,
-- per support.google.com/youtube/answer/12220281). Observed 2026-09-05: before 08-24 long-form
-- views and engaged views were identical on this channel (ratio 0.998), so the source column
-- matched BOTH denominators (1.00 / 0.997); from 08-24 the ratio is 0.64 and the source column
-- matches neither within 1 s on about a third of rows. Shorts never matched cleanly (source
-- reports 0 when engaged_views = 0).
-- Expected: the full_length / before row has match_share_over_engaged >= 0.95 (a fixed cohort
-- that cannot decay). Every other row is informational.
-- ---------------------------------------------------------------------------
WITH single AS (
  SELECT b.report_date, b.video_id, IFNULL(v.video_type, 'unknown') AS video_type,
         ANY_VALUE(b.average_view_duration_seconds) AS src_avd, SUM(b.views) AS views, SUM(b.engaged_views) AS engaged_views
  FROM `youtube_analytics.reporting_channel_basic_a3` b
  LEFT JOIN `youtube_analytics.video_current` v USING (video_id)
  WHERE b.video_id IS NOT NULL AND b.views > 0
  GROUP BY 1, 2, 3 HAVING COUNT(*) = 1
)
SELECT s.video_type, IF(s.report_date >= '2026-08-24', 'from_2026-08-24', 'before') AS period,
       COUNT(*) AS single_segment_video_days,
       ROUND(SUM(s.engaged_views) / SUM(s.views), 3) AS engaged_per_view,
       ROUND(COUNTIF(ABS(f.avg_view_duration_over_views_seconds - s.src_avd) <= 1) / COUNT(*), 3) AS match_share_over_views,
       ROUND(COUNTIF(ABS(f.avg_view_duration_seconds - s.src_avd) <= 1) / COUNT(*), 3) AS match_share_over_engaged
FROM single s JOIN `youtube_analytics.video_daily_funnel` f USING (report_date, video_id)
GROUP BY 1, 2 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- --summary_reconciles_to_sources
-- channel_daily_summary totals must equal the raw sources summed the same way.
-- Expected: every diff column = 0.
-- ---------------------------------------------------------------------------
WITH s AS (SELECT SUM(views) v, SUM(subscribers_gained) sg, SUM(subscribers_lost) sl, SUM(impressions) imp FROM `youtube_analytics.channel_daily_summary`),
     b AS (SELECT SUM(views) v, SUM(subscribers_gained) sg, SUM(subscribers_lost) sl FROM `youtube_analytics.reporting_channel_basic_a3`),
     r AS (SELECT SUM(video_thumbnail_impressions) imp FROM `youtube_analytics.reporting_channel_reach_basic_a1`)
SELECT s.v - b.v AS views_diff, s.sg - b.sg AS gained_diff, s.sl - b.sl AS lost_diff, s.imp - r.imp AS impressions_diff FROM s, b, r;

-- ---------------------------------------------------------------------------
-- --channel_level_subscribers_not_dropped
-- The channel-level subscriber rows (NULL video_id) must appear in video_audience_growth and
-- be included in channel_daily_summary. Expected: audience_channel_rows > 0 and
-- summary_channel_level equals source_channel_level.
-- ---------------------------------------------------------------------------
SELECT (SELECT COUNT(*) FROM `youtube_analytics.video_audience_growth` WHERE is_channel_level) AS audience_channel_rows,
       (SELECT SUM(subscribers_gained_channel_level) FROM `youtube_analytics.channel_daily_summary`) AS summary_channel_level,
       (SELECT SUM(subscribers_gained) FROM `youtube_analytics.reporting_channel_basic_a3` WHERE video_id IS NULL) AS source_channel_level;

-- ---------------------------------------------------------------------------
-- --traffic_codes_all_named
-- Every traffic_source_type code present in either code-resolving view must resolve to a name
-- in the lookup. One summary row so an empty result can never read as a pass.
-- Expected: unnamed_codes = 0 and rows_checked > 0.
-- ---------------------------------------------------------------------------
SELECT COUNT(*) AS rows_checked,
       COUNT(DISTINCT IF(traffic_source_name IS NULL, CONCAT(src, ':', traffic_source_type), NULL)) AS unnamed_codes,
       STRING_AGG(DISTINCT IF(traffic_source_name IS NULL, CONCAT(src, ':', traffic_source_type), NULL), ',') AS unnamed_list
FROM (SELECT 'detail' AS src, traffic_source_type, traffic_source_name FROM `youtube_analytics.video_traffic_detail_daily`
      UNION ALL SELECT 'surface', traffic_source_type, traffic_source_name FROM `youtube_analytics.video_ctr_by_surface_daily`);

-- ---------------------------------------------------------------------------
-- --non_subscriber_split
-- The subscribed_status literals the views hardcode must be the ones the source emits, and the
-- non-subscriber share must be a live number, not a silent zero from a wrong literal.
-- Expected: literals = 'not_subscribed|subscribed' (pipe-joined so the CSV stays one field);
-- 0 < non_subscriber_views < views.
-- ---------------------------------------------------------------------------
SELECT (SELECT STRING_AGG(DISTINCT subscribed_status, '|' ORDER BY subscribed_status) FROM `youtube_analytics.reporting_channel_basic_a3`) AS literals,
       SUM(views_from_non_subscribers) AS non_subscriber_views, SUM(views) AS views,
       SUM(engaged_views_from_non_subscribers) AS non_subscriber_engaged_views, SUM(engaged_views) AS engaged_views
FROM `youtube_analytics.channel_daily_summary`;

-- ---------------------------------------------------------------------------
-- --rolling_windows
-- The 7 and 28 day windows are calendar windows: recomputed here from the summary's own daily
-- views by date arithmetic, and days_in_7d can never exceed 7.
-- Expected: every column 0.
-- ---------------------------------------------------------------------------
WITH s AS (SELECT * FROM `youtube_analytics.channel_daily_summary`),
     recomputed AS (
  SELECT a.report_date, a.views_7d, a.views_28d, a.days_in_7d,
         (SELECT SUM(b.views) FROM s b WHERE b.report_date BETWEEN DATE_SUB(a.report_date, INTERVAL 6 DAY) AND a.report_date) AS v7,
         (SELECT SUM(b.views) FROM s b WHERE b.report_date BETWEEN DATE_SUB(a.report_date, INTERVAL 27 DAY) AND a.report_date) AS v28
  FROM s a)
SELECT COUNTIF(views_7d != v7) AS rows_7d_wrong, COUNTIF(views_28d != v28) AS rows_28d_wrong,
       COUNTIF(days_in_7d > 7) AS windows_over_7_days, COUNTIF(views_7d IS NULL) AS null_windows
FROM recomputed;

-- ---------------------------------------------------------------------------
-- --type_split_reconciles
-- shorts_views + long_form_views + unclassified_views must equal views on every day.
-- Expected: days_off = 0; unclassified_views_total is informational (videos gone from metadata).
-- ---------------------------------------------------------------------------
SELECT COUNTIF(shorts_views + long_form_views + unclassified_views != views) AS days_off,
       SUM(unclassified_views) AS unclassified_views_total, COUNT(*) AS days
FROM `youtube_analytics.channel_daily_summary`;

-- ---------------------------------------------------------------------------
-- --studio_spotcheck
-- Three videos for the newest complete day, laid out in YouTube Studio's Advanced Mode
-- order so the two can sit side by side: Views, Watch time (hours), Average view duration,
-- Impressions, Impressions click-through rate, Subscribers. The day is the newest one at least
-- three days old (docs/studio-comparison.md rule 2). Pick that day in Studio, set the date
-- range to that single day, and compare. Edit the LIMIT or add a WHERE video_id IN (...)
-- to choose the videos. Studio rounds CTR to 0.1% and watch time to 0.1 h.
-- Expected: exact on views, impressions and subscribers; within rounding on CTR and hours.
-- ---------------------------------------------------------------------------
WITH day AS (SELECT MAX(report_date) AS d FROM `youtube_analytics.video_daily_funnel`
             WHERE views IS NOT NULL AND impressions IS NOT NULL
               AND report_date <= DATE_SUB(CURRENT_DATE('America/Los_Angeles'), INTERVAL 3 DAY))
SELECT report_date AS pacific_day, video_id, title,
       views AS studio_views,
       ROUND(watch_time_minutes / 60, 2) AS studio_watch_time_hours,
       CAST(ROUND(avg_view_duration_seconds) AS INT64) AS studio_avg_view_duration_seconds,  -- watch time / engaged views, Studio's definition
       impressions AS studio_impressions,
       ROUND(ctr * 100, 1) AS studio_impressions_ctr_pct,
       net_subscribers AS studio_subscribers
FROM `youtube_analytics.video_daily_funnel`, day
WHERE report_date = day.d AND views IS NOT NULL
ORDER BY views DESC
LIMIT 3;
