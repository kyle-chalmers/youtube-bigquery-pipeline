# YouTube BigQuery Pipeline — Project Context

## Project Purpose

Daily automated pipeline that snapshots YouTube analytics for the KC Labs AI channel into BigQuery. This enables historical trend analysis, content performance comparison, and growth tracking that YouTube Studio doesn't natively support.

**This build is being recorded as a YouTube video.** Keep the build process clean, demonstrable, and well-narrated through commit messages and README documentation. The video title is "I Let Claude Code Build My Entire YouTube Analytics Pipeline."

## YouTube Channel Details

- **Channel:** KC Labs AI (@kylechalmersdataai)
- **Channel ID:** `UCkRi29nXFxNBuPhjseoB6AQ`
- **Uploads Playlist:** `UUkRi29nXFxNBuPhjseoB6AQ` (replace `UC` with `UU` in channel ID)
- **Current content:** 12 full-length videos + 46+ shorts (58+ total, requires pagination)
- **Shorts threshold:** Duration <= 180 seconds

## Environment & Authentication

- **YOUTUBE_API_KEY:** Set in `~/.zshrc` — source it if not in env: `source ~/.zshrc`
- **YOUTUBE_CHANNEL_ID:** Set in `~/.zshrc`
- **gcloud CLI:** Installed and authenticated
- **GCP Project:** "My First Project" — get ID via `gcloud config get-value project`
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
- All 12 full-length videos and 46+ shorts are accessible via the uploads playlist

## Video Script Structure (for demo flow)

The video follows this section structure. The build should naturally align with these sections:

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
- **Libraries:** google-cloud-bigquery, google-api-python-client, google-auth

## Cost Expectations

Everything runs within GCP free tier:
- Cloud Functions: 2M free invocations/month (we use 1/day = 30)
- Cloud Scheduler: 3 free jobs (we use 1)
- BigQuery: 10GB storage + 1TB queries free (our data is tiny)
- YouTube Data API: 10,000 units/day (we use ~4)
