# Objective

Build a complete daily YouTube analytics pipeline that fetches metrics for all published videos (full-length and shorts) from the YouTube Data API v3 and YouTube Analytics API, stores daily snapshots in Google BigQuery, and runs automatically via Google Cloud Scheduler triggering a Cloud Function.

This pipeline will provide historical trend data for Kyle's YouTube channel (KC Labs AI) — tracking how each video's views, likes, comments, watch time, traffic sources, and engagement metrics change over time. The daily snapshot approach enables trend analysis, growth tracking, and content performance comparison.

## Context

### Channel Details

- Channel ID: UCkRi29nXFxNBuPhjseoB6AQ
- Uploads Playlist: UUkRi29nXFxNBuPhjseoB6AQ
- 63 total videos (mix of full-length and shorts), growing over time
- YOUTUBE_API_KEY is set in ~/.zshrc for the Data API v3 (source it if not in env)
- The Analytics API requires OAuth2 (not just an API key) — this will need to be set up

### GCP Details

- GCP Project: "My First Project" (use project ID from `gcloud config get-value project`)
- BigQuery dataset: youtube_analytics
- Region: us-central1
- Cloud Function runtime: Python 3.11+
- gcloud CLI is already installed and authenticated

### API Coverage

Two YouTube APIs are needed because they provide different data:

**YouTube Data API v3** (API key auth):

- Video metadata: title, publishedAt, duration, tags, categoryId
- Public stats: viewCount, likeCount, commentCount, favoriteCount
- Endpoint: videos?part=snippet,contentDetails,statistics

**YouTube Analytics API** (OAuth2 auth):

- Watch time metrics: estimatedMinutesWatched, averageViewDuration, averageViewPercentage
- Engagement: likes, dislikes, comments, shares, subscribersGained, subscribersLost
- Traffic sources: trafficSourceType, trafficSourceDetail
- Impressions and CTR: impressions, impressionClickThroughRate
- Audience retention data
- Endpoint: youtube/analytics/v2/reports

This is a fresh repository. All code should be created at the repo root level.

## Research

Before writing any code, thoroughly investigate the current GCP setup and API requirements:

1. Run `gcloud config get-value project` to confirm the project ID
2. Run `gcloud services list --enabled` to check which APIs are already enabled
3. Check if BigQuery dataset `youtube_analytics` already exists: `bq ls`
4. Check existing Cloud Functions: `gcloud functions list`
5. Check existing Cloud Scheduler jobs: `gcloud scheduler jobs list`
6. Verify the YouTube API key works: `source ~/.zshrc && curl -s "https://www.googleapis.com/youtube/v3/channels?part=snippet&id=UCkRi29nXFxNBuPhjseoB6AQ&key=$YOUTUBE_API_KEY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('items',[{}])[0].get('snippet',{}).get('title','ERROR'))"`
7. Check if OAuth2 credentials exist for the Analytics API, or if they need to be created

## Requirements

### BigQuery Schema

Create these tables in the `youtube_analytics` dataset:

**Table: video_metadata** (slowly changing dimension — updated daily)

- video_id (STRING, primary identifier)
- title (STRING)
- published_at (TIMESTAMP)
- duration_seconds (INTEGER)
- duration_formatted (STRING, e.g., "12:34" or "1:12:54")
- video_type (STRING: "full_length" or "short", based on duration <= 180s)
- tags (STRING, comma-separated)
- category_id (STRING)
- thumbnail_url (STRING)
- snapshot_date (DATE, partition key)

**Table: daily_video_stats** (append-only daily snapshots from Data API)

- snapshot_date (DATE, partition key)
- video_id (STRING)
- view_count (INTEGER)
- like_count (INTEGER)
- comment_count (INTEGER)
- favorite_count (INTEGER)

**Table: daily_video_analytics** (append-only daily snapshots from Analytics API)

- snapshot_date (DATE, partition key)
- video_id (STRING)
- estimated_minutes_watched (FLOAT)
- average_view_duration_seconds (FLOAT)
- average_view_percentage (FLOAT)
- impressions (INTEGER)
- impression_ctr (FLOAT)
- subscribers_gained (INTEGER)
- subscribers_lost (INTEGER)
- shares (INTEGER)
- annotation_click_through_rate (FLOAT)
- card_click_rate (FLOAT)

**Table: daily_traffic_sources** (append-only from Analytics API)

- snapshot_date (DATE, partition key)
- video_id (STRING)
- traffic_source_type (STRING)
- views (INTEGER)
- estimated_minutes_watched (FLOAT)

All tables should be partitioned by snapshot_date for cost-efficient querying.

### Cloud Function

Create a Python Cloud Function (2nd gen) that:

1. Fetches ALL video IDs from the uploads playlist (handle pagination — channel has 63+ videos)
2. Calls the Data API `videos` endpoint with `snippet,contentDetails,statistics` parts (batch up to 50 IDs per request)
3. Calls the Analytics API for each video's daily metrics (for the previous day, since Analytics API data is delayed ~2 days, use a 3-day lookback window and upsert)
4. Calls the Analytics API for traffic source breakdown per video
5. Writes all data to the appropriate BigQuery tables
6. Handles errors gracefully — if Analytics API fails for one video, continue with others and log the error
7. Returns a summary of what was processed (video count, rows inserted, any errors)

**Important implementation details:**

- Use `google-cloud-bigquery` library for BigQuery writes
- Use `google-api-python-client` for YouTube APIs
- Use `google-auth` for OAuth2 service account authentication
- The function should be idempotent — running it twice on the same day should not create duplicates (use DELETE + INSERT or MERGE for the current snapshot_date)
- Set function timeout to 540 seconds (9 minutes) since we're making many API calls
- Set memory to 512MB

### OAuth2 Setup

The YouTube Analytics API requires OAuth2, not just an API key. Set up:

1. Create OAuth2 credentials in Google Cloud Console (or via gcloud) for the YouTube Analytics API
2. Since this runs as an automated Cloud Function (no user interaction), use a **service account** approach:
   - Option A: If the channel owner can do a one-time OAuth consent flow, store the refresh token as a Secret Manager secret
   - Option B: Use domain-wide delegation if applicable
3. Store sensitive credentials (refresh token, client ID, client secret) in Google Cloud Secret Manager, NOT as environment variables
4. The Cloud Function reads secrets at runtime from Secret Manager

Document the one-time OAuth setup steps clearly so Kyle can complete the consent flow.

### Cloud Scheduler

Set up Cloud Scheduler to trigger the Cloud Function:

- Schedule: daily at 6:00 AM UTC (midnight CST, after YouTube's daily stats finalize)
- HTTP trigger pointing to the Cloud Function URL
- Include appropriate authentication (OIDC token)
- Retry config: 3 retries with exponential backoff

## Implementation

Create all files at the repository root:

### File Structure

```text
./
├── README.md                        # Setup guide, architecture overview, one-time OAuth instructions
├── .gitignore                       # Python, GCP, secrets exclusions
├── cloud_function/
│   ├── main.py                      # Cloud Function entry point
│   ├── requirements.txt             # Python dependencies
│   ├── youtube_data_api.py          # YouTube Data API v3 client
│   ├── youtube_analytics_api.py     # YouTube Analytics API client
│   └── bigquery_writer.py           # BigQuery table operations
├── setup/
│   ├── 1_enable_apis.sh             # Enable required GCP APIs
│   ├── 2_create_bigquery.sh         # Create dataset and tables
│   ├── 3_setup_oauth.sh             # OAuth2 credential setup instructions
│   ├── 4_deploy_function.sh         # Deploy Cloud Function
│   └── 5_create_scheduler.sh        # Create Cloud Scheduler job
└── sql/
    ├── create_tables.sql            # BigQuery DDL for all tables
    └── sample_queries.sql           # Useful analytical queries (trends, comparisons)
```

### Coding Standards

- Use type hints throughout Python code
- Include docstrings for all functions
- Use logging (not print) — Cloud Functions integrate with Cloud Logging
- Handle API rate limits with exponential backoff
- Keep functions modular — each module handles one API or one destination
- Use constants for channel ID, playlist ID, dataset name (configurable at top of main.py)
- All shell scripts should be executable and include error handling (set -euo pipefail)

### Constraints

- YouTube Data API quota: 10,000 units/day. A videos.list call costs 1 unit per request (50 videos per request). Playlist items costs 1 unit. Total for ~60 videos: ~4 units. Well within quota.
- YouTube Analytics API: Has its own quota, typically generous for channel owners querying their own data.
- BigQuery: Free tier covers 1TB queries/month and 10GB storage — this pipeline will use a tiny fraction.
- Cloud Function: Free tier covers 2M invocations/month — 1/day is nothing.
- Cloud Scheduler: Free tier covers 3 jobs — 1 job is fine.

## Verification

Before declaring complete, verify:

1. All shell scripts are syntactically valid: `bash -n setup/*.sh`
2. Python code has no import errors: `cd cloud_function && python3 -c "import main"`
3. BigQuery DDL is valid SQL
4. The README clearly documents:
   - Architecture diagram (text-based)
   - One-time OAuth setup steps
   - How to deploy
   - How to manually trigger for testing
   - How to query the data
5. All API endpoints and parameters are correct per current documentation
6. Idempotency logic is sound (no duplicate rows on re-run)
7. .gitignore properly excludes secrets, credentials, and Python artifacts

### Success Criteria

- Complete, deployable Cloud Function code that fetches from both YouTube APIs
- BigQuery table DDL with proper partitioning and schema
- Numbered setup scripts that can be run sequentially to deploy everything
- OAuth2 setup documentation for the one-time consent flow
- Cloud Scheduler configuration for daily automated runs
- Sample BigQuery queries demonstrating how to analyze the collected data
- README with clear architecture overview and setup instructions
- .gitignore protecting secrets and build artifacts
