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

## What Claude Code Has Access To

Claude Code is building this project with the following access:

- **Shell:** Full terminal access — can run `gcloud`, `bq`, `curl`, `python3`, `git`, and any CLI tools
- **File system:** Read/write access to the repo and local config files (e.g., `~/.zshrc`)
- **gcloud CLI:** Authenticated to GCP project `primeval-node-478707-e9` — can enable APIs, create BigQuery resources, deploy Cloud Functions, manage Scheduler jobs
- **bq CLI:** Can create datasets, tables, and run queries against BigQuery
- **YouTube Data API v3:** Via `YOUTUBE_API_KEY` env var — can fetch video metadata and public stats
- **YouTube Analytics API:** Not yet accessible — requires OAuth2 setup with the channel owner's consent
- **Git:** Can stage, commit, and manage the local repo (pushes require user approval)
- **No access to:** GCP Console UI, browser-based OAuth flows, or the YouTube Studio dashboard. Kyle handles those manually when needed.

## Build Process Documentation

**As we build, continuously update README.md with the actual steps taken, commands run, and issues encountered.** After the build is complete, we'll organize the README into a polished guide. For now, document everything in the order it happens — this serves as both a build log and the foundation for the final README.

Include:
- Each setup/deployment step with the exact commands used
- Any errors hit and how they were resolved (great video content)
- Environment prerequisites and authentication steps
- Architecture decisions made along the way

## Research Findings (Pre-Build)

Completed the PROMPT.md research checklist. Results:

- **GCP Project ID:** `primeval-node-478707-e9`
- **Already enabled APIs:** BigQuery (+ related), YouTube Data API v3, Cloud Storage, Logging, Monitoring
- **Not yet enabled:** Cloud Functions, Cloud Scheduler, Secret Manager, YouTube Analytics API, Cloud Build
- **BigQuery datasets:** None (fresh start)
- **Cloud Functions:** None
- **Cloud Scheduler jobs:** None
- **YouTube API key:** Had to fix — the key in `~/.zshrc` was from a different project. Updated to match the key in `primeval-node-478707-e9`. Confirmed working against the channel.
- **Channel stats at build start (2026-02-17):** 63 videos, 278 subscribers, 30,565 views
- **OAuth2 credentials:** Not yet created — needed for Analytics API
