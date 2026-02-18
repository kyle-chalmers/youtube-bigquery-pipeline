# YouTube BigQuery Pipeline

Daily automated pipeline that snapshots YouTube analytics into BigQuery for historical trend analysis. Built for the [KC Labs AI](https://www.youtube.com/@kylechalmersdataai) channel.

> This project was built live as a YouTube video: **"I Let Claude Code Build My Entire YouTube Analytics Pipeline."** The build prompt (`PROMPT.md`) was created using the [`/taches-cc-resources:create-prompt`](https://github.com/taches-ai/taches-cc-resources) Claude Code skill.

---

## Architecture

```text
                            ┌──────────────────────────────────┐
                            │       Google Cloud Scheduler      │
                            │  (Daily @ 11:50 PM Phoenix time)  │
                            └────────────────┬─────────────────┘
                                             │ HTTP trigger
                                             ▼
┌─────────────────────┐     ┌──────────────────────────────────┐
│  YouTube Data API   │────▶│                                  │
│       v3            │     │     Cloud Function (Python)      │
│                     │     │         (2nd gen)                │
│  • Video metadata   │     │                                  │
│  • Public stats     │     │  1. Fetch all video IDs          │
│  (API Key auth)     │     │  2. Get metadata + stats         │
└─────────────────────┘     │  3. Get analytics + traffic      │
                            │  4. Write snapshots to BQ        │
┌─────────────────────┐     │                                  │
│ YouTube Analytics   │────▶│                                  │
│     API v2          │     └──────────────┬───────────────────┘
│                     │                    │
│  • Watch time       │                    │ Reads secrets
│  • Impressions/CTR  │                    │ at runtime
│  • Traffic sources  │     ┌──────────────┴───────────────────┐
│  (OAuth2 auth)      │     │      Secret Manager              │
└─────────────────────┘     │  (OAuth2 refresh token,          │
                            │   client ID, client secret)      │
                            └──────────────────────────────────┘
                                             │
                                             │ Write daily snapshots
                                             ▼
                ┌──────────────────────────────────────────────────┐
                │                 BigQuery                         │
                │            dataset: youtube_analytics            │
                │                                                  │
                │  ┌─────────────────┐  ┌───────────────────────┐  │
                │  │ video_metadata  │  │  daily_video_stats    │  │
                │  │                 │  │                       │  │
                │  │ title, duration │  │ views, likes,         │  │
                │  │ type, tags      │  │ comments, favorites   │  │
                │  │ (updated daily) │  │ (appended daily)      │  │
                │  └─────────────────┘  └───────────────────────┘  │
                │                                                  │
                │  ┌─────────────────┐  ┌───────────────────────┐  │
                │  │ daily_video_    │  │ daily_traffic_        │  │
                │  │ analytics       │  │ sources               │  │
                │  │                 │  │                       │  │
                │  │ watch time,     │  │ source type,          │  │
                │  │ impressions,    │  │ views, watch time     │  │
                │  │ CTR, subs       │  │ (appended daily)      │  │
                │  │ (appended daily)│  │                       │  │
                │  └─────────────────┘  └───────────────────────┘  │
                │                                                  │
                │  All tables partitioned by snapshot_date         │
                └──────────────────────────────────────────────────┘
```

**Data flow summary:**

- **Cloud Scheduler** triggers the Cloud Function once daily
- **Cloud Function** calls both YouTube APIs, then writes to 4 BigQuery tables
- **Data API** (API key) provides video metadata and public stats
- **Analytics API** (OAuth2) provides watch time, impressions, traffic sources
- **Secret Manager** stores OAuth2 credentials so no secrets live in code
- **BigQuery** stores daily snapshots partitioned by date for efficient querying
- Everything runs within GCP free tier

---

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| **Google Cloud SDK (`gcloud`)** | GCP project management, deployments | [Install guide](https://cloud.google.com/sdk/docs/install) |
| **`bq` CLI** | BigQuery management (included with gcloud) | Included with gcloud |
| **Python 3.11+** | Cloud Function runtime | [python.org](https://www.python.org/downloads/) |

### GCP Authentication

```bash
gcloud auth login
gcloud config set project <your-project-id>
gcloud config get-value project
```

### Environment Variables

Add to your `~/.zshrc` (or `~/.bashrc`):

```bash
export YOUTUBE_API_KEY="your-api-key-here"
export YOUTUBE_CHANNEL_ID="your-channel-id-here"
```

Then `source ~/.zshrc` to load them.

---

## BigQuery Schema

All tables live in the `youtube_analytics` dataset and are partitioned by `snapshot_date` for cost-efficient querying.

### `video_metadata`

Slowly changing dimension — refreshed daily with the latest metadata from the YouTube Data API v3. One row per video per day.

| Column | Type | Description |
|--------|------|-------------|
| `video_id` | STRING | YouTube video ID (e.g., `5_q7j-k8DbM`) |
| `title` | STRING | Video title (can change over time — snapshots capture changes) |
| `published_at` | TIMESTAMP | When the video was originally published |
| `duration_seconds` | INT64 | Video length in seconds |
| `duration_formatted` | STRING | Human-readable duration (e.g., `12:34` or `1:12:54`) |
| `video_type` | STRING | `short` (<=180s) or `full_length` |
| `tags` | STRING | Comma-separated tags set by the creator |
| `category_id` | STRING | YouTube category ID (e.g., `28` = Science & Technology) |
| `thumbnail_url` | STRING | URL of the highest-resolution thumbnail |
| `snapshot_date` | DATE | Date this snapshot was captured (partition key) |

### `daily_video_stats`

Append-only daily snapshots of public stats from the YouTube Data API v3. These are cumulative counters — views only go up over time.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_date` | DATE | Date this snapshot was captured (partition key) |
| `video_id` | STRING | YouTube video ID |
| `view_count` | INT64 | Total cumulative views as of snapshot time |
| `like_count` | INT64 | Total cumulative likes |
| `comment_count` | INT64 | Total cumulative comments |
| `favorite_count` | INT64 | Total cumulative favorites (rarely used) |

### `daily_video_analytics`

Append-only daily snapshots from the YouTube Analytics API v2. These are per-day metrics (not cumulative) — they represent activity for a specific analytics date. Only videos with activity on the lookback date will have rows.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_date` | DATE | Date the pipeline ran (partition key) |
| `video_id` | STRING | YouTube video ID |
| `estimated_minutes_watched` | FLOAT64 | Total watch time in minutes across all viewers |
| `average_view_duration_seconds` | FLOAT64 | How long the average viewer watched before leaving |
| `average_view_percentage` | FLOAT64 | Percentage of the video the average viewer watched (e.g., 45.0 = 45%) |
| `impressions` | INT64 | Times YouTube showed the thumbnail (NULL until aggregated from traffic data) |
| `impression_ctr` | FLOAT64 | Click-through rate on impressions (NULL until aggregated from traffic data) |
| `subscribers_gained` | INT64 | Subscriptions gained from this video |
| `subscribers_lost` | INT64 | Subscriptions lost from this video |
| `shares` | INT64 | Times the video was shared (share button, copy link, etc.) |
| `annotation_click_through_rate` | FLOAT64 | Click rate on annotations (legacy — mostly NULL for newer videos) |
| `card_click_rate` | FLOAT64 | Click rate on info cards (the "i" popups added to videos) |

### `daily_traffic_sources`

Append-only from the YouTube Analytics API v2. One row per video per traffic source type — shows where viewers discovered each video.

| Column | Type | Description |
|--------|------|-------------|
| `snapshot_date` | DATE | Date the pipeline ran (partition key) |
| `video_id` | STRING | YouTube video ID |
| `traffic_source_type` | STRING | How viewers found the video (see values below) |
| `views` | INT64 | Views from this source on the analytics date |
| `estimated_minutes_watched` | FLOAT64 | Watch time from this source |

**Common `traffic_source_type` values:**

| Source | Meaning |
|--------|---------|
| `YT_SEARCH` | Found via YouTube search |
| `SUGGESTED` | Recommended in sidebar or feed |
| `BROWSE_FEATURES` | Home page, subscription feed, trending |
| `EXT_URL` | External website (blog, Reddit, social media) |
| `NOTIFICATION` | Bell or push notification |
| `PLAYLIST` | Watched via a playlist |
| `SHORTS` | Shorts feed |
| `NO_LINK_OTHER` | Direct URL or uncategorized |

---

## Deployment (Step by Step)

Run the setup scripts in order. Each script is idempotent (safe to re-run).

### Step 1: Enable GCP APIs

```bash
bash setup/1_enable_apis.sh
```

Enables: Cloud Functions, Cloud Scheduler, Secret Manager, YouTube Analytics API, Cloud Build, Cloud Run.

### Step 2: Create BigQuery Tables

```bash
bash setup/2_create_bigquery.sh
```

Creates the `youtube_analytics` dataset and 4 tables:

| Table | Source | Type |
|-------|--------|------|
| `video_metadata` | Data API v3 | Slowly changing dimension (updated daily) |
| `daily_video_stats` | Data API v3 | Append-only daily snapshots |
| `daily_video_analytics` | Analytics API v2 | Append-only daily snapshots |
| `daily_traffic_sources` | Analytics API v2 | Append-only daily snapshots |

### Step 3: Set Up OAuth2 (One-Time)

The YouTube Analytics API requires OAuth2 consent from the channel owner. This is a manual process:

```bash
bash setup/3_setup_oauth.sh
```

Follow the printed instructions to:
1. Configure the OAuth consent screen in GCP Console
2. Create OAuth credentials (Desktop app type)
3. Run `python3 setup/oauth_helper.py` to complete the browser consent flow
4. Store the refresh token, client ID, and client secret in Secret Manager

**Note:** If your YouTube channel is on a different Google account than your GCP project, add that personal account as a test user in the consent screen, then sign in with it during the consent flow.

### Step 4: Store API Key in Secret Manager

```bash
source ~/.zshrc
printf '%s' "$YOUTUBE_API_KEY" > /tmp/secret.tmp
gcloud secrets create youtube-data-api-key --data-file=/tmp/secret.tmp
rm /tmp/secret.tmp
```

### Step 5: Deploy the Cloud Function

```bash
bash setup/4_deploy_function.sh
```

Deploys a 2nd gen Cloud Function (Python 3.11, 512MB memory, 9-minute timeout).

**IAM permissions needed** (grant these if deployment fails):

```bash
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')

# Cloud Build permissions
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/cloudbuild.builds.builder"

# Secret Manager access
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# BigQuery access
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/bigquery.jobUser"
```

### Step 6: Create Cloud Scheduler Job

```bash
bash setup/5_create_scheduler.sh
```

Creates a daily trigger at 11:50 PM Phoenix time (`America/Phoenix` timezone — no DST surprises) with 3 retries and exponential backoff.

---

## Manual Testing

Trigger the function manually:

```bash
FUNCTION_URL=$(gcloud functions describe youtube-bigquery-pipeline \
    --region=us-central1 --gen2 --format='value(serviceConfig.uri)')

curl -s -H "Authorization: bearer $(gcloud auth print-identity-token)" \
    "$FUNCTION_URL" | python3 -m json.tool
```

Expected response:

```json
{
    "snapshot_date": "2026-02-17",
    "videos_processed": 63,
    "shorts": 51,
    "full_length": 12,
    "rows_inserted": {
        "video_metadata": 63,
        "daily_video_stats": 63,
        "daily_video_analytics": 0,
        "daily_traffic_sources": 0
    },
    "analytics_errors": []
}
```

Analytics tables show 0 until OAuth2 is configured (Step 3).

---

## Historical Backfill

The Analytics API supports historical date ranges, so we backfilled data from the channel's first public video (October 16, 2025) to the present. This gives ~125 days of historical watch time, subscriber impact, and traffic source data.

```bash
python3 setup/backfill_analytics.py --start 2025-10-16 --end 2026-02-17
```

**What gets backfilled:**
- `daily_video_analytics` — watch time, retention, subscriber gains/losses, shares per video per day
- `daily_traffic_sources` — traffic source breakdown per video per day

**What doesn't get backfilled:**
- `video_metadata` and `daily_video_stats` (Data API) — these only return current cumulative totals, not historical snapshots. They start accumulating from the first pipeline run forward.

**Note:** The backfill makes ~64 API calls per day (1 video analytics call + 1 traffic source call per video). For 125 days, that's ~8,000 calls total. Expect it to take 45–60 minutes. Occasional YouTube API 500 errors on individual calls are normal — the script logs them and continues.

After backfilling, run the verification queries to confirm coverage:

```bash
bq query --use_legacy_sql=false < sql/verification_queries.sql
```

---

## Querying the Data

See `sql/sample_queries.sql` for a full set of analytical queries and `sql/verification_queries.sql` for data integrity checks. Here are a few examples:

### Top videos by views

```sql
SELECT m.title, m.video_type, s.view_count, s.like_count
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s USING (video_id, snapshot_date)
WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`)
ORDER BY s.view_count DESC
LIMIT 10;
```

### Shorts vs full-length comparison

```sql
SELECT m.video_type, COUNT(*) AS videos,
    ROUND(AVG(s.view_count)) AS avg_views,
    ROUND(AVG(s.like_count)) AS avg_likes
FROM `youtube_analytics.video_metadata` m
JOIN `youtube_analytics.daily_video_stats` s USING (video_id, snapshot_date)
WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM `youtube_analytics.video_metadata`)
GROUP BY m.video_type;
```

### Week-over-week channel growth

```sql
SELECT snapshot_date, SUM(view_count) AS total_views,
    SUM(view_count) - LAG(SUM(view_count)) OVER (ORDER BY snapshot_date) AS daily_delta
FROM `youtube_analytics.daily_video_stats`
GROUP BY snapshot_date
ORDER BY snapshot_date DESC LIMIT 14;
```

---

## Cost

Everything runs within GCP free tier:

| Service | Free Tier | Our Usage |
|---------|-----------|-----------|
| Cloud Functions | 2M invocations/month | ~30 (1/day) |
| Cloud Scheduler | 3 jobs | 1 job |
| BigQuery | 10GB storage, 1TB queries | Tiny |
| YouTube Data API | 10,000 units/day | ~4 units |
| Secret Manager | 10,000 access ops/month | ~4/day |

---

## Project Structure

```text
cloud_function/
  main.py                      # Cloud Function entry point
  requirements.txt             # Python dependencies
  youtube_data_api.py          # YouTube Data API v3 client
  youtube_analytics_api.py     # YouTube Analytics API v2 client
  bigquery_writer.py           # BigQuery write operations
setup/
  1_enable_apis.sh             # Enable GCP APIs
  2_create_bigquery.sh         # Create BigQuery dataset + tables
  3_setup_oauth.sh             # OAuth2 setup guide
  4_deploy_function.sh         # Deploy Cloud Function
  5_create_scheduler.sh        # Create Cloud Scheduler job
  oauth_helper.py              # One-time OAuth consent flow
  backfill_analytics.py        # Backfill historical Analytics API data
sql/
  create_tables.sql            # BigQuery DDL (4 tables)
  sample_queries.sql           # Analytical queries
  verification_queries.sql     # Data integrity and backfill verification
```

---

## Future Enhancements

Additional fields available from the YouTube APIs that we're not currently capturing:

**YouTube Data API v3 (additional metadata):**

| Field | Description |
|-------|-------------|
| `description` | Full video description text |
| `defaultLanguage` / `defaultAudioLanguage` | Video language settings |
| `liveBroadcastContent` | Whether the video was a livestream |
| `topicCategories` | Wikipedia URLs classifying the video topic (e.g., "Technology") |
| `definition` | HD vs SD |
| `caption` | Whether closed captions are available |

**YouTube Analytics API v2 (additional metrics):**

| Field | Description |
|-------|-------------|
| `annotationCloseRate` | Annotation dismissal rate |
| `cardImpressions` / `cardClicks` | Info card engagement |
| `audienceWatchRatio` | Audience retention curve data |
| `likes` / `dislikes` | API-level counts (separate from Data API) |

**YouTube Analytics API v2 (new dimensions — would need new tables):**

| Dimension | Description |
|-----------|-------------|
| `ageGroup` / `gender` | Viewer demographics (requires additional OAuth scope) |
| `country` / `province` | Geographic breakdown of views |
| `insightPlaybackLocationType` | Watch page vs embedded vs mobile app |

The geography and demographics data would be the most valuable for channel growth analysis — identifying which countries and age groups are watching. These would require new BigQuery tables since they represent different analytical dimensions.

---

## Build Log

### Step 0: Environment Verification (2026-02-17)

Verified GCP project `primeval-node-478707-e9`. Found BigQuery and YouTube Data API already enabled. Fixed an API key mismatch (key was from a wrong project). Channel confirmed: 63 videos, 278 subscribers, 30,565 views.

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

Created daily trigger at 11:50 PM Phoenix time (`America/Phoenix` — no DST) with OIDC authentication and 3 retries.

### Step 6: OAuth2 + Analytics API

Completed OAuth2 consent flow for the YouTube Analytics API. Configured consent screen, created Desktop app credentials, ran the browser-based authorization flow, and stored the refresh token + client credentials in Secret Manager. Analytics tables now populate with watch time, retention, subscriber impact, and traffic source data.

### Step 7: Historical Backfill

Backfilled Analytics API data from the channel's first video (October 16, 2025) through February 17, 2026 — 125 days of historical data. This populated `daily_video_analytics` and `daily_traffic_sources` with per-day metrics that the daily pipeline wouldn't have captured retroactively. Added verification queries (`sql/verification_queries.sql`) to confirm backfill coverage and spot gaps.
