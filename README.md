# YouTube BigQuery Pipeline

Daily automated pipeline that snapshots YouTube analytics into BigQuery for historical trend analysis. Built for the [KC Labs AI](https://www.youtube.com/@kylechalmersdataai) channel.

> This project was built live as a YouTube video: **"I Let Claude Code Build My Entire YouTube Analytics Pipeline."** The build prompt (`PROMPT.md`) was created using the [`/taches-cc-resources:create-prompt`](https://github.com/taches-ai/taches-cc-resources) Claude Code skill.

---

## Architecture

```
                            ┌──────────────────────────────────┐
                            │       Google Cloud Scheduler      │
                            │      (Daily @ 6:00 AM UTC)        │
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

Before starting, you need the following installed and configured:

| Tool | Purpose | Install |
|------|---------|---------|
| **Google Cloud SDK (`gcloud`)** | GCP project management, API enabling, deployments | [Install guide](https://cloud.google.com/sdk/docs/install) |
| **`bq` CLI** | BigQuery dataset/table management (included with gcloud SDK) | Included with gcloud |
| **Python 3.11+** | Cloud Function runtime and local testing | [python.org](https://www.python.org/downloads/) |
| **`curl`** | API testing during development | Pre-installed on macOS/Linux |

### GCP Authentication

```bash
# Authenticate with your GCP account
gcloud auth login

# Set your project
gcloud config set project <your-project-id>

# Verify
gcloud config get-value project
```

### Environment Variables

Add these to your `~/.zshrc` (or `~/.bashrc`):

```bash
# YouTube API key (create one in GCP Console → APIs & Services → Credentials)
export YOUTUBE_API_KEY="your-api-key-here"

# YouTube Channel ID (find at youtube.com/account_advanced)
export YOUTUBE_CHANNEL_ID="your-channel-id-here"
```

Then `source ~/.zshrc` to load them.

---

## Build Log

*This section documents the actual build process in order. It will be reorganized into a polished setup guide after the build is complete.*

### Step 0: Environment Verification

**Date:** 2026-02-17

Verified the starting environment before writing any code:

```bash
# Confirm GCP project
gcloud config get-value project
# → primeval-node-478707-e9

# Check enabled APIs
gcloud services list --enabled
# → BigQuery, YouTube Data API v3, Cloud Storage, Logging already enabled
# → NOT yet enabled: Cloud Functions, Cloud Scheduler, Secret Manager, YouTube Analytics API

# Check for existing resources
bq ls                          # → No datasets
gcloud functions list          # → No functions
gcloud scheduler jobs list     # → No scheduler jobs
```

**Issue found: API key mismatch.** The `YOUTUBE_API_KEY` in `~/.zshrc` was from a different GCP project and returned 403 errors. Discovered by comparing `gcloud services api-keys list` output against the env var. Updated `~/.zshrc` with the correct key from `primeval-node-478707-e9`.

**Channel verified working:**
```
Channel: Kyle Chalmers | Data + AI
Handle:  @kylechalmersdataai
Videos:  63
Subs:    278
Views:   30,565
```

### Step 1: Enable APIs & Create Infrastructure

*Next up...*
