# Adopt this pipeline for your own channel

Everything here is reproducible for any YouTube channel. Nothing in the repo is specific to the
original channel: the project, dataset, region, channel id and alert address all come from
environment variables, and the owner's values live in a gitignored folder. This page is the
short path from "I found this repo" to "my channel's data is in my BigQuery", and it ends with
a prompt you can hand to a coding agent (Claude Code, Codex, Gemini CLI or similar) to do the
adaptation with you.

## What you need before starting

- A YouTube channel you own, and the Google account that owns it.
- A Google Cloud project with billing enabled. Everything runs inside the free tier at a
  personal-channel scale (two functions, three scheduler jobs, a few megabytes of storage and
  well under 1 GB of BigQuery a month). The channel account and the cloud project can be
  different Google accounts; the OAuth refresh token is what bridges them.
- On your machine: `gcloud`, `bq`, Python 3.11 or newer, and `git`.

## What you will end up with

| Piece | What it does |
|---|---|
| Cloud Function `youtube-bigquery-pipeline` | Nightly snapshot of your video catalogue and public counters (Data API) and per-video daily metrics and traffic sources a few days back (Analytics API). |
| Cloud Function `youtube-reporting-ingest` | Twice a day, loads every Reporting API bulk report YouTube has produced for your channel (19 report types: impressions and CTR, engaged views, traffic sources with search terms, devices, playback locations, demographics, cards, end screens, sharing, playlists). |
| BigQuery dataset `youtube_analytics` | Four snapshot and activity tables, nineteen `reporting_*` tables, an ingest ledger, and twelve growth views. |
| Cloud Storage bucket | Every Reporting API file ever downloaded, so history survives YouTube's 60-day retention. |
| Four email alerts | Pipeline crash or empty analytics, Reporting load failure or conflict, stale report types, scheduler failures. |
| Staging dataset and functions | A mirror where changes are proven before they touch production. |

## The order that works

The README's "Deployment (Step by Step)" section has the commands. This is the order and the
reasons, so an agent or a person does not skip the step that makes the next one safe.

1. **Copy `.env.example` to `.env` and fill it in.** Channel id (UC...), project, region, dataset
   name, alert email. Never commit `.env`. Create `.internal/` for anything private (it is
   gitignored); the repo's `CLAUDE.md` explains the public/private split.
2. **Enable the APIs** (`setup/1_enable_apis.sh`).
3. **Create the BigQuery dataset and tables** (`setup/2_create_bigquery.sh`). The Reporting
   tables come from a generated DDL file; do not hand-edit them, edit `report_specs.py` and
   regenerate.
4. **OAuth once** (`setup/3_setup_oauth.sh`, `setup/oauth_helper.py`): create an OAuth client,
   consent as the channel-owning account with the `yt-analytics.readonly` scope, store the
   refresh token in Secret Manager. One token serves both the Analytics and the Reporting API.
5. **Store the Data API key** in Secret Manager (`setup/4` reads it from there).
6. **Create the Reporting jobs first, then archive** (`setup/7_create_reporting_jobs.py`, then
   `setup/archive_reporting_raw.py`). Jobs backfill 30 days from creation and reports expire
   60 days after generation, so create them on day one even if nothing else is ready.
7. **Create staging** (`setup/8_create_staging.sh`) and deploy both functions there first
   (`FUNCTION_NAME=...-staging BQ_DATASET=youtube_analytics_staging`). Run
   `scripts/verify_reporting.sh youtube_analytics_staging` and
   `scripts/verify_views.sh youtube_analytics_staging` until they are green.
8. **Deploy to production**: the daily function (`setup/4_deploy_function.sh`), the ingest with
   the kill switch off (`REPORTING_ENABLED=false setup/9_deploy_reporting_function.sh`),
   backfill from the archive (`setup/backfill_reporting.py --dataset youtube_analytics
   --from-gcs`), verify, then redeploy with the switch on and create the schedulers
   (`setup/5_create_scheduler.sh` for each function) and alerts (`setup/6_setup_monitoring.sh`).
9. **Create the views** (`setup/10_create_views.sh`) and compare a few numbers against YouTube
   Studio using `docs/studio-comparison.md`. Studio is the source of truth; the guide says what
   will and will not match, and why.

## What to change, and what to leave alone

Change: everything in `.env`; the alert email; the schedule times if your quota day or time
zone differ (the nightly run is placed just after the Data API quota reset at Pacific midnight
on purpose); `SHORTS_THRESHOLD_SECONDS` if you classify Shorts differently.

Leave alone unless you know why: the writer's two properties (delete keyed on the activity
date, refuse to delete when the API returned nothing), the in-transaction assertions in the
Reporting replacer, the alert log strings (the monitoring script matches them literally and the
tests pin them), and the lookback of 6 days (the Analytics API has nothing for the most recent
three days).

## Things that bit the original build, so you do not have to find them again

- The Data API quota is per project and shared with every other tool using that project. One
  bulk upload elsewhere can starve the nightly run.
- YouTube changed what a "view" is on 2026-08-24 (counted from the first frame, every format)
  and did not restate history. Use `engaged_views` for trends that cross that date; average
  view duration in Studio is watch time over engaged views.
- Moving a scheduler's time cancels the run in between; trigger the skipped slot by hand.
- The refresh token uses a sensitive scope; keep the rotation procedure somewhere private.
- Two credentials on your machine: `gcloud auth login` for the CLIs, `gcloud auth
  application-default login` for the Python scripts. Both expire.

## A prompt for your coding agent

Paste this into Claude Code, Codex, or a similar agent opened in your clone of the repo. It is
written so the agent reads the repo first, asks you only for what it cannot find, and never
puts a secret or a channel-specific value into a tracked file.

```text
I want to run this repository's pipeline for my own YouTube channel. Work with me through it.

First, read CLAUDE.md, README.md (the Architecture, Why Three YouTube APIs, BigQuery Schema and
Deployment sections), docs/adopt-for-your-channel.md, .env.example and the setup/ scripts, in
that order. Then tell me in plain language what this pipeline will collect for my channel, what
it will cost, and what accounts and permissions it needs, before running anything.

Rules for the whole session:
- Ask me for values you cannot derive: my channel id, GCP project id, region, alert email, and
  whether the channel and the cloud project are different Google accounts. Put them in .env and
  in .internal/OWNER_CONFIG.md only; never in a tracked file, a commit message, or a log.
- Run the setup in the order docs/adopt-for-your-channel.md gives, one step at a time, showing
  me each command before it runs and its result after. Stop and tell me when a step needs
  something only I can do (the OAuth consent screen in a browser, enabling billing).
- Create the Reporting API jobs and run the archive script before anything else that can wait;
  reports expire.
- Deploy to the staging dataset and staging functions first, run both verify scripts there,
  and show me the results. Only propose the production steps after staging is green, and ask
  me before each production deploy or scheduler change.
- Do not change the writer's delete-then-load invariants, the transaction assertions, or the
  alert log strings. If something in the repo does not fit my channel, explain the trade-off
  and ask, rather than editing silently.
- When done, run scripts/verify_reporting.sh and scripts/verify_views.sh against production,
  then walk me through docs/studio-comparison.md so I can check specific numbers against
  YouTube Studio, the source of truth, myself.
```
