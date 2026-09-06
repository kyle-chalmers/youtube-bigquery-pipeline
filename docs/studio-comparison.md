# Comparing the warehouse against YouTube Studio

YouTube Studio is the source of truth. This guide says which number in Studio to put next to which
column here, and what will and will not match.

## Three rules

1. **Use Advanced Mode.** In Channel Analytics or a video's Analytics, click "Advanced mode" (top right).
   It gives one row per day or per dimension value, a custom date range, and Export to CSV. The overview
   cards round and blend in ways that make small differences impossible to interpret.
2. **Pick days at least three days old.** A day's report lands about two days after the day, and YouTube
   can regenerate it later with corrections. Both sides settle after about three days.
3. **Days are Pacific time on both sides.** Studio's day boundary is Pacific, and so is `report_date` in
   every `reporting_*` table and view. Set the Studio range to the same Pacific day.

Compare against the Reporting tables and the views built on them, never against `daily_video_analytics`
(it holds only videos with activity on the day and was fetched on a different schedule) and never against
`daily_video_stats.view_count` (a lifetime public counter, counted differently from Analytics views).

## What to compare, easiest first

| # | In YouTube Studio | In BigQuery | Expect |
|---|---|---|---|
| 1 | Channel Analytics, Advanced mode, metric Views, group by Date, 7 complete days | `channel_daily_summary.views` per `report_date` | Exact per day |
| 2 | One video, Reach tab, Impressions and Impressions click-through rate, one day | `video_daily_funnel.impressions`, `.ctr` for that `report_date` | Exact impressions; CTR within Studio's 0.1% rounding |
| 3 | Same video, Reach tab, Traffic source types, 28 days | `video_traffic_detail_daily` summed by `traffic_source_name` over the 28 days | Exact per source; Studio folds tiny sources into Other |
| 4 | Same video, Reach tab, YouTube search terms | `video_traffic_detail_daily` where `surface = 'search'` and `traffic_source_detail IS NOT NULL` | Same top terms. Most search rows carry no term (anonymisation); Studio hides them too |
| 5 | Same video, Reach tab, Suggested videos (traffic from) | `video_traffic_detail_daily` where `surface = 'suggested'`, `referrer_title` when it is one of your videos | Same list |
| 6 | Same video, Engagement tab, Watch time (hours) and Average view duration, 28 days | `SUM(watch_time_minutes)/60`, and AVD recomputed over the range as `SUM(watch_time_minutes)*60 / SUM(views)` from `video_daily_funnel` (never an average of the daily `avg_view_duration_seconds`; the per-engaged variant is `SUM(watch_time_minutes)*60 / SUM(engaged_views)`) | Watch time exact. AVD exact on full-length videos. On Shorts, compare both `avg_view_duration_seconds` and `avg_view_duration_per_engaged_view_seconds`; whichever Studio matches is the definition Studio uses for Shorts, and that result should be recorded here |
| 7 | Same video, Audience tab, Subscribers, 28 days | `SUM(net_subscribers)` from `video_daily_funnel` | Exact; Studio's video-level number is net |
| 8 | Channel Analytics, Audience tab, Subscribers (channel total), 28 days | `SUM(net_subscribers)` from `channel_daily_summary` | Exact only from the summary: about a third of subscribers are channel-level rows with no video, which per-video views cannot see |
| 9 | Audience tab, Watch time from subscribers (percentage) | the watch-time split in `video_audience_growth` (`watch_time_minutes_from_non_subscribers` over the total); `1 - non_subscriber_view_share` is the same idea measured in views and will differ | Exact on the watch-time split |
| 10 | Audience tab, Age and gender; Geography; Device type (Advanced mode dimension) | `channel_demographics`, `country_code` shares from `reporting_channel_basic_a3`, `channel_device_mix_daily` | Same shape once those jobs have data (from about 2026-09-07) |
| 11 | Content tab, Shorts, Views | `channel_daily_summary.shorts_views` | Exact. Studio's "viewed vs swiped away" has no API equivalent; `engaged_start_share` is a different number by design |
| 12 | Any video, Engagement tab, End screen element click rate; Cards | `video_end_screen_daily`, `video_cards_daily` | Exact once those jobs have data |

`sql/verification/phase3_views.sql --studio_spotcheck` prints three videos for the newest day at least three
days old with columns in Studio's Advanced Mode order (Views, Watch time hours, Average view duration,
Impressions, Impressions CTR, Subscribers), so the two can sit side by side on screen.

**Status (2026-09-05): the spot-check has not been performed yet.** It needs YouTube Studio, which the agent
cannot open, so the channel owner runs it. Record the observed differences here when done, including which
AVD definition Studio matched on Shorts.

## What will not match, so nobody chases it

- **Public view count** on the video page versus Analytics views for a date range: different counters.
- **A day that matched yesterday and moved today.** YouTube revises history when it removes invalid traffic.
  The loader picks up the regenerated report and `reporting_ingest_ledger` shows the previous generation as
  `superseded`. Studio moved too.
- **Views in `reporting_channel_traffic_source_a3` versus `reporting_channel_basic_a3`** on the same day can
  differ by up to about 10 percent when the two reports were generated at different times. The views use
  `channel_basic_a3` for views; compare Studio's traffic-source breakdown as shares, not as totals.
- **Average view percentage above 100** on Shorts. Looped playback; the source reports it that way and so
  does Studio's average percentage viewed.
- **Impressions** count YouTube surfaces only (home, search, suggested, subscriptions). A video that gets
  most of its views from external links will show views far above clicks. That is correct.
