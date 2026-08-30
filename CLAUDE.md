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
- **APIs:** YouTube Data API v3, YouTube Analytics API v2
- **Libraries:** `google-cloud-bigquery`, `google-api-python-client`, `google-auth`, `google-cloud-logging`, `google-cloud-secret-manager`

## Cloud Function Configuration

- **Function name:** `youtube-bigquery-pipeline`
- **Runtime:** Python 3.11, 2nd gen, Memory: 512MB, Timeout: 540s (9 min)
- **Entry point:** `main` function in `cloud_function/main.py`
- **Environment variables:** `GCP_PROJECT`, `BQ_DATASET`, `YOUTUBE_CHANNEL_ID`, `UPLOADS_PLAYLIST_ID`
- **Secrets (from Secret Manager):** `youtube-data-api-key`, `youtube-oauth-client-id`, `youtube-oauth-client-secret`, `youtube-oauth-refresh-token`
- **IAM roles needed:** `cloudbuild.builds.builder`, `secretmanager.secretAccessor`, `bigquery.dataEditor`, `bigquery.jobUser`

## Key Code Patterns

- **Idempotent writes:** DELETE + batch load (not streaming inserts) — avoids BigQuery streaming buffer consistency issues. The analytics tables key the DELETE on `activity_date`, NOT `snapshot_date`. Recovered rows share one collection date, so a snapshot-keyed delete would erase them all. The DELETE also runs only after rows are in hand; deleting first destroyed three days of history on 2026-05-25.
- **Structured logging:** JSON via `google.cloud.logging`, each run tagged with `run_id` (UUID prefix)
- **Graceful degradation:** Analytics API failure does not crash the pipeline; Data API tables always populated
- **Exponential backoff:** 2^attempt seconds, max 3 retries, on `{429, 500, 502, 503, 504}`. Retrying only 429 silently dropped a video's traffic on the first transient 500.
- **Traffic sources:** require per-video calls (can't batch); video analytics is a single call for all videos
- **Lookback window:** `ANALYTICS_LOOKBACK_DAYS = 5`. It was 3, which is exactly the edge of availability (T-0/T-1/T-2 return nothing, T-3 is the first populated day), so any extra day of latency produced an empty result.
- **Self-healing gaps:** each run re-queries activity dates with no rows, within `GAP_LOOKBACK_DAYS` (21), up to `MAX_GAP_REPAIRS_PER_RUN` (5), tagged `load_source='gap_repair'`. Covers `daily_video_analytics` only; traffic-source gaps do NOT self-heal.
- **Run date:** `PIPELINE_TZ` (America/Phoenix). Cloud Run is UTC and the job fires at 23:50 local, so `date.today()` stamped every row with the next day's date.
- **200-row cap:** the unfiltered video report is a capped top-N report. `startIndex` does not page past it and must never be reintroduced. On hitting the cap the client re-fetches via `filters=video==` shards. Full detail in the comment block atop `youtube_analytics_api.py`.
- **Shorts threshold:** `SHORTS_THRESHOLD_SECONDS = 180`, a module constant in `youtube_data_api.py`. Despite appearing in `.env.example`, it is NOT read from the environment.

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

The build is complete. `README.md` contains the deployment guide and build log. Keep both files in sync with any future changes to the pipeline.

## Current Deployment Status

Pipeline is fully deployed and operational.

- **APIs enabled:** BigQuery, YouTube Data API v3, YouTube Analytics API, Cloud Functions, Cloud Scheduler, Secret Manager, Cloud Build, Cloud Run, Cloud Storage, Logging, Monitoring
- **BigQuery dataset:** `youtube_analytics`. Four live tables (`video_metadata`, `daily_video_stats`, `daily_video_analytics`, `daily_traffic_sources`) plus two pre-migration archives (`daily_video_analytics_v1_archive`, `daily_traffic_sources_v1_archive`). The analytics tables are partitioned by `activity_date` and clustered by `video_id`; the two Data API tables remain on `snapshot_date`. Every analytics row carries `load_source` (`cron`, `backfill_YYYYMMDD`, `recovery_YYYYMMDD`, `gap_repair`), which is the only way to tell writers apart.
- **Cloud Function:** `youtube-bigquery-pipeline` deployed (2nd gen, Python 3.11, 512MB, 540s timeout)
- **Cloud Scheduler:** runs daily at 11:50 PM Phoenix time (`America/Phoenix`, no DST)
- **OAuth2:** refresh token + client credentials stored in Secret Manager

Operational specifics (initial deploy date, channel stats at build, current ingestion health) live in `.internal/OWNER_CONFIG.md`.

## Known Limitations

- `impressions` and `impression_ctr` in `daily_video_analytics` are always `NULL` **because the Analytics API does not expose them** (probed 2026-05-25: `impressions`, `videoThumbnailImpressions`, `videoThumbnailImpressionsClickRate` all rejected). **Corrected 2026-08-29: they are NOT unreachable.** The YouTube *Reporting* API report `channel_reach_basic_a1` provides `video_thumbnail_impressions` and `video_thumbnail_impressions_ctr` per video per day, dimensions `date, channel_id, video_id`. It needs no new OAuth scope, but a reporting job only backfills 30 days before its creation, so history is lost until a job exists. See `.internal/NEXT-SESSION-reporting-api-jobs.md`.
- `annotation_click_through_rate` always `NULL` — YouTube retired annotations in 2019.
- `card_click_rate` always `NULL` — `cardClickRate`/`cardImpressions` ARE valid metrics but require per-video calls with `filters=video==X`, and this channel does not use cards. Wire it up if cards start being used.
- The OAuth app uses the sensitive `yt-analytics.readonly` scope. Refresh tokens were previously expiring on a ~7-day cycle; the current token (Secret Manager version created 2026-05-25) has survived **90+ days**, so that cap no longer appears to apply. Rotation procedure lives in `.internal/REFRESH_TOKEN_ROTATION.md`.
- **Corrected 2026-07-26 — do not attribute missing analytics days to token expiry.** This file previously claimed the pipeline silently writes 0 analytics rows because the refresh token dies. Cloud Function logs disprove that for the 2026-07-07 and 2026-07-17 runs. Verbatim from 2026-07-17: `Got analytics for 0 videos (date: 2026-07-14)`, then `Deleted existing rows`, then `No rows to insert` — all at severity INFO, no error raised. Traffic sources fetched 103 rows on the same run seconds later using the same credentials, so auth was healthy.
- **Causes of missing analytics days (there is more than one).** (1) The lookback sat at exactly the edge of availability, so a day of extra latency returned an empty set. Confirmed by live probe 2026-08-29 and fixed by raising the lookback to 5. (2) A single metric in the six-metric query can zero the whole response; a Google engineer confirmed this on issuetracker 552694602, alongside not owning the video and privacy thresholds. Cause 2 was ruled out for the specific day probed, NOT in general. Do not write this up as a single cause. Both are now mitigated by the self-healing re-query rather than by any assumption about which one fired.
- **Recovered activity dates (2026-08-29):** 2026-02-22/23/24 (destroyed by the 2026-05-25 backfill overwrite), plus 07-03, 07-04, 07-14, 08-11 (never collected). 2025-10-22/23 remain empty and are genuine zero-activity days, not gaps.
- **Delete-before-insert:** fixed in `cloud_function/bigquery_writer.py` and in `setup/backfill_analytics.py` (both now check for rows first and key the delete on `activity_date`). This hazard destroyed activity 2026-02-22/23/24. If you write any new loader, preserve both properties.
- Analytics API quota is not publicly documented like the Data API's 10,000-unit system
- Recent Google docs suggest `youtube.readonly` scope may now be required alongside `yt-analytics.readonly` — current single-scope config still works but worth monitoring

## Additional API Fields Not Yet Captured

**YouTube Data API v3:** `description`, `defaultLanguage`, `defaultAudioLanguage`, `liveBroadcastContent`, `topicCategories`, `definition`, `caption`

**YouTube Analytics API v2 (additional metrics):** `annotationCloseRate`, `cardImpressions`, `cardClicks`, `audienceWatchRatio`, `likes`/`dislikes`

**YouTube Analytics API v2 (new dimensions — would need new tables):** `ageGroup`/`gender` (demographics, requires additional OAuth scope), `country`/`province` (geography), `insightPlaybackLocationType` (playback location)
