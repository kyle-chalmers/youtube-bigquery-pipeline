# Analytics refresh window review (October 2026)

**Question:** should the weekly `youtube-analytics-refresh` change its window (activity dates 6 to 35 days back) or its cadence (Sundays 03:00 Phoenix)?

**Recommendation:** keep the weekly cadence. Keep the 35-day window for now, and re-measure after the 2026-12-27 run. If that check still shows no change older than 24 days, set `REFRESH_TRAILING_DAYS=25` (window 6 to 30 days back). That makes a month pinnable about five days sooner. Changing the cadence would not make months final any sooner.

**Confidence:** medium. The measurement is exact for what it covers, but it covers only seven steady-state refreshes (2026-09-13 to 2026-10-25) on one small channel. No YouTube document states how long Analytics numbers can be revised, so the window has to be justified by measurement, not by a documented limit.

## 1. Why 35 days and why weekly

- **35 is 30 plus the 6-day lookback.** `compute_refresh_window` in `cloud_function/analytics_refresh.py` ends the window `ANALYTICS_LOOKBACK_DAYS` (6) before the run date and covers `REFRESH_TRAILING_DAYS` (30) dates, so it spans ages 6 to 35. Ending at the lookback keeps the refresh out of the days YouTube has not published yet.
- **30 came from a belief, not a source.** The module docstring says YouTube revises Analytics numbers "for up to ~30 days after the fact". The idea first appears in `.planning/plans/reporting-api-hardening-plan.md` item 6 (2026-09-06), and the PR that shipped it (#28, `e80779b`) repeats it. Neither cites a source, and none of the YouTube documentation in section 3 gives a 30-day figure. The nearest documented 30-day numbers belong to the Reporting API: a new job backfills 30 days of history, and historical reports expire 30 days after they are generated. The figure was probably borrowed from those.
- **Weekly was a cost and safety choice.** The same plan item framed weekly as a decision for Kyle. The PR sized it from a staging rehearsal (840.7 s for the full window) and scheduled it on Sundays with zero retries, away from the daily 00:10 run. Production runs from 2026-10-04 to 2026-10-25 took 10.6 to 12.1 minutes.

## 2. What the refreshes actually changed

### Method

Every refresh archives the rows it is about to replace (in `daily_video_analytics_refresh_archive` and `daily_traffic_sources_refresh_archive`) before it writes new ones. For a refresh R and an activity date d:

- **Before** = R's archive rows for d.
- **After** = the archive rows for d from the next refresh whose window covers d, or the current table when no later refresh covers it. Only `gap_repair` can write a date inside a refresh window, and it touches only empty dates, so this rebuilds exactly what R wrote.
- **Age** = R's run date minus d.

Rows are matched on `(activity_date, video_id)`, and on `traffic_source_type` too for traffic. A row that appears only in the after set counts as added, and one that appears only in the before set counts as removed. The queries are in the appendix.

**The first refresh (2026-09-07) is reported separately.** Its before rows were the daily run's first read, and most of those dates were loaded while the lookback was still 3 days (it went to 6 on 2026-08-29). So a difference at "age 30" in that run is everything that changed between age 3 and age 30, with no way to tell when inside that span it happened. Every later run compares against the previous week's refresh, which puts each change inside a 7-day age band.

### Steady state: 7 refreshes, 2026-09-13 to 2026-10-25

`daily_video_analytics`. Each band covers the change since the previous read, which happened 7 days earlier (or was the daily run's day-6 read for the first band).

| Age band (days) | Row pairs | Rows changed | Minutes changed | AVD changed | Subs changed | Abs. minutes change, % of total | Added / removed rows |
|---|---|---|---|---|---|---|---|
| 6 to 12 | 3,064 | 29 | 24 | 7 | 1 | 0.445% | 2 / 9 |
| 13 to 19 | 3,003 | 46 | 8 | 39 | 1 | 0.025% | 1 / 7 |
| 20 to 26 | 2,889 | 5 | 0 | 5 | 0 | 0% | 0 / 1 |
| 27 to 33 | 2,760 | 0 | 0 | 0 | 0 | 0% | 0 / 1 |
| 34 to 35 | 823 | 0 | 0 | 0 | 0 | 0% | 1 / 1 |

`daily_traffic_sources`, summed per activity date:

| Age band (days) | Dates | Dates where views changed | Abs. views change, % of total |
|---|---|---|---|
| 6 to 12 | 49 | 5 | 0.324% |
| 13 to 19 | 49 | 2 | 0.017% |
| 20 to 35 | 161 | 0 | 0% |

Oldest age at which each kind of change appeared:

| Change | Oldest age seen |
|---|---|
| `estimated_minutes_watched` | 16 |
| `subscribers_gained` / `subscribers_lost` | 14 |
| Traffic-source views or minutes | 14 |
| `average_view_duration_seconds` | 24 |
| Added or removed row carrying any minutes or subscribers | never |

What sits behind those numbers:

- **Every change past age 16 is AVD-only.** The five rows changed in the 20 to 26 band all came from the 2026-09-27 run. On rows with at least 10 watch minutes, AVD moved by at most 10 seconds, and minutes did not move at all. Changes like this, where AVD shifts but minutes do not, cluster in the 2026-09-27 and 2026-10-11 runs. That looks like YouTube recounting the AVD denominator in a batch, not a steady trickle of revisions.
- **Every added or removed row in steady state had zero minutes.** These are zero-activity rows that the API sometimes returns and sometimes leaves out. None of them changes a total.
- **`shares` never changed.**
- **The 13 to 19 band has AVD movement but almost no minutes or views movement.** Of its 39 AVD changes, 27 are on rows with at least 10 minutes. The largest move on such a row was 166 seconds, on a video with only a few views that day.

### First refresh (2026-09-07), reported separately

Comparing first reads (mostly at age 3) with the values at ages 13 to 35: rows changed in every age band, and traffic views fell by 0.5% to 3.4% of the total per band. That shows revisions happen somewhere between day 3 and day 35. It does not show when. The steady-state runs answer that, and they place all minutes and views changes by day 16.

### Check against the claim that September stopped changing after 2026-10-04

The claim mostly holds. For September 2026 activity dates:

| Refresh | September dates rewritten | Rows changed | Minutes changed | Dates where traffic views changed |
|---|---|---|---|---|
| 2026-09-13 | 7 | 6 | 3 | 3 |
| 2026-09-20 | 14 | 29 | 18 | 4 |
| 2026-09-27 | 21 | 34 | 4 | 0 |
| 2026-10-04 | 28 | 4 | 3 | 0 |
| 2026-10-11 | 25 | 13 | 0 | 0 |
| 2026-10-18 | 18 | 0 | 0 | 0 |
| 2026-10-25 | 11 | 0 | 0 | 0 |

The 2026-10-11 changes were AVD-only or zero-minute rows on September 25 to 28 (ages 13 to 16). No September minutes or views changed after 2026-10-04.

### Cross-check: late Reporting API reissues

The Reporting ledger shows YouTube sometimes issues a replacement `channel_basic_a3` report weeks later. For example, the second report for 2026-09-11 arrived at age 34, and the one for 2026-09-13 at age 31. Both cut views by about 5% compared with the first report (which was generated at age 1). That could look like a revision at day 30-plus that a shorter window would miss. It was not. The Analytics API already returned the revised view totals at ages 9 and 7, exactly matching the late Reporting files, and no later refresh changed those dates. The revision happened early. Only the Reporting file arrived late. Since September, the typical pattern is a first report at age 1 to 2 and a replacement at age 4 to 6.

## 3. What YouTube documents

- **Latency:** "Data processing typically introduces a latency of 48 to 72 hours." Responses stop at the last day for which every queried metric is available. ([Analytics API data model](https://developers.google.com/youtube/analytics/data_model))
- **Revisions:** the Reporting API says a backfill "replaces a previously delivered set" and that clients should update stored data to match. It gives no deadline for when a backfill can arrive. Reports stay available for 60 days, or 30 days for historical ones. ([Reporting API reports](https://developers.google.com/youtube/reporting/v1/reports))
- **Invalid traffic:** some invalid traffic "can only be detected by our systems after it happens", and Analytics can be adjusted to remove it. The only time limit stated is that payments "may be delayed up to 90 days". ([Invalid traffic on your videos](https://support.google.com/youtube/answer/10285842))
- The limited-data help page gives no latency or finality guidance. ([Understand limited data](https://support.google.com/youtube/answer/9101241))

**Bottom line:** YouTube documents a 2 to 3 day latency and states that revisions happen. It never says how long they can go on. The 30-day figure in our code is an assumption, and the 90-day payment hold means YouTube reserves the right to revise later than any window we choose.

## 4. Trade-offs of the alternatives

A month's last day D can be pinned once no future refresh window reaches it. With weekly runs, the last refresh touching D happens between D + (window end - 6) and D + window end, depending on the weekday.

| Option | Pinnable after month end | Re-reads each date at least once at age ≥ | API calls and run time | Risk |
|---|---|---|---|---|
| **Keep: weekly, ages 6 to 35** | 29 to 35 days (September: 2026-11-01, day 32) | 29 | Baseline: ~11 to 12 min, 30 daily video-analytics calls plus one traffic range query | None new |
| Weekly, ages 6 to 30 (`REFRESH_TRAILING_DAYS=25`) | 24 to 30 days | 24, the oldest change seen | About 17% fewer per-day calls | A revision older than 24 days would be missed until a manual `setup/backfill_analytics.py` run. None has been seen in 7 runs. |
| Weekly, ages 6 to 27 (`=22`) | 21 to 27 days | 21 | About 27% fewer | Would have missed the 2026-09-27 AVD batch at age 24. Not recommended. |
| Daily, same window | Exactly 35 days | 35 | 7 times the runs and Analytics API calls (that quota is undocumented). Holds the `analytics-writer` lease for about 11 minutes every day, which has to stay clear of the 00:10 run. | Pins later on average than weekly. More fetches catch nothing new, because changes past day 16 are rare. |
| Monthly finalization run (refresh the whole month at D + N, then mark it final) | N days | Varies: the first day of the month gets read at N + 30 | New code, plus a fourth Scheduler job (the billing account already uses its 3 free jobs) | More moving parts to get roughly what option 2 gives |

Cost is not a deciding factor for any option, since every option stays at a few cents a month or less. The real trade is between how soon a month can be pinned and the risk of silently freezing a revised number.

**Effect on the canary:** `youtube-semantic-layer` pins only months that no refresh can rewrite, so the rule keeps working under any of these options. Only the wait changes: 29 to 35 days today, 24 to 30 days with option 2. The rule needs no change either way.

## 5. Why keep for now instead of shortening today

- Seven runs is a small sample. It includes one batch AVD recount (2026-09-27) that reached age 24, which is exactly the edge option 2 would keep.
- The gain is five days on a monthly pin. The cost of a wrong cut is a revision frozen in place with nothing to flag it.
- `daily_video_analytics` has no views column, so view revisions are measured only through `daily_traffic_sources`. Both tables agree that nothing changes after day 16.

**Re-measure trigger:** after the 2026-12-27 run, which brings the count to about 15 steady-state runs including a month end and holiday traffic, re-run the appendix queries. Shorten to `REFRESH_TRAILING_DAYS=25` if no change in minutes, views or subscribers appears past age 16 and no AVD change appears past age 24. Otherwise keep 35.

**Related fix (separate change):** the "~30 days" sentence in the `analytics_refresh.py` docstring and in CLAUDE.md should say the window comes from measurement, and point to this document, so the unsourced claim stops spreading.

## Appendix: queries

Run them in the deployed project (`GCP_PROJECT`), dataset `youtube_analytics`. They are read-only. A refresh's run date is recovered as `MAX(activity_date) + ANALYTICS_LOOKBACK_DAYS` from its archive rows, which matches the run dates in the Cloud Function logs.

```sql
-- daily_video_analytics: before/after pairs per refresh, by age
WITH runs AS (
  SELECT refresh_run_id, DATE_ADD(MAX(activity_date), INTERVAL 6 DAY) AS run_date,
         MIN(activity_date) AS wstart, MAX(activity_date) AS wend
  FROM `PROJECT.youtube_analytics.daily_video_analytics_refresh_archive` GROUP BY 1
),
versions AS (  -- state of each date as seen just before each run; current table = latest
  SELECT r.run_date AS vdate, a.activity_date, a.video_id, a.estimated_minutes_watched emw,
         a.average_view_duration_seconds avd, a.subscribers_gained sg, a.subscribers_lost sl, a.shares sh
  FROM `PROJECT.youtube_analytics.daily_video_analytics_refresh_archive` a JOIN runs r USING (refresh_run_id)
  UNION ALL
  SELECT DATE '9999-12-31', activity_date, video_id, estimated_minutes_watched,
         average_view_duration_seconds, subscribers_gained, subscribers_lost, shares
  FROM `PROJECT.youtube_analytics.daily_video_analytics`
  WHERE activity_date >= (SELECT MIN(wstart) FROM runs)
),
cover AS (  -- each (run, date) rewritten, and which version holds that run's output
  SELECT r.run_date, d AS activity_date,
         COALESCE((SELECT MIN(r2.run_date) FROM runs r2
                   WHERE r2.run_date > r.run_date AND d BETWEEN r2.wstart AND r2.wend),
                  DATE '9999-12-31') AS after_vdate
  FROM runs r, UNNEST(GENERATE_DATE_ARRAY(r.wstart, r.wend)) d
),
bef AS (SELECT c.run_date, c.activity_date, v.* EXCEPT (vdate, activity_date)
        FROM cover c JOIN versions v ON v.vdate = c.run_date AND v.activity_date = c.activity_date),
aft AS (SELECT c.run_date, c.activity_date, v.* EXCEPT (vdate, activity_date)
        FROM cover c JOIN versions v ON v.vdate = c.after_vdate AND v.activity_date = c.activity_date),
pairs AS (
  SELECT run_date, activity_date, video_id, DATE_DIFF(run_date, activity_date, DAY) AS age,
         b.emw IS NULL AND b.avd IS NULL AND b.sg IS NULL AS added,
         a.emw IS NULL AND a.avd IS NULL AND a.sg IS NULL AS removed,
         b.emw b_emw, a.emw a_emw, b.avd b_avd, a.avd a_avd,
         b.sg b_sg, a.sg a_sg, b.sl b_sl, a.sl a_sl, b.sh b_sh, a.sh a_sh
  FROM bef b FULL OUTER JOIN aft a USING (run_date, activity_date, video_id)
)
SELECT run_date > '2026-09-07' AS steady_state,
       CAST(FLOOR((age - 6) / 7) AS INT64) * 7 + 6 AS age_from,
       COUNT(*) AS row_pairs, COUNTIF(added) AS added, COUNTIF(removed) AS removed,
       COUNTIF(NOT added AND NOT removed AND (b_emw != a_emw OR b_avd != a_avd
               OR b_sg != a_sg OR b_sl != a_sl OR b_sh != a_sh)) AS changed,
       COUNTIF(NOT added AND NOT removed AND b_emw != a_emw) AS minutes_changed,
       COUNTIF(NOT added AND NOT removed AND b_avd != a_avd) AS avd_changed,
       ROUND(100 * SUM(ABS(IFNULL(a_emw, 0) - IFNULL(b_emw, 0))) / SUM(IFNULL(b_emw, 0)), 3) AS abs_minutes_pct
FROM pairs GROUP BY 1, 2 ORDER BY 1, 2;
```

The traffic-source version uses the same `runs` / `cover` structure on `daily_traffic_sources_refresh_archive` and `daily_traffic_sources`, aggregated to `SUM(views)` and `SUM(estimated_minutes_watched)` per `(version, activity_date)` before the before/after join.

```sql
-- Reporting API: generations per report_date and the age each was created at
SELECT report_date,
       STRING_AGG(CAST(DATE_DIFF(DATE(report_create_time, 'America/Los_Angeles'), report_date, DAY) AS STRING)
                  || ':' || status, ' | ' ORDER BY report_create_time) AS generations
FROM `PROJECT.youtube_analytics.reporting_ingest_ledger`
WHERE report_type = 'channel_basic_a3'
GROUP BY 1 ORDER BY 1;
```
