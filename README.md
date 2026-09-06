# YouTube BigQuery Pipeline

Daily automated pipeline that snapshots YouTube analytics into BigQuery for historical trend analysis. Built for the [KC Labs AI](https://www.youtube.com/@kylechalmersdataai) channel.

> This project was built live as a YouTube video: **"I Let Claude Code Build My Entire YouTube Analytics Pipeline."** The build prompt (`PROMPT.md`) was created using the [`/taches-cc-resources:create-prompt`](https://github.com/glittercowboy/taches-cc-resources) Claude Code skill. Claude Code then generated a [6-phase implementation plan](prompts/completed/001-youtube-bigquery-pipeline-plan.md) from that prompt.

> **Audited and repaired live in a follow-up video: "I Audited the BigQuery Pipeline Claude Code Built Me. It Was Wrong."** The audit prompt, the standing rules to paste into your own agent, and the five takeaways are in **[AUDITING-YOUR-DATA-WITH-AI.md](AUDITING-YOUR-DATA-WITH-AI.md)**.

OAuth verification pages: [YouTube Analytics Pipeline](https://kyle-chalmers.github.io/youtube-bigquery-pipeline/), [Privacy Policy](https://kyle-chalmers.github.io/youtube-bigquery-pipeline/privacy.html), and [Terms of Service](https://kyle-chalmers.github.io/youtube-bigquery-pipeline/terms.html).

---

## Architecture

```text
                            ┌──────────────────────────────────┐
                            │       Google Cloud Scheduler      │
                            │  (Daily @ 00:10 Phoenix time)    │
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
│  • Subscriber gains │                    │ at runtime
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
                │        video_metadata (dimension table)          │
                │        ┌──────────────────────────────┐          │
                │        │ video_id, title, duration,   │          │
                │        │ type, tags, published_at     │          │
                │        │ (refreshed daily — Data API) │          │
                │        └─────┬──────────┬─────────┬───┘          │
                │          1:1 │      1:1 │   1:many│              │
                │              ▼          ▼         ▼              │
                │  ┌───────────────┐ ┌──────────┐ ┌────────────┐  │
                │  │ daily_video_  │ │ daily_   │ │ daily_     │  │
                │  │ stats         │ │ video_   │ │ traffic_   │  │
                │  │               │ │ analytics│ │ sources    │  │
                │  │ views, likes, │ │          │ │            │  │
                │  │ comments      │ │ watch    │ │ source,    │  │
                │  │ (cumulative)  │ │ time,    │ │ views,     │  │
                │  │               │ │ CTR,subs │ │ watch time │  │
                │  │ (Data API)    │ │(Ana. API)│ │ (Ana. API) │  │
                │  └───────────────┘ └──────────┘ └────────────┘  │
                │                                                  │
                │  Join key: video_id (see Semantic note below)    │
                │  Analytics tables: PARTITION BY activity_date    │
                │                                                  │
                │  reporting_* (19 raw tables, one per report type)│
                │  + reporting_ingest_ledger                       │
                │  PARTITION BY report_date, written by the second │
                │  function below, never by the daily pipeline     │
                └──────────────────────────────────────────────────┘

                ┌──────────────────────────────────┐
                │  Cloud Scheduler (08:00 + 14:00) │
                └────────────────┬─────────────────┘
                                 ▼
┌─────────────────────┐   ┌──────────────────────────────────┐   ┌──────────────────────┐
│ YouTube Reporting   │──▶│  Cloud Function                  │──▶│  Cloud Storage       │
│     API v1          │   │  youtube-reporting-ingest        │   │  raw report archive  │
│  • Impressions/CTR  │   │  1. List every retained report   │   │  (every CSV, forever)│
│  • Per-video daily  │   │  2. Newest generation per day    │   └──────────────────────┘
│    activity, traffic│   │  3. Archive, parse by header     │
│    detail, devices, │   │  4. One transaction per day:     │
│    demographics ... │   │     assert, replace, ledger      │
│  (same OAuth2 token)│   └──────────────────────────────────┘
└─────────────────────┘
```

**Data flow summary:**

- **[Cloud Scheduler](https://cloud.google.com/scheduler)** triggers the Cloud Function once daily
- **[Cloud Function](https://cloud.google.com/functions)** `youtube-bigquery-pipeline` calls the Data and Analytics APIs, then writes to 4 BigQuery tables
- A second Cloud Function, `youtube-reporting-ingest`, pulls the [Reporting API](https://developers.google.com/youtube/reporting) bulk reports into 19 raw `reporting_*` tables and archives every report file to Cloud Storage
- **[Data API](https://developers.google.com/youtube/v3)** (API key) provides video metadata and public stats
- **[Analytics API](https://developers.google.com/youtube/analytics)** (OAuth2) provides watch time, traffic sources, subscriber changes. Not impressions: those come only from the Reporting API.
- **[Secret Manager](https://cloud.google.com/secret-manager)** stores OAuth2 credentials so no secrets live in code
- **[BigQuery](https://cloud.google.com/bigquery)** stores daily snapshots partitioned by date for efficient querying
- Everything runs within GCP free tier

---

## Why Three YouTube APIs?

YouTube has three separate APIs that give you different data. The first two are called by the daily pipeline function:

| | [Data API v3](https://developers.google.com/youtube/v3) | [Analytics API v2](https://developers.google.com/youtube/analytics) |
|--|-------------|-------------------|
| **What it gives you** | Public stats: views, likes, comments, video metadata | Private creator data: watch time, avg view duration, traffic sources, impressions, CTR, subscriber gains/losses |
| **Authentication** | API key (simple) | OAuth2 (must prove channel ownership) |
| **Data type** | Cumulative totals (views only go up) | Per-day activity metrics (not cumulative) |
| **Think of it as** | The "what" — how many views | The "why" — where views came from and how long people watched |

We need both to get the full picture. The Data API tells you a video has 10,000 views. The Analytics API tells you 60% came from YouTube search, viewers watched an average of 4 minutes, and the video gained 12 subscribers.

The third is the **[YouTube Reporting API (v1)](https://developers.google.com/youtube/reporting)**. Instead of answering queries, it generates one CSV per report type per day and keeps each file for 60 days. It is the only public source of thumbnail impressions and click-through rate, of `engaged_views`, of traffic-source detail (the actual search terms and the suggesting videos), and of device, playback-location, demographic, card, end-screen, sharing and playlist breakdowns. It accepts the same OAuth2 scope as the Analytics API, so the one stored refresh token serves all three.

| | [Reporting API v1](https://developers.google.com/youtube/reporting) |
|--|--|
| **What it gives you** | Bulk daily CSVs at native grain (video x day x subscribed status x country, and so on): impressions, CTR, engaged views, traffic detail, devices, demographics, cards, end screens, playlists |
| **Authentication** | OAuth2, same `yt-analytics.readonly` scope |
| **Data type** | Per-day activity; a day can be regenerated later with corrections, and only the newest generation is authoritative |
| **Think of it as** | The "everything YouTube Studio knows", delivered as files you must persist before they expire |

The catch is retention: a report file exists for 60 days after YouTube generates it, and a new job only backfills 30 days before its creation. That is why the ingest archives every file to Cloud Storage before it parses it, and why the jobs were created before the loader was written.

---

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| **Google Cloud SDK (`gcloud`)** | GCP project management, deployments | [Install guide](https://cloud.google.com/sdk/docs/install) |
| **`bq` CLI** | BigQuery management (included with gcloud) | Included with gcloud |
| **Python 3.11+** | Cloud Function runtime | [python.org](https://www.python.org/downloads/) |

### Install Google Cloud CLI

If you're using Claude Code, paste this prompt:

> Install the Google Cloud CLI (`gcloud`) on my machine. Detect my OS and architecture, download the correct package, run the installer, and walk me through `gcloud init` to authenticate and select my project. Here is the official documentation: https://cloud.google.com/sdk/docs/install-sdk

Or install manually via the [official guide](https://cloud.google.com/sdk/docs/install-sdk). The `bq` CLI is included with gcloud — no separate install needed.

### Definitions

| Term | What it means |
|------|---------------|
| **BigQuery** | Google's serverless data warehouse — think of it as a massive SQL database in the cloud where you only pay for what you query |
| **Cloud Function** | A small piece of code that runs in the cloud without you managing a server — you upload it, Google runs it when triggered |
| **Cloud Scheduler** | Google's version of a cron job — it triggers your Cloud Function on a schedule (daily, hourly, whatever) |
| **OAuth2** | An authentication method that lets your code access YouTube data on your behalf — more secure than just an API key, but more complex to set up |
| **API** | Application Programming Interface — the way your code talks to YouTube and BigQuery to send and receive data |

### GCP Authentication

The only manual step is logging in — this opens a browser window where you sign in with your Google account:

```bash
gcloud auth login
```

From there, Claude Code handled the rest (setting the project, enabling APIs, granting IAM roles, etc.) while I reviewed its output. If you're following along, you can do the same — just authenticate and let Claude Code take it from there.

### Environment Variables

Add to your `~/.zshrc` (or `~/.bashrc`):

```bash
export YOUTUBE_API_KEY="your-api-key-here"
export YOUTUBE_CHANNEL_ID="your-channel-id-here"
```

Then `source ~/.zshrc` to load them.

---

## BigQuery Schema

All tables live in the `youtube_analytics` dataset. `video_metadata` and `daily_video_stats` are partitioned by `snapshot_date`. `daily_video_analytics` and `daily_traffic_sources` are partitioned by `activity_date` and clustered by `video_id`.

**The four tables at a glance:**

| Table | Role | Source | Pattern |
|-------|------|--------|---------|
| `video_metadata` | Dimension table | Data API v3 | Refreshed daily (titles can change) |
| `daily_video_stats` | Fact table | Data API v3 | Append-only cumulative counters |
| `daily_video_analytics` | Fact table | Analytics API v2 | Append-only per-day activity metrics |
| `daily_traffic_sources` | Fact table | Analytics API v2 | Append-only per-day, one row per source type |

**Table relationships (star schema):**

`video_metadata` is the central dimension table. The fact tables join to it via `video_id` alone:

- `video_metadata` → `daily_video_stats` — **1:1** per video per day. Every video gets a stats row on every pipeline run.
- `video_metadata` → `daily_video_analytics` — **1:1** per video per day. Only videos with activity on the analytics date get rows.
- `video_metadata` → `daily_traffic_sources` — **1:many** per video per day. One row per traffic source type (e.g., `YT_SEARCH`, `SUGGESTED`, `BROWSE_FEATURES`).

**Semantic note (rewritten 2026-08-29):** `snapshot_date` used to mean two different things depending on which writer produced the row, and that ambiguity silently double-counted two days and hid three destroyed ones. The analytics tables now carry both columns explicitly: `activity_date` is the day the views happened, `snapshot_date` is the day we collected it. Join and group on `activity_date`. The old note read: For Data API tables (`video_metadata`, `daily_video_stats`), it's the date the pipeline ran and values are cumulative totals as of that day. For Analytics API tables (`daily_video_analytics`, `daily_traffic_sources`), it's the analytics date and values represent that single day's activity.

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
| `activity_date` | DATE | Day the views happened (partition key) |
| `snapshot_date` | DATE | Day the pipeline collected the row |
| `load_source` | STRING | Which writer produced it: `cron`, `backfill_YYYYMMDD`, `recovery_YYYYMMDD`, `gap_repair` |
| `video_id` | STRING | YouTube video ID |
| `estimated_minutes_watched` | FLOAT64 | Total watch time in minutes across all viewers |
| `average_view_duration_seconds` | FLOAT64 | How long the average viewer watched before leaving |
| `average_view_percentage` | FLOAT64 | Percentage of the video the average viewer watched (e.g., 45.0 = 45%) |
| `impressions` | INT64 | Always NULL. The Analytics API does not expose it; the Reporting API report `channel_reach_basic_a1` does |
| `impression_ctr` | FLOAT64 | Always NULL. Same reason as `impressions` |
| `subscribers_gained` | INT64 | Subscriptions gained from this video |
| `subscribers_lost` | INT64 | Subscriptions lost from this video |
| `shares` | INT64 | Times the video was shared (share button, copy link, etc.) |
| `annotation_click_through_rate` | FLOAT64 | Click rate on annotations (legacy — mostly NULL for newer videos) |
| `card_click_rate` | FLOAT64 | Click rate on info cards (the "i" popups added to videos) |

### `daily_traffic_sources`

Append-only from the YouTube Analytics API v2. One row per video per traffic source type — shows where viewers discovered each video.

| Column | Type | Description |
|--------|------|-------------|
| `activity_date` | DATE | Day the views happened (partition key) |
| `snapshot_date` | DATE | Day the pipeline collected the row |
| `load_source` | STRING | Which writer produced it: `cron`, `backfill_YYYYMMDD`, `recovery_YYYYMMDD`, `gap_repair` |
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

### Reporting API raw tables (`reporting_*`)

One table per report type, generated from `cloud_function/report_specs.py` into `sql/reporting_tables.sql` (a test asserts the two agree). Each table keeps the report's native grain: `report_date` (a Pacific-time day, the same convention as `activity_date`) plus the report's own dimensions, then its metrics, then provenance columns (`report_id`, `report_create_time`, `job_id`, `load_source`, `ingested_at`). Partitioned by `report_date`, clustered by `video_id`. Dimension values can be NULL: `channel_basic_a3` emits a channel-level row with no `video_id`, and detail dimensions are blank under YouTube's anonymisation threshold. Ratio columns (`*_ctr`, `*_rate`, `*_percentage`) are per-row averages and are never summed; recompute from totals.

`reporting_ingest_ledger` has one row per report file YouTube generated, with its status (`loaded`, `header_only`, `header_only_conflict`, `superseded`, `skipped_older`, `failed`), row count, hash, and archive URI. It is what the ingest reads to decide what to load, and what you read to see coverage: `sql/verification/phase2_reporting_raw.sql` has a coverage calendar query.

Facts about the source data worth knowing before querying it, all observed on this channel: `average_view_duration_percentage` is on a 0 to 100 scale and exceeds 100 on looped Shorts (it is exactly `average_view_duration_seconds` over the video's duration); `video_thumbnail_impressions_ctr` is a 0 to 1 fraction; `likes` and `dislikes` can be negative (net of removals on the day); `traffic_source_type` is a numeric code (5 search, 7 suggested, 3 browse, 9 external, 24 Shorts feed, 17 notifications); `channel_basic_a3` carries channel-level subscriber rows with a NULL `video_id` that hold roughly a third of all subscribers gained, so channel growth is the sum over all rows, not over videos; and `channel_basic_a3` and `channel_traffic_source_a3` disagree on a day's views by up to about 10 percent when their reports were generated at different times, so treat `channel_basic_a3` as the source of truth for views. `subscribed_status` values are `subscribed` and `not_subscribed` (lowercase snake case, not the `UNSUBSCRIBED` the docs might suggest); `average_view_duration_seconds` is 0 whenever `engaged_views` is 0 even with positive watch time, so per-row AVD on Shorts is not reproducible from the other columns; and `average_view_duration_percentage` exceeds 100 on looped playback of any video type, not only Shorts.

Three rules the ingest enforces, inside one BigQuery transaction per report: the newest `createTime` for a day wins and an older generation can never overwrite it; a report with only a header row never deletes a populated day (it is recorded as a conflict and alerted instead); and the ledger row commits with the data, so a crash cannot leave a loaded partition unrecorded or an emptied one behind.

### Growth views (`sql/views/`)

Twelve views sit on the Reporting tables and answer the growth questions the raw grain makes awkward. `setup/10_create_views.sh` creates them in filename order; they hold no data, so they are safe to iterate on. Every file's header states its grain, timezone (Pacific day), join cardinality and formulas, and `tests/test_views_sql.py` enforces that contract.

| View | Grain | Answers |
|---|---|---|
| `video_current` | one row per video | latest metadata snapshot; the only relation other views may read `video_metadata` through |
| `traffic_source_type_lookup` | one row per code | Reporting's numeric traffic codes with names and the Analytics API enum they correspond to |
| `video_daily_funnel` | video, day | impressions, CTR, clicks, views, engaged views, watch time, average view duration, subscribers per 1,000 views, non-subscriber view share, Shorts engaged-start share |
| `video_audience_growth` | video, day (plus a channel-level line) | which videos bring in non-subscribers and subscribers; channel-level subscriber rows are kept as their own line, never dropped |
| `video_traffic_detail_daily` | video, day, source, detail | search terms, suggested-from videos (internal vs external), external referrers |
| `video_ctr_by_surface_daily` | video, day, source, device | CTR read per surface, which is the only fair way to read it |
| `channel_device_mix_daily` | day, video type, device | phone vs TV vs desktop share of watch time |
| `video_end_screen_daily`, `video_cards_daily` | video, day, element or card type | end screen and card click rates |
| `channel_sharing_daily` | day, service | where shares go |
| `channel_demographics` | day, age group, gender | audience composition (percentages, never summed) |
| `channel_daily_summary` | day | views, engaged views, watch time, subscribers (including channel-level rows), likes, comments, shares, impressions, CTR and clicks rolled up to the channel day from `channel_basic_a3` and `reach_basic_a1`, with calendar 7 and 28 day windows and the count of days that fed each window |

**Reading views across 2026-08-24.** YouTube changed what a `view` is on that date for every format (counted from the first frame, including autoplay and hover; Shorts made the same change on 2025-03-31) and did not restate history. On this channel views per day rose 36 percent at the boundary while engaged views fell 13 percent, so any views-based trend that crosses 2026-08-24 is comparing two definitions. `engaged_views` is the definition-stable series (it is the old `view` for long-form), and every ratio in the views is exposed on both denominators (`non_subscriber_view_share` and `non_subscriber_engaged_share`, `subscribers_gained_per_1k_views` and `_per_1k_engaged_views`, `views_7d` and `engaged_views_7d`). Nothing in the raw tables is wrong; each row holds the definition in force on its day.

Two rules every view follows: aggregate each source to the target grain before any join, and recompute ratios from totals instead of averaging the source's per-row ratios. Two findings from building them, both observed 2026-09-05: the source occasionally reports a click-through rate above 1 on a one-impression row, so `clicks` can exceed `impressions` on such rows; and YouTube's per-row average view duration is reproducible from watch time over views on full-length videos (96 percent of single-segment days within one second) but not cleanly on Shorts, where a view has counted any start or replay since 2025-03-31. The Studio spot-check on 2026-09-02 then showed that Studio's "Average view duration" is watch time over *engaged* views for long-form and Shorts alike (2:44 for a video where watch time over views gives 64 s), which is what YouTube documents ("calculated from engaged views and their corresponding watch time", [Understand your content performance](https://support.google.com/youtube/answer/12220281)). Since [2026-08-24](https://blog.youtube/inside-youtube/engaged-views-youtube-explained/) a view counts from the first frame for every format, so long-form views now exceed engaged views too (0.64 engaged per view on this channel from that date, 0.998 before). The funnel's `avg_view_duration_seconds` therefore uses engaged views; `avg_view_duration_over_views_seconds` keeps the older definition for the pre-change history. In the funnel a metric is 0 when that day's report exists and omits the video, and NULL only when no report for that day has been loaded on that side. `docs/studio-comparison.md` says which YouTube Studio number to put next to which column.

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

Creates a daily trigger at 00:10 Phoenix time (`America/Phoenix` timezone — no DST surprises), ten minutes after the YouTube Data API quota resets at Pacific midnight so the pipeline is the first consumer of the day rather than the last (moved from 11:50 PM on 2026-09-05 after a quota-exhaustion failure) with 3 retries and exponential backoff.

### Step 7: Create Reporting API jobs and archive what exists

```bash
python3 setup/7_create_reporting_jobs.py            # dry run: shows which report types have no job
python3 setup/7_create_reporting_jobs.py --create   # creates them (each starts a 30-day backfill clock)
python3 setup/archive_reporting_raw.py --verify     # copies every retained report file to gs://<project>-youtube-reporting-raw
```

Do this before anything else in this section. Reports expire 60 days after generation; the archive is the only durable copy.

### Step 8: Create the Reporting tables and deploy the ingest function

```bash
bash setup/2_create_bigquery.sh                                                  # idempotent; now also applies sql/reporting_tables.sql
BQ_DATASET=youtube_analytics REPORTING_ENABLED=false bash setup/9_deploy_reporting_function.sh
python3 setup/backfill_reporting.py --dataset youtube_analytics --max 200         # first catch-up, from the API
python3 setup/backfill_reporting.py --dataset youtube_analytics --from-gcs         # then anything YouTube has since expired, from the archive
BQ_DATASET=youtube_analytics REPORTING_ENABLED=true bash setup/9_deploy_reporting_function.sh   # switch the ingest on
```

The first deploy uses `REPORTING_ENABLED=false`: the function answers with `skipped`, logs a WARNING the failure alert matches, and touches nothing, so the deploy itself can be checked. The last line switches it on. `REPORTING_ENABLED` has no default for the production function, so a later redeploy cannot silently switch it off. The script grants the function's service account `objectCreator` and `objectViewer` on the archive bucket only (it never deletes).

### Step 9: Schedule the ingest and set up alerts

```bash
FUNCTION_NAME=youtube-reporting-ingest JOB_NAME=youtube-reporting-daily SCHEDULE="0 8,14 * * *" bash setup/5_create_scheduler.sh
ALERT_EMAIL=you@example.com bash setup/6_setup_monitoring.sh
```

Twice a day because each report loads in about 15 seconds and a run is capped by `MAX_REPORTS_PER_RUN`. Both scripts are idempotent, and the scheduler script also grants its service account permission to invoke the function (without it the job fails with a 403 that only the scheduler-failure alert reports). The monitoring script creates four email alerts: the daily pipeline's analytics failure or a whole-pipeline crash (`Pipeline failed`); the Reporting ingest's failures, header-only conflicts and a switched-off kill switch; a per-report-type freshness alert the ingest raises itself when any type's newest loaded day is older than `REPORTING_STALE_DAYS`; and a Cloud Scheduler failure alert for runs that time out or never answer.

### Step 10: Create the growth views

```bash
BQ_DATASET=youtube_analytics bash setup/10_create_views.sh
bash scripts/verify_views.sh youtube_analytics
```

`CREATE OR REPLACE VIEW` is idempotent and touches no data. The verify script asserts every view's grain is unique, that the funnel and summary have no fan-out, that the summary reconciles to the raw tables, that channel-level subscribers survive, and that every traffic code in the data is named. It ends by printing three videos for the newest complete day in YouTube Studio's Advanced Mode column order for a side-by-side check.

---

## Staging and Verification

Nothing reaches production untested. `setup/8_create_staging.sh` creates `youtube_analytics_staging` seeded with a copy of the four production tables, and both functions deploy there with `FUNCTION_NAME=...-staging BQ_DATASET=youtube_analytics_staging`. The deploy scripts refuse both directions of a production/staging cross.

```bash
python3 -m pytest tests/ -q                                   # offline unit tests (also run by CI on every push)
bash scripts/verify_parity.sh                                 # staging vs prod, by business key and fingerprint
bash scripts/verify_reporting.sh youtube_analytics_staging    # grain, ledger, cross-source reconciliation
bash scripts/verify_views.sh youtube_analytics_staging        # view grain, fan-out, ratio recomputation, Studio spot-check rows
```

The queries behind those scripts live in `sql/verification/` as paste-ready blocks with the expected result written above each one, so they can be run by hand in the BigQuery console.

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
# The backfill REPLACES whole activity days. It keys its delete on activity_date and
# refuses to delete when the API returns nothing, but it will still overwrite any day in
# the range. Check what is there first, and prefer a narrow range.
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

**Quota math for the YouTube Data API:** Each pipeline run makes 2 `playlistItems.list` calls (1 unit each, paginating 63 videos at 50/page) + 2 `videos.list` calls (1 unit each, batching 63 videos at 50/batch) = **4 units total** out of 10,000 daily. The YouTube Analytics API is a separate API with its own quota — its calls do not count against the Data API's 10,000 unit limit.

**Note:** Cloud Scheduler's 3 free jobs is per **billing account**, not per project. If you run multiple GCP projects on the same billing account, all scheduler jobs across projects share the 3-job limit.

---

## Project Structure

```text
cloud_function/                # the only directory deployed to Cloud Functions
  main.py                      # daily pipeline entry point (Data + Analytics APIs)
  reporting_main.py            # Reporting ingest entry point (second function)
  youtube_data_api.py          # YouTube Data API v3 client
  youtube_analytics_api.py     # YouTube Analytics API v2 client
  youtube_reporting_api.py     # YouTube Reporting API v1 client (list, download)
  reporting_parser.py          # header-driven CSV parser, fails on schema drift
  report_specs.py              # schema registry for the 19 report types
  reporting_loader.py          # newest-generation selection, ledger, archive
  partition_replacer.py        # one-transaction partition replace with assertions
  bigquery_writer.py           # BigQuery writes for the four original tables
  oauth_credentials.py         # the one Secret Manager credential loader
  retry.py                     # the one retry policy (stdlib only)
  log_safety.py                # redacts API keys and tokens before anything is logged
  requirements.txt
setup/
  1_enable_apis.sh             # Enable GCP APIs
  2_create_bigquery.sh         # Create BigQuery dataset + original tables
  3_setup_oauth.sh             # OAuth2 setup guide
  4_deploy_function.sh         # Deploy the daily pipeline function
  5_create_scheduler.sh        # Create a Cloud Scheduler job (either function)
  6_setup_monitoring.sh        # Email alert policies
  7_create_reporting_jobs.py   # Create Reporting API jobs
  8_create_staging.sh          # Create and seed the staging dataset
  9_deploy_reporting_function.sh
  10_create_views.sh           # Create or replace the growth views in one dataset
  archive_reporting_raw.py     # Copy every retained report to Cloud Storage
  backfill_reporting.py        # Catch-up, replay from archive, deliberate overrides
  backfill_analytics.py        # Backfill historical Analytics API data
  generate_reporting_ddl.py    # Render sql/reporting_tables.sql from report_specs.py
  oauth_helper.py              # One-time OAuth consent flow
  _bootstrap.py                # sys.path shim so setup/ imports cloud_function/
scripts/
  check_recent_runs.sh         # Freshness of the four original tables
  verify_audit_fixes.sh        # The 2026-08 audit assertions
  verify_parity.sh             # Staging vs prod parity
  verify_reporting.sh          # Reporting tables and ledger assertions
  verify_views.sh              # Growth view assertions and Studio spot-check rows
sql/
  create_tables.sql            # BigQuery DDL (4 original tables)
  reporting_tables.sql         # GENERATED DDL (19 Reporting tables + ledger)
  verification/                # paste-ready verification blocks per phase
  views/                       # the twelve growth views, one file each
  sample_queries.sql           # Analytical queries
  verification_queries.sql     # Data integrity and backfill verification
tests/                         # offline pytest suite; python3 -m pytest tests/ -q
docs/
  studio-comparison.md         # which YouTube Studio number to compare to which column
prompts/
  completed/
    001-youtube-bigquery-pipeline-plan.md   # Claude Code's 6-phase implementation plan
    001-add-structured-cloud-logging.md     # Structured logging upgrade prompt
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

Created daily trigger at 11:50 PM Phoenix time (`America/Phoenix` — no DST) with OIDC authentication and 3 retries.

### Step 6: OAuth2 + Analytics API

Completed OAuth2 consent flow for the YouTube Analytics API. Configured consent screen, created Desktop app credentials, ran the browser-based authorization flow, and stored the refresh token + client credentials in Secret Manager. Analytics tables now populate with watch time, retention, subscriber impact, and traffic source data.

### Step 7: Historical Backfill

Backfilled Analytics API data from the channel's first video (October 16, 2025) through February 17, 2026 — 125 days of historical data. This populated `daily_video_analytics` and `daily_traffic_sources` with per-day metrics that the daily pipeline wouldn't have captured retroactively. Added verification queries (`sql/verification_queries.sql`) to confirm backfill coverage and spot gaps.
