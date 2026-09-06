# YouTube BigQuery Pipeline — Project Context

## Project Purpose

Daily automated pipeline that snapshots YouTube channel analytics into BigQuery for historical trend analysis. Captures content performance, engagement, watch time, and traffic source data that YouTube Studio surfaces visually but does not expose for SQL-style querying.

> **Public repo.** Everything here is published on GitHub. Anything personal, operational, or secret (specific project IDs, channel-specific stats, deployment notes) belongs in `.internal/OWNER_CONFIG.md` (gitignored). Anything reproducible for any reader belongs in the public files.

## YouTube channel identity (configurable)

- **Channel handle / ID:** set via `YOUTUBE_CHANNEL_ID` env var (UC-prefixed)
- **Uploads playlist:** set via `UPLOADS_PLAYLIST_ID` (always the channel ID with `UC` → `UU`)
- **Shorts threshold:** 180 seconds (configurable via `SHORTS_THRESHOLD_SECONDS`)

For the channel this pipeline is deployed against, see `.internal/OWNER_CONFIG.md`.

## Environment & Authentication

- **YOUTUBE_API_KEY:** YouTube Data API v3 key (set in shell env or `.env`)
- **YOUTUBE_CHANNEL_ID:** UC-prefixed channel ID (shell env or `.env`)
- **gcloud CLI:** authenticated to the deploy account
- **GCP Project:** read from `GCP_PROJECT` env var or active gcloud config — never hardcoded
- **BigQuery dataset name:** `youtube_analytics` by default (override via `BQ_DATASET`)
- **Default region:** `us-central1` (override via `GCP_REGION`)

A complete config template lives at `.env.example`. Owner-specific real values live in `.internal/OWNER_CONFIG.md`.

## Validated API Patterns

These API calls have been tested and confirmed working in this environment:

**Fetch channel info:**

```bash
curl -s "https://www.googleapis.com/youtube/v3/channels?part=snippet&id=$YOUTUBE_CHANNEL_ID&key=$YOUTUBE_API_KEY"
```

**Fetch video details (snippet + contentDetails + statistics):**

```bash
curl -s "https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails,statistics&id={comma-separated-ids}&key=$YOUTUBE_API_KEY"
```

**Key API facts:**

- `publishedAt` is in `snippet` (not `contentDetails`)
- Duration is ISO 8601 format in `contentDetails.duration` (e.g., `PT12M34S`)
- Pagination uses `nextPageToken` — max 50 results per request
- Full-length videos and shorts are both accessible via the uploads playlist

## Tech Stack

- **Runtime:** Python 3.11+ on Google Cloud Functions (2nd gen)
- **Data warehouse:** Google BigQuery
- **Scheduler:** Google Cloud Scheduler
- **Secrets:** Google Cloud Secret Manager
- **APIs:** YouTube Data API v3, YouTube Analytics API v2, YouTube Reporting API v1
- **Libraries:** `google-cloud-bigquery`, `google-api-python-client`, `google-auth`, `google-cloud-logging`, `google-cloud-secret-manager`, `google-cloud-storage`, `requests`
- **Tests:** `python3 -m pytest tests/ -q` (offline, no credentials; CI runs it on every push). A project venv is the expected way to run them: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

## Cloud Function Configuration

Two functions, one source directory (`cloud_function/`), one service account.

- **`youtube-bigquery-pipeline`** (daily pipeline): entry point `main` in `main.py`; env `GCP_PROJECT`, `BQ_DATASET`, `YOUTUBE_CHANNEL_ID`, `UPLOADS_PLAYLIST_ID`, `PIPELINE_TZ`, `ANALYTICS_LOOKBACK_DAYS`, `GAP_LOOKBACK_DAYS`, `MAX_GAP_REPAIRS_PER_RUN` (the deploy script sets every one explicitly). Scheduler `youtube-daily-snapshot`, 00:10 America/Phoenix (moved from 23:50 on 2026-09-05 so the run is first in the Data API quota day, not last).
- **`youtube-reporting-ingest`** (Reporting API, live in prod since 2026-09-06): entry point `reporting_main` in `reporting_main.py`; env `GCP_PROJECT`, `BQ_DATASET`, `YOUTUBE_CHANNEL_ID`, `REPORTING_ENABLED` (kill switch, default false), `MAX_REPORTS_PER_RUN` (30), `REPORTING_STALE_DAYS` (4), `REPORTING_ARCHIVE_BUCKET`. Scheduler `youtube-reporting-daily`, 08:00 and 14:00 America/Phoenix. Deployed by `setup/9_deploy_reporting_function.sh`.
- **Staging copies:** `youtube-bigquery-pipeline-staging` and `youtube-reporting-ingest-staging`, pointed at `youtube_analytics_staging`, no scheduler. The deploy scripts refuse both directions of a prod/staging cross.
- **Runtime:** Python 3.11, 2nd gen, Memory: 512MB, Timeout: 540s (9 min), both functions
- **Secrets (from Secret Manager):** `youtube-data-api-key`, `youtube-oauth-client-id`, `youtube-oauth-client-secret`, `youtube-oauth-refresh-token`. One refresh token serves the Analytics and Reporting APIs (same scope).
- **IAM roles needed:** `cloudbuild.builds.builder`, `secretmanager.secretAccessor`, `bigquery.dataEditor`, `bigquery.jobUser`, plus `storage.objectAdmin` on the archive bucket only

## Key Code Patterns

- **Idempotent writes:** DELETE + batch load (not streaming inserts) — avoids BigQuery streaming buffer consistency issues. The analytics tables key the DELETE on `activity_date`, NOT `snapshot_date`. Recovered rows share one collection date, so a snapshot-keyed delete would erase them all. The DELETE also runs only after rows are in hand; deleting first destroyed three days of history on 2026-05-25.
- **Structured logging:** JSON via `google.cloud.logging`, each run tagged with `run_id` (UUID prefix)
- **Graceful degradation:** Analytics API failure does not crash the pipeline; Data API tables always populated
- **Exponential backoff:** 2^attempt seconds, max 3 retries, on `{429, 500, 502, 503, 504}`. Retrying only 429 silently dropped a video's traffic on the first transient 500.
- **Traffic sources:** require per-video calls (can't batch); video analytics is a single call for all videos
- **Lookback window:** `ANALYTICS_LOOKBACK_DAYS = 5`. It was 3, which is exactly the edge of availability (T-0/T-1/T-2 return nothing, T-3 is the first populated day), so any extra day of latency produced an empty result.
- **Self-healing gaps:** each run re-queries activity dates with no rows, within `GAP_LOOKBACK_DAYS` (21), up to `MAX_GAP_REPAIRS_PER_RUN` (5), tagged `load_source='gap_repair'`. Covers `daily_video_analytics` only; traffic-source gaps do NOT self-heal.
- **Run date:** `PIPELINE_TZ` (America/Phoenix). Cloud Run is UTC and the job fired at 23:50 local, so `date.today()` stamped every row with the next day's date. Since 2026-09-05 the job fires at 00:10 local and stamps that calendar day; the analytics lookback is measured from it.
- **200-row cap:** the unfiltered video report is a capped top-N report. `startIndex` does not page past it and must never be reintroduced. On hitting the cap the client re-fetches via `filters=video==` shards. Full detail in the comment block atop `youtube_analytics_api.py`.
- **Shorts threshold:** `SHORTS_THRESHOLD_SECONDS = 180`, a module constant in `youtube_data_api.py`. Despite appearing in `.env.example`, it is NOT read from the environment.
- **One credential loader, one retry policy:** `oauth_credentials.load_oauth_credentials` and `retry.with_retry` are the only copies. `retry.py` is stdlib-only on purpose: `main.py` skips the analytics path on `ImportError`, so nothing imported on that path may carry a dependency that could fail. The attempt count is always the caller's (function 3, backfill 5).
- **Reporting ingest (`reporting_loader.py`, `partition_replacer.py`):** every run lists every retained report and diffs against `reporting_ingest_ledger`; there is no watermark, so a failed report is retried next run. Newest `createTime` per day wins, enforced inside the transaction. Every downloaded body goes to the GCS archive before parsing. Parse by header name (`reporting_parser.py`); an unknown or missing column raises `SchemaDriftError`. A header-only report never deletes; over a populated day it becomes `header_only_conflict` and alerts. The only way to empty a populated day is `setup/backfill_reporting.py --allow-empty-replace`.
- **Schemas are generated:** `report_specs.py` is the registry; `setup/generate_reporting_ddl.py` renders `sql/reporting_tables.sql`; a test fails if they differ. Grain columns other than `report_date` and `channel_id` are nullable (`channel_basic_a3` has a channel-level row with no `video_id`).
- **Reporting source semantics** (observed): AVD percentage is 0..100 and exceeds 100 on looped Shorts; CTR is a 0..1 fraction; likes can be negative; traffic_source_type is a numeric code; `channel_basic_a3` has channel-level subscriber rows with NULL video_id holding about a third of subscribers gained; basic_a3 and traffic_source_a3 disagree on daily views by up to ~10% when generated at different times, so basic_a3 is the source of truth for views. Full list in README's Reporting schema section.
- **Views changed meaning on 2026-08-24** (every format counts a view from the first frame; Shorts since 2025-03-31; history not restated). Raw rows are correct for their day, but a views trend crossing that date compares two definitions (+36% views/day, minus 13% engaged views/day on this channel). Use `engaged_views` and the `*_engaged_*` ratio columns for anything spanning the boundary; Studio's AVD, CTR and retention are engaged-based.
- **Growth views (`sql/views/`, 12 files):** created by `setup/10_create_views.sh`, asserted by `scripts/verify_views.sh`. Rules: aggregate to the target grain before any join; recompute ratios from totals, never AVG a source ratio (`channel_demographics` is the documented exception); only `video_current` reads `video_metadata`. Every header states Grain, Cardinality, Timezone, Source; `tests/test_views_sql.py` enforces it. AVD is exposed twice: `avg_view_duration_seconds` is watch time over engaged views, which is what YouTube Studio shows and documents (spot-check 2026-09-02; help article 12220281), and `avg_view_duration_over_views_seconds` is the older definition that coincided with it until YouTube unified view counting on 2026-08-24 (views count from the first frame for every format; long-form engaged/view on this channel fell from 0.998 to 0.64 that day).
- **Source of truth for views:** the Reporting tables (`reporting_channel_basic_a3` for views, `reporting_channel_traffic_source_a3` for the source split) from 2026-06-15 / 2026-07-31 onward; the Analytics tables before that. Reporting days are replaced automatically when YouTube issues a backfill report (newest generation wins), but YouTube does not issue one for every revision, and the Analytics tables are never re-queried for a day that already has rows except by `setup/backfill_analytics.py`. Neither source re-reads Studio's live counters.
- **Alert strings are a contract:** `setup/6_setup_monitoring.sh` matches literal log strings; `tests/test_main.py` and `tests/test_reporting_alerts.py` pin them. Change both sides in the same commit.

## Cost Expectations

Everything runs within GCP free tier on a personal-channel scale:

- Cloud Functions: 2M free invocations/month (this pipeline uses 1/day = ~30)
- Cloud Scheduler: 3 free jobs (this pipeline uses 1)
- BigQuery: 10GB storage + 1TB queries free (snapshot data is tiny)
- YouTube Data API: 10,000 units/day quota (this pipeline uses ~4 per run)

## What Claude Code Has Access To

For any AI agent (Claude Code, Codex, Gemini CLI, etc.) working in this repo:

- **Shell:** full terminal access — `gcloud`, `bq`, `curl`, `python3`, `git`, any CLI tool
- **File system:** read/write to the repo and local config files (e.g., `~/.zshrc`)
- **gcloud CLI:** runs under the active gcloud account — can enable APIs, create BigQuery resources, deploy Cloud Functions, manage Scheduler jobs (subject to that account's IAM)
- **bq CLI:** can create datasets, tables, and run queries
- **YouTube Data API v3:** via `YOUTUBE_API_KEY` env var
- **YouTube Analytics API:** via OAuth2 — refresh token + client credentials stored in Secret Manager
- **Git:** can stage, commit, manage the local repo (pushes require human approval)
- **No access to:** GCP Console UI, browser-based OAuth flows, or the YouTube Studio dashboard. The owner handles those manually when needed.

## Documentation

`README.md` is the deployment guide and reference; the original build log moved to `docs/build-log.md`. Keep README and this file in sync with any change to the pipeline.

## Current Deployment Status

Pipeline is fully deployed and operational.

- **APIs enabled:** BigQuery, YouTube Data API v3, YouTube Analytics API, Cloud Functions, Cloud Scheduler, Secret Manager, Cloud Build, Cloud Run, Cloud Storage, Logging, Monitoring
- **BigQuery dataset:** `youtube_analytics`. Four original tables (`video_metadata`, `daily_video_stats`, `daily_video_analytics`, `daily_traffic_sources`), two pre-migration archives (`daily_video_analytics_v1_archive`, `daily_traffic_sources_v1_archive`), 19 Reporting API raw tables (`reporting_<report_type>`), and `reporting_ingest_ledger`. `youtube_analytics_staging` mirrors all of it for rehearsal.
- **Cloud Storage:** `<project>-youtube-reporting-raw` holds every Reporting API CSV ever downloaded (`<report_type>/<report_date>/<report_id>.csv.gz`, with sha256 and row-count metadata). It is the replay source for anything YouTube has since expired.
- **Reporting API jobs:** 19, one per non-retired channel and playlist report type (annotations skipped, retired 2019). Ids in `.internal/OWNER_CONFIG.md`. The analytics tables are partitioned by `activity_date` and clustered by `video_id`; the two Data API tables remain on `snapshot_date`. Every analytics row carries `load_source` (`cron`, `backfill_YYYYMMDD`, `recovery_YYYYMMDD`, `gap_repair`), which is the only way to tell writers apart.
- **Cloud Functions:** `youtube-bigquery-pipeline` (refactor promoted 2026-09-06, `ANALYTICS_LOOKBACK_DAYS=6`) and `youtube-reporting-ingest` (promoted 2026-09-06, `REPORTING_ENABLED=true`, scheduler `youtube-reporting-daily` at 08:00 and 14:00 Phoenix), both 2nd gen, Python 3.11, 512MB, 540s. The four alert policies exist for production; the `-staging` copies stay until the staging soak is closed.
- **Cloud Scheduler:** runs daily at 00:10 Phoenix time (`America/Phoenix`, no DST), ten minutes after the Data API quota resets
- **OAuth2:** refresh token + client credentials stored in Secret Manager

Operational specifics (initial deploy date, channel stats at build, current ingestion health) live in `.internal/OWNER_CONFIG.md`.

## Known Limitations

- `impressions` and `impression_ctr` in `daily_video_analytics` are always `NULL` and are deprecated columns: the Analytics API does not expose them (probed 2026-05-25). Impressions and CTR live in `reporting_channel_reach_basic_a1` (per video per day, from 2026-07-31) and, by traffic source and device, in `reporting_channel_reach_combined_a1`. Read them from there; the NULL columns are kept only so existing queries do not break.
- `annotation_click_through_rate` always `NULL` — YouTube retired annotations in 2019.
- `card_click_rate` in `daily_video_analytics` is always `NULL` because the Analytics path never queries it. The channel DOES use cards (Reporting data 2026-09-05: 592 card impressions, 2,184 teaser impressions, 10 clicks over five weeks); card metrics live in `reporting_channel_basic_a3` and `reporting_channel_cards_a1`.
- The OAuth app uses the sensitive `yt-analytics.readonly` scope. Refresh tokens were previously expiring on a ~7-day cycle; the current token (Secret Manager version created 2026-05-25) has survived **90+ days**, so that cap no longer appears to apply. Rotation procedure lives in `.internal/REFRESH_TOKEN_ROTATION.md`.
- **Corrected 2026-07-26 — do not attribute missing analytics days to token expiry.** This file previously claimed the pipeline silently writes 0 analytics rows because the refresh token dies. Cloud Function logs disprove that for the 2026-07-07 and 2026-07-17 runs. Verbatim from 2026-07-17: `Got analytics for 0 videos (date: 2026-07-14)`, then `Deleted existing rows`, then `No rows to insert` — all at severity INFO, no error raised. Traffic sources fetched 103 rows on the same run seconds later using the same credentials, so auth was healthy.
- **Causes of missing analytics days (there is more than one).** (1) The lookback sat at exactly the edge of availability, so a day of extra latency returned an empty set. Confirmed by live probe 2026-08-29 and fixed by raising the lookback to 5. (2) A single metric in the six-metric query can zero the whole response; a Google engineer confirmed this on issuetracker 552694602, alongside not owning the video and privacy thresholds. Cause 2 was ruled out for the specific day probed, NOT in general. Do not write this up as a single cause. Both are now mitigated by the self-healing re-query rather than by any assumption about which one fired.
- **Recovered activity dates (2026-08-29):** 2026-02-22/23/24 (destroyed by the 2026-05-25 backfill overwrite), plus 07-03, 07-04, 07-14, 08-11 (never collected). 2025-10-22/23 remain empty and are genuine zero-activity days, not gaps.
- **2026-08-11 was never collected because of Data API quota, not the Analytics API.** The 2026-08-14 run failed on its first `playlistItems.list` call with 403 `quotaExceeded`: another tool in the same GCP project had spent the daily 10,000 units, and the pipeline fires ten minutes before the Pacific-midnight reset. The 2026-08-29 recovery of that day fetched only 37 of 52 videos' traffic (per-video failures are swallowed as warnings); a second recovery on 2026-09-05 completed it and matches the Reporting API exactly. A whole-pipeline crash logs `Pipeline failed`, which the analytics alert now matches.
- **Never log an exception message or traceback raw.** `HttpError` messages embed the request URL, and the Data API key rides in that URL; four Cloud Logging entries carried it. Pass messages through `log_safety.redact` (as `main.py` does) before logging.
- **Delete-before-insert:** fixed in `cloud_function/bigquery_writer.py` and in `setup/backfill_analytics.py` (both now check for rows first and key the delete on `activity_date`). This hazard destroyed activity 2026-02-22/23/24. If you write any new loader, preserve both properties.
- Analytics API quota is not publicly documented like the Data API's 10,000-unit system
- Recent Google docs suggest `youtube.readonly` scope may now be required alongside `yt-analytics.readonly` — current single-scope config still works but worth monitoring

## Additional API Fields Not Yet Captured

**YouTube Data API v3:** `description`, `defaultLanguage`, `defaultAudioLanguage`, `liveBroadcastContent`, `topicCategories`, `definition`, `caption`

**YouTube Analytics API v2 (additional metrics):** `annotationCloseRate`, `cardImpressions`, `cardClicks`, `audienceWatchRatio`, `likes`/`dislikes`

**YouTube Analytics API v2 (new dimensions — would need new tables):** `ageGroup`/`gender` (demographics, requires additional OAuth scope), `country`/`province` (geography), `insightPlaybackLocationType` (playback location)
