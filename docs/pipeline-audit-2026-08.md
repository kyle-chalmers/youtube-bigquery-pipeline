# Pipeline Audit Evidence

A correctness audit of this pipeline run against live BigQuery data on 2026-08-02.
Each finding carries the query that proves it.

Generated live against `<your-gcp-project-id>.youtube_analytics` on 2026-07-25. Every number below came from a query, not from reading code.

Frozen pre-fix copy: `<your-gcp-project-id>.youtube_analytics_broken_20260725` (verified to reproduce the offset bug).

## 1. Table freshness and shape

| table | min | max | rows | days | distinct videos |
|---|---|---|--:|--:|--:|
| `video_metadata` | 2026-02-17 | 2026-07-25 | 18,583 | 159 | 156 |
| `daily_video_stats` | 2026-02-17 | 2026-07-25 | 18,583 | 159 | 156 |
| `daily_video_analytics` | 2025-10-16 | 2026-07-25 | 7,045 | 274 | 162 |
| `daily_traffic_sources` | 2025-10-16 | 2026-07-25 | 18,798 | 277 | 153 |

Asymmetric history: analytics and traffic reach back to 2025-10-16 via the backfill; metadata and stats only start 2026-02-17 when the daily function was deployed.

## 2. The snapshot_date defect (headline finding)

`cloud_function/main.py:121` computes `analytics_date = snapshot_date - 3`, fetches that date, then `main.py:183` writes those rows stamped with `snapshot_date` (today). `setup/4_deploy_function.sh:29` never sets `ANALYTICS_LOOKBACK_DAYS`, so the `main.py:37` default of 3 is live.

Empirical proof: for videos published after the daily function went live, the first analytics row lands about 3 days after publish.

> **Window note (added 2026-08-02).** The 19 rows below came from a wider window than the one the video actually runs. BLOCK 1, the query on screen, bounds publish dates to `BETWEEN 2026-06-01 AND 2026-07-20` and returns **18 rows, 14 of them at +3** — re-verified live on 2026-08-02. Where this file and the script disagree on the count, the script matches what the audience sees and this table is the stale one.

| published | first analytics row | offset (days) | title |
|---|---|--:|---|
| 2026-07-19 | 2026-07-22 | **+3** | Make Your AI Agents Check Each Other |
| 2026-07-16 | 2026-07-19 | **+3** | Apache Iceberg Explained (Is It Here to Stay |
| 2026-07-14 | 2026-07-18 | **+4** | Never Bounce Between Windows For A Data Tick |
| 2026-07-11 | 2026-07-14 | **+3** | Reduce Redshift Query Costs 16x With Claude  |
| 2026-07-09 | 2026-07-11 | **+2** | DataGrip Is Now Free: An AI Data Analysis Wo |
| 2026-07-03 | 2026-07-08 | **+5** | Stack Multiple AI Reviewers Against Each Oth |
| 2026-07-01 | 2026-07-04 | **+3** | How Redshift Actually Moves Your Data (Distr |
| 2026-06-29 | 2026-07-01 | **+2** | Claude Code Reads Your Amazon Redshift Query |
| 2026-06-17 | 2026-06-20 | **+3** | The 1-command fix for Claude Code sessions # |
| 2026-06-15 | 2026-06-18 | **+3** | Save and Resume Any Claude Code Session | Ne |
| 2026-06-10 | 2026-06-13 | **+3** | The fastest way to merge CSV data with AI #d |
| 2026-06-09 | 2026-06-12 | **+3** | The fastest way to analyze CSV data #duckdb  |
| 2026-06-08 | 2026-06-11 | **+3** | Query CSVs and More in Plain English with Du |
| 2026-06-07 | 2026-06-10 | **+3** | How to hide sensitive data from AI agents #t |
| 2026-06-07 | 2026-06-10 | **+3** | Hashing and masking PII for AI safely #snowf |
| 2026-06-06 | 2026-06-09 | **+3** | Is your data safe when you give it to AI? #D |
| 2026-06-04 | 2026-06-07 | **+3** | Obtain Better AI Data Security in 3 Steps #t |
| 2026-06-02 | 2026-06-05 | **+3** | The 4-Tier Framework Your AI & Data Team Nee |
| 2026-06-01 | 2026-06-04 | **+3** | AI Retention Policies Are Confusing But This |

The backfill wrote the true metric date, so `snapshot_date` carries two incompatible meanings either side of a mid-February seam.

## 3. Missing days (no gap detection, no self-healing)

9 analytics days (this counts missing STAMPS; against metric_date after the rung-1 migration it is 7, because 2026-02-16/17 have no stamp but their activity sits in the rows stamped 02-19/20 — the script says seven and is correct) are permanently absent:

- `2025-10-22`
- `2025-10-23`
- `2026-02-16`
- `2026-02-17`
- `2026-05-23`
- `2026-05-24`
- `2026-07-06`
- `2026-07-07`
- `2026-07-17`

2026-07-17 is the instructive one: stats and metadata wrote normally that day, so the analytics failure was completely silent.

## 4. Coverage: analytics is NOT one row per video per day

| date | analytics | stats | metadata | traffic |
|---|--:|--:|--:|--:|
| 2026-07-16 | 44 | 149 | 149 | 120 |
| 2026-07-17 | 0 | 150 | 150 | 103 |
| 2026-07-18 | 40 | 150 | 150 | 98 |
| 2026-07-19 | 42 | 150 | 150 | 101 |
| 2026-07-20 | 41 | 151 | 151 | 104 |
| 2026-07-21 | 37 | 151 | 151 | 86 |
| 2026-07-22 | 41 | 152 | 152 | 104 |
| 2026-07-23 | 43 | 153 | 153 | 94 |
| 2026-07-24 | 47 | 154 | 154 | 121 |
| 2026-07-25 | 47 | 155 | 155 | 118 |

The Analytics API returns only videos with activity, so an inner join on this table silently drops roughly two thirds of the catalogue.

## 5. Pagination cliff

- Videos in latest snapshot: **155**
- `maxResults=200` with no `nextPageToken` handling (`youtube_analytics_api.py:101`, `setup/backfill_analytics.py:96`)
- Headroom: ~~**45 videos**~~ **SUPERSEDED 2026-08-02.** That figure compared the catalogue (155) against a cap that does not apply to it. `youtube_analytics_api.py:101` is the Analytics `reports.query`, which returns only videos with activity on the day (37-47 per section 4), so the 200 cap is years away, not 45 videos. `youtube_data_api.py:42`, the call that lists the catalogue, paginates correctly via `pageToken`/`nextPageToken`. The script states the corrected version.

## 6. Always-NULL columns (documented, not a defect)

Across all 7,045 analytics rows: impressions=0, impression_ctr=0, card_click_rate=0, annotation_ctr=0 non-null.

`youtube_analytics_api.py:130` documents why. The real problem is `sql/create_tables.sql` still declaring them, so anything reading the schema assumes they hold data. **Deferred: closing this needs a new data source, tracked in `.internal/NEXT-SESSION-reporting-api-jobs.md`.**

## 7. Confirmed healthy (do not 'fix')

- duplicate (snapshot_date, video_id) in analytics: **0**
- duplicate (snapshot_date, video_id) in metadata: **0**
- missing days in daily_video_stats since 2026-02-17: **0**

DELETE-then-INSERT works whenever it completes. The atomicity risk is real but has not yet bitten.
