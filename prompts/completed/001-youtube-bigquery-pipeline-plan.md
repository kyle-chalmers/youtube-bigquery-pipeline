# YouTube BigQuery Pipeline — Implementation Plan

## Context

Build a daily automated pipeline that snapshots YouTube analytics into BigQuery for the KC Labs AI channel. The repo is clean (only docs exist) — all implementation code needs to be created from scratch. This build is being recorded as a YouTube video, so the process should be incremental and demonstrable.

**Current state:** GCP project `primeval-node-478707-e9` is set up with BigQuery and YouTube Data API v3 enabled. No datasets, functions, or scheduler jobs exist yet. OAuth2 for Analytics API is not configured.

---

## Build Order (6 Phases, 6 Commits)

Each phase produces a working checkpoint and maps to a section of the video.

### Phase 1: Infrastructure — Setup Scripts + BigQuery DDL

**Commit:** `feat: add GCP setup scripts and BigQuery table DDL`

Create files:
- `setup/1_enable_apis.sh` — Enable Cloud Functions, Scheduler, Secret Manager, YouTube Analytics API, Cloud Build, Cloud Run
- `sql/create_tables.sql` — DDL for 4 tables (all partitioned by `snapshot_date`):
  - `video_metadata` (slowly changing dimension)
  - `daily_video_stats` (append-only, Data API)
  - `daily_video_analytics` (append-only, Analytics API)
  - `daily_traffic_sources` (append-only, Analytics API)
- `setup/2_create_bigquery.sh` — Create dataset + run DDL

**Run:** Execute both setup scripts, verify with `bq ls youtube_analytics`

### Phase 2: Cloud Function — Data API Integration

**Commit:** `feat: add Cloud Function with YouTube Data API integration`

Create files:
- `cloud_function/requirements.txt` — `functions-framework`, `google-cloud-bigquery`, `google-api-python-client`, `google-auth`, `google-cloud-secret-manager`
- `cloud_function/youtube_data_api.py` — Fetch all video IDs via uploads playlist (pagination, max 50/page), fetch video details in batches of 50 (`snippet,contentDetails,statistics`), parse ISO 8601 duration, classify shorts (<=180s)
- `cloud_function/bigquery_writer.py` — Idempotent writes using DELETE + batch load (not streaming insert, to avoid buffer consistency issues)
- `cloud_function/main.py` — HTTP entry point, orchestration (Data API only in this phase, Analytics API stubbed as skip)

**Key patterns:**
- `BigQueryWriter._delete_and_insert()` uses `client.query()` for DELETE then `client.load_table_from_file()` for INSERT (batch load avoids streaming buffer conflicts)
- `YouTubeDataAPI.parse_duration()` — regex parser for `PT12M34S` format
- Constants configurable at top of `main.py` (project ID, dataset, channel ID, playlist ID)

### Phase 3: First Deploy + Data API Verification

**Commit:** `feat: add deployment script and verify first data load`

Create files:
- `setup/4_deploy_function.sh` — Deploy 2nd gen Cloud Function (Python 3.11, 512MB, 540s timeout, env vars + secrets mount for API key)

**Run:** Deploy function, trigger manually via `curl`, verify `video_metadata` and `daily_video_stats` have data in BigQuery.

**Note:** YouTube API key should be stored in Secret Manager and mounted via `--set-secrets` rather than passed as plain env var.

### Phase 4: OAuth2 Setup + Analytics API Integration

**Commit:** `feat: add OAuth2 setup and YouTube Analytics API integration`

Create files:
- `setup/3_setup_oauth.sh` — Documentation + commands for the manual consent flow
- `setup/oauth_helper.py` — One-time local script using `google-auth-oauthlib` to run browser consent flow and output refresh token
- `cloud_function/youtube_analytics_api.py` — Analytics API client with Secret Manager credential loading

Update files:
- `cloud_function/main.py` — Wire in Analytics API calls

**OAuth2 approach (multi-account):**
1. Create "Desktop app" OAuth credentials in GCP Console (work account)
2. Configure consent screen as External, add Kyle's personal email as test user
3. **Publish to "In production"** to avoid 7-day refresh token expiry (unverified app warning is fine for single-user)
4. Kyle runs `oauth_helper.py` locally, signs in with personal account (channel owner)
5. Store `client_id`, `client_secret`, `refresh_token` in Secret Manager (3 secrets)
6. Cloud Function reads secrets at runtime, uses refresh token for auto-refreshing credentials

**Analytics API query strategy:**
- **Video analytics:** Single call with `dimensions=video` for core metrics (watch time, engagement, subscribers, shares) — returns all videos at once
- **Traffic sources:** Per-video calls with `filters=video==VIDEO_ID`, `dimensions=insightTrafficSourceType` — needed because traffic source dimension requires video filter
- **Impressions/CTR:** Aggregated from traffic source responses (sum `videoThumbnailImpressions` per video)
- **Lookback:** Query `startDate = endDate = today - 3 days` (single finalized day)
- **Error handling:** Log-and-continue per video; Data API tables always populated even if Analytics fails

### Phase 5: Cloud Scheduler for Daily Automation

**Commit:** `feat: add Cloud Scheduler for daily automation`

Create files:
- `setup/5_create_scheduler.sh` — Daily at 6:00 AM UTC, OIDC auth, 3 retries with exponential backoff

### Phase 6: Sample Queries + README Polish

**Commit:** `docs: add sample queries and finalize README`

Create files:
- `sql/sample_queries.sql` — Daily view growth, top performers, shorts vs full-length comparison, traffic source breakdown, subscriber impact, week-over-week growth, watch time leaders

Update files:
- `README.md` — Add deployment steps, OAuth setup instructions, manual trigger guide, sample query examples

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Idempotency | DELETE + batch load | Simpler than MERGE; batch load avoids streaming buffer consistency issues |
| OAuth2 flow | Desktop app + refresh token in Secret Manager | Only approach that works across personal YouTube / work GCP accounts |
| Analytics lookback | Single day (today-3) | Analytics API data delayed ~2-3 days; single finalized day is most reliable |
| Rate limiting | Exponential backoff (1s base, 3 retries) | Standard pattern for Google API 429 responses |
| Impressions/CTR | Aggregated from traffic source data | These metrics only available in traffic source reports, not basic video reports |
| API key storage | Secret Manager (not env var) | Consistent with OAuth secrets; avoids plaintext in Cloud Function config |

---

## File Structure (Final)

```
cloud_function/
  main.py                      # Entry point + orchestration
  requirements.txt             # Python dependencies
  youtube_data_api.py          # Data API v3 client
  youtube_analytics_api.py     # Analytics API v2 client
  bigquery_writer.py           # BigQuery operations
setup/
  1_enable_apis.sh             # Enable GCP APIs
  2_create_bigquery.sh         # Create dataset + tables
  3_setup_oauth.sh             # OAuth2 setup docs + commands
  4_deploy_function.sh         # Deploy Cloud Function
  5_create_scheduler.sh        # Create Scheduler job
  oauth_helper.py              # One-time consent flow script
sql/
  create_tables.sql            # BigQuery DDL (4 tables)
  sample_queries.sql           # Analytical queries
```

---

## Verification

After each phase, verify:

1. **Phase 1:** `bq ls youtube_analytics` shows 4 tables, `bq show youtube_analytics.video_metadata` shows correct schema
2. **Phase 2:** `cd cloud_function && python3 -c "import main"` — no import errors
3. **Phase 3:** Manual trigger via curl returns 200 with summary JSON; `bq query "SELECT COUNT(*) FROM youtube_analytics.daily_video_stats WHERE snapshot_date = CURRENT_DATE()"` shows 63 rows
4. **Phase 4:** Redeploy, trigger again; all 4 tables populated; check `daily_video_analytics` has rows
5. **Phase 5:** `gcloud scheduler jobs describe youtube-daily-snapshot` shows correct config
6. **Phase 6:** Sample queries execute without errors

**End-to-end test:** Trigger scheduler manually with `gcloud scheduler jobs run youtube-daily-snapshot`, wait for completion, verify all 4 tables have fresh data.
