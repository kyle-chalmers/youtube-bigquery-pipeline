# YouTube BigQuery Pipeline — Project Context

## Project Purpose

Daily automated pipeline that snapshots YouTube analytics for the KC Labs AI channel into BigQuery. This enables historical trend analysis, content performance comparison, and growth tracking that YouTube Studio doesn't natively support.

IMPORTANT: Everything in this repo is public-facing, so do not place any sensitive info here and make sure to distinguish between what should be internal-facing info (e.g. secrets, PII, recording guides/scripts), and public-facing info (instructions, how-to guides, actual code utilized). If there is information that Claude Code needs across sessions but should not be published, put it in the `.internal/` folder which is ignored by git per the `.gitignore`.

**This build is being recorded as a YouTube video.** Keep the build process clean, demonstrable, and well-narrated through commit messages and README documentation. The video title is "I Let Claude Code Build My Entire YouTube Analytics Pipeline."

## YouTube Channel Details

- **Channel:** KC Labs AI (@kylechalmersdataai)
- **Channel ID:** `UCkRi29nXFxNBuPhjseoB6AQ`
- **Current content:** 63 total videos (requires pagination, mix of full-length and shorts)
- **Shorts threshold:** Duration <= 180 seconds

## Environment & Authentication

- **YOUTUBE_API_KEY:** Set in `~/.zshrc` — source it if not in env: `source ~/.zshrc`
- **YOUTUBE_CHANNEL_ID:** Set in `~/.zshrc`
- **gcloud CLI:** Installed and authenticated
- **GCP Project:** "My First Project" — project ID: `primeval-node-478707-e9`
- **BigQuery dataset name:** `youtube_analytics`
- **Region:** `us-central1`

## Validated API Patterns

These API calls have been tested and confirmed working in this environment:

**Fetch channel info:**

```bash
curl -s "https://www.googleapis.com/youtube/v3/channels?part=snippet&id=UCkRi29nXFxNBuPhjseoB6AQ&key=$YOUTUBE_API_KEY"
```

**Fetch video details (snippet + contentDetails + statistics):**

```bash
curl -s "https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails,statistics&id={comma-separated-ids}&key=$YOUTUBE_API_KEY"
```

**Key API facts discovered:**

- `publishedAt` is in `snippet` (not `contentDetails`)
- Duration is ISO 8601 format in `contentDetails.duration` (e.g., `PT12M34S`)
- Pagination uses `nextPageToken` — max 50 results per request
- All 63 videos (full-length and shorts) are accessible via the uploads playlist

## Video Script Structure (for demo flow)

**Note:** The build is complete and all sections below have been implemented. These remain as reference for the video edit and any future content.

The video follows this section structure:

1. **Architecture overview** — explain what we're building (4 tables, 2 APIs, Cloud Function, Scheduler)
2. **The Prompt** — show the structured PROMPT.md
3. **Infrastructure & setup scripts** — enabling APIs, creating BigQuery tables
4. **Cloud Function code** — Python modules for each API + BigQuery writer
5. **OAuth2 & Secret Manager** — the tricky authentication part
6. **Deploy & schedule** — deploy the function, create the scheduler job
7. **The payoff** — trigger manually, show data in BigQuery

**Build tips for video quality:**

- Write clear commit messages that tell a story
- Build incrementally — get the Data API working first, then add Analytics API
- When errors occur, show the debugging process (this is valuable content)
- The README should be comprehensive enough that a viewer could clone and deploy

## Tech Stack

- **Runtime:** Python 3.11+ on Google Cloud Functions (2nd gen)
- **Data warehouse:** Google BigQuery
- **Scheduler:** Google Cloud Scheduler
- **Secrets:** Google Cloud Secret Manager
- **APIs:** YouTube Data API v3, YouTube Analytics API v2
- **Libraries:** google-cloud-bigquery, google-api-python-client, google-auth, google-cloud-logging, google-cloud-secret-manager

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
- **Graceful degradation:** Analytics API failure doesn't crash pipeline; Data API tables always populated
- **Exponential backoff:** 2^attempt seconds on 429s, max 3 retries (Analytics API)
- **Traffic sources:** Require per-video calls (can't batch); video analytics is a single call for all videos
- **Lookback window:** `ANALYTICS_LOOKBACK_DAYS = 3` (Analytics API data has ~2-3 day latency)
- **Shorts threshold:** `SHORTS_THRESHOLD_SECONDS = 180`

## Cost Expectations

Everything runs within GCP free tier:

- Cloud Functions: 2M free invocations/month (we use 1/day = 30)
- Cloud Scheduler: 3 free jobs (we use 1)
- BigQuery: 10GB storage + 1TB queries free (our data is tiny)
- YouTube Data API: 10,000 units/day (we use ~4)

## What Claude Code Has Access To

Claude Code is building this project with the following access:

- **Shell:** Full terminal access — can run `gcloud`, `bq`, `curl`, `python3`, `git`, and any CLI tools
- **File system:** Read/write access to the repo and local config files (e.g., `~/.zshrc`)
- **gcloud CLI:** Authenticated to GCP project `primeval-node-478707-e9` — can enable APIs, create BigQuery resources, deploy Cloud Functions, manage Scheduler jobs
- **bq CLI:** Can create datasets, tables, and run queries against BigQuery
- **YouTube Data API v3:** Via `YOUTUBE_API_KEY` env var — can fetch video metadata and public stats
- **YouTube Analytics API:** Accessible via OAuth2 — refresh token + client credentials stored in Secret Manager
- **Git:** Can stage, commit, and manage the local repo (pushes require user approval)
- **No access to:** GCP Console UI, browser-based OAuth flows, or the YouTube Studio dashboard. Kyle handles those manually when needed.

## Documentation

The build is complete. README.md contains the finalized deployment guide and build log. Keep it in sync with any future changes to the pipeline.

## Current Deployment Status

Fully deployed as of 2026-02-17. All components operational.

- **GCP Project ID:** `primeval-node-478707-e9`
- **APIs enabled:** BigQuery, YouTube Data API v3, YouTube Analytics API, Cloud Functions, Cloud Scheduler, Secret Manager, Cloud Build, Cloud Run, Cloud Storage, Logging, Monitoring
- **BigQuery dataset:** `youtube_analytics` with 4 populated tables (`video_metadata`, `daily_video_stats`, `daily_video_analytics`, `daily_traffic_sources`)
- **Cloud Function:** `youtube-bigquery-pipeline` deployed (2nd gen, Python 3.11, 512MB, 540s timeout)
- **Cloud Scheduler:** `youtube-daily-snapshot` running daily at 11:50 PM Phoenix time (`America/Phoenix`, no DST)
- **OAuth2:** Configured and operational — refresh token + client credentials in Secret Manager
- **Historical backfill:** Complete — Analytics API data from Oct 16, 2025 (first video) to Feb 17, 2026 via `setup/backfill_analytics.py`
- **Channel stats at build start (2026-02-17):** 63 videos, 278 subscribers, 30,565 views

## Known Limitations

- `impressions` and `impression_ctr` columns in `daily_video_analytics` are always `NULL` — the Analytics API offers `videoThumbnailImpressions` and `videoThumbnailImpressionsClickRate` but they're not yet wired in
- `annotation_click_through_rate` and `card_click_rate` are also `NULL` (not in current API query)
- Analytics API quota is not publicly documented like the Data API's 10,000 unit system
- Recent Google docs suggest `youtube.readonly` scope may now be required alongside `yt-analytics.readonly` — current single-scope config still works but worth monitoring

## Additional API Fields Not Yet Captured

**YouTube Data API v3:** `description`, `defaultLanguage`, `defaultAudioLanguage`, `liveBroadcastContent`, `topicCategories`, `definition`, `caption`

**YouTube Analytics API v2 (additional metrics):** `annotationCloseRate`, `cardImpressions`, `cardClicks`, `audienceWatchRatio`, `likes`/`dislikes`

**YouTube Analytics API v2 (new dimensions — would need new tables):** `ageGroup`/`gender` (demographics, requires additional OAuth scope), `country`/`province` (geography), `insightPlaybackLocationType` (playback location)
