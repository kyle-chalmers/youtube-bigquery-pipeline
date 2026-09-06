# Build log

The original build, February 2026, as recorded in the README at the time. Kept here as history; the
current state of the pipeline is in the README and `CLAUDE.md`.

### Step 0: Environment Verification (2026-02-17)

Verified target GCP project (set via `GCP_PROJECT` env var or active gcloud config). Found BigQuery and YouTube Data API already enabled. Fixed an API key mismatch (key was from a wrong project). Channel confirmed via uploads playlist enumeration.

### Step 1: Infrastructure Setup

Enabled Cloud Functions, Scheduler, Secret Manager, YouTube Analytics API, Cloud Build, and Cloud Run APIs. Created `youtube_analytics` dataset with 4 partitioned tables.

### Step 2: Cloud Function Development

Built modular Python Cloud Function with:
- `youtube_data_api.py`: Playlist pagination, batch video detail fetching (50/request), ISO 8601 duration parsing, shorts classification
- `bigquery_writer.py`: Idempotent DELETE + batch load pattern (avoids streaming buffer consistency issues)
- `main.py`: Orchestration with graceful Analytics API fallback

### Step 3: First Deployment

Deployed 2nd gen Cloud Function. Resolved IAM permission gaps: Cloud Build builder role, Secret Manager accessor, BigQuery data editor + job user. Stored API key in Secret Manager.

**First successful trigger:** 63 videos processed (12 full-length, 51 shorts), `video_metadata` and `daily_video_stats` populated.

### Step 4: Analytics API + OAuth2

Created OAuth2 setup guide, consent flow helper script, and Analytics API client module with exponential backoff. Analytics tables will populate after OAuth2 consent flow is completed.

### Step 5: Cloud Scheduler

Created daily trigger at 11:50 PM Phoenix time (`America/Phoenix`, no DST) with OIDC authentication and 3 retries. Moved to 00:10 on 2026-09-05 so the run is first in the Data API quota day.

### Step 6: OAuth2 + Analytics API

Completed OAuth2 consent flow for the YouTube Analytics API. Configured consent screen, created Desktop app credentials, ran the browser-based authorization flow, and stored the refresh token + client credentials in Secret Manager. Analytics tables now populate with watch time, retention, subscriber impact, and traffic source data.

### Step 7: Historical Backfill

Backfilled Analytics API data from the channel's first video (October 16, 2025) through February 17, 2026, 125 days of historical data. This populated `daily_video_analytics` and `daily_traffic_sources` with per-day metrics that the daily pipeline wouldn't have captured retroactively. Added verification queries (`sql/verification_queries.sql`) to confirm backfill coverage and spot gaps.

### Later work

- 2026-08: audit and repair of the analytics tables (`docs/pipeline-audit-2026-08.md`, `AUDITING-YOUR-DATA-WITH-AI.md`).
- 2026-09-05/06: Reporting API source, growth views, hardening and promotion (`docs/diagrams/`, `docs/studio-comparison.md`).
