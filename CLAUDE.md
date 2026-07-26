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

- **Idempotent writes:** DELETE + batch load (not streaming inserts) — avoids BigQuery streaming buffer consistency issues
- **Structured logging:** JSON via `google.cloud.logging`, each run tagged with `run_id` (UUID prefix)
- **Graceful degradation:** Analytics API failure does not crash the pipeline; Data API tables always populated
- **Exponential backoff:** 2^attempt seconds on 429s, max 3 retries (Analytics API)
- **Traffic sources:** require per-video calls (can't batch); video analytics is a single call for all videos
- **Lookback window:** `ANALYTICS_LOOKBACK_DAYS = 3` (Analytics API data has ~2-3 day latency)
- **Shorts threshold:** `SHORTS_THRESHOLD_SECONDS = 180`

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
- **BigQuery dataset:** `youtube_analytics` with 4 populated tables (`video_metadata`, `daily_video_stats`, `daily_video_analytics`, `daily_traffic_sources`)
- **Cloud Function:** `youtube-bigquery-pipeline` deployed (2nd gen, Python 3.11, 512MB, 540s timeout)
- **Cloud Scheduler:** runs daily at 11:50 PM Phoenix time (`America/Phoenix`, no DST)
- **OAuth2:** refresh token + client credentials stored in Secret Manager

Operational specifics (initial deploy date, channel stats at build, current ingestion health) live in `.internal/OWNER_CONFIG.md`.

## Known Limitations

- `impressions` and `impression_ctr` columns in `daily_video_analytics` are always `NULL`. YouTube Studio's "Impressions" and "Impressions CTR" come from an internal Google API. The public YouTube Analytics API v2 does not expose them at the per-video level (probed 2026-05-25: `impressions`, `videoThumbnailImpressions`, and `videoThumbnailImpressionsClickRate` all rejected). Columns are kept for forward compatibility.
- `annotation_click_through_rate` always `NULL` — YouTube retired annotations in 2019.
- `card_click_rate` always `NULL` — `cardClickRate`/`cardImpressions` ARE valid metrics but require per-video calls with `filters=video==X`, and this channel does not use cards. Wire it up if cards start being used.
- The OAuth app uses the sensitive `yt-analytics.readonly` scope. Refresh tokens were previously expiring on a ~7-day cycle; the current token (Secret Manager version created 2026-05-25) has survived **62+ days**, so that cap no longer appears to apply. Rotation procedure lives in `.internal/REFRESH_TOKEN_ROTATION.md`.
- **Corrected 2026-07-26 — do not attribute missing analytics days to token expiry.** This file previously claimed the pipeline silently writes 0 analytics rows because the refresh token dies. Cloud Function logs disprove that for the 2026-07-07 and 2026-07-17 runs. Verbatim from 2026-07-17: `Got analytics for 0 videos (date: 2026-07-14)`, then `Deleted existing rows`, then `No rows to insert` — all at severity INFO, no error raised. Traffic sources fetched 103 rows on the same run seconds later using the same credentials, so auth was healthy.
- **Actual cause of missing analytics days:** the Analytics API sometimes has no data yet for `snapshot_date - ANALYTICS_LOOKBACK_DAYS` (3). It returns an empty result set, which is a valid response, not a failure. `main.py` writes nothing and never re-queries that date, so the gap is permanent. Confirmed gaps at activity dates 2025-10-22/23, 2026-05-20/21, 07-03/04, 07-14. The fix is gap detection plus a self-healing re-query, not token rotation.
- **Related hazard visible in the same logs:** `_delete_and_insert` deletes the target partition *before* it knows the API returned rows. Harmless when the partition was already empty, but a zero-row response for a date that previously had data would erase it.
- Analytics API quota is not publicly documented like the Data API's 10,000-unit system
- Recent Google docs suggest `youtube.readonly` scope may now be required alongside `yt-analytics.readonly` — current single-scope config still works but worth monitoring

## Additional API Fields Not Yet Captured

**YouTube Data API v3:** `description`, `defaultLanguage`, `defaultAudioLanguage`, `liveBroadcastContent`, `topicCategories`, `definition`, `caption`

**YouTube Analytics API v2 (additional metrics):** `annotationCloseRate`, `cardImpressions`, `cardClicks`, `audienceWatchRatio`, `likes`/`dislikes`

**YouTube Analytics API v2 (new dimensions — would need new tables):** `ageGroup`/`gender` (demographics, requires additional OAuth scope), `country`/`province` (geography), `insightPlaybackLocationType` (playback location)
