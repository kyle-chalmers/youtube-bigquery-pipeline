# Takeaways from the hardening session

This is the companion to the video in which an AI coding agent added the YouTube Reporting API to this
pipeline, hardened the existing one, and promoted the whole thing to production over two days, with me
reviewing at each gate. The repo is the evidence; these are the things worth taking away if you run a
data pipeline with an agent doing the work.

## How the session was run

- **Plan first, in three moves: harden the seam, add the source, harden the rest.** The new source had to
  reuse a shared writer, credential loader and test harness, so those came first. The broad hardening of
  the old pipeline came last, after the new tables had proven the safer write path.
- **An independent review of the plan before any code.** A second model reviewed the plan and changed it
  (transactional replace, newest-generation rule, full-outer-join reconciliation, monitoring as a
  promotion prerequisite). Disagreements were recorded, not smoothed over.
- **Every phase closed the same way:** offline tests green, staging verification green, a second-model
  review plus five review agents on the diff, findings verified against the code before acting, a written
  record, and a paste-ready SQL file so I could check the data myself.
- **Nothing touched production until the staging copy had proven it**, and the promotion ran from a
  written runbook with a stop condition at every step.

## What to take away

- **Capture before you build.** Reporting API files expire 60 days after YouTube generates them. The first
  thing that shipped was an archive of every file to Cloud Storage, before a line of loader code existed.
  When the loader later needed a replay, the archive was there.
- **One copy of anything that can destroy data.** The old pipeline had two hand-copied writers and two
  retry wrappers, and the copies had drifted. The load-bearing rules (delete keyed on the activity date,
  refuse to delete on an empty response) now live in one place with tests that pin them.
- **Make each load one transaction with assertions in front of the delete.** Rows present, exactly the
  expected day, the configured channel, unique grain, not already loaded, no newer generation loaded. If
  any assertion fails, nothing changes. A ledger row commits with the data, so the table and its memory of
  what loaded cannot disagree.
- **An empty file is not a correction until a human says so.** A regenerated report with only a header row
  never deletes a populated day; it is recorded as a conflict and emailed. This repo lost three days once by
  trusting an empty response.
- **A verification script must fail loudly, or it is decoration.** Review found a check that printed PASS
  when its query failed, because the query ran inside a command substitution and its exit code vanished.
  Every check now assigns first and treats an empty result as a failure.
- **Reviewers produce leads, not verdicts.** Six reviewers found real defects the author missed: a
  header-only day that read as "no report", rolling windows that counted rows instead of days, a test
  regex that could never match the code it guarded. Each finding was reproduced before it was fixed, and
  one was recorded as a disagreement. A reviewer that hit its usage limit was recorded as not having run,
  never as an approval.
- **Compare against the source of truth, and read the documentation when it disagrees.** Checking three
  videos in YouTube Studio showed that "average view duration" is watch time over engaged views, not
  views. YouTube's own help page says so. It also surfaced that YouTube changed what a view means on
  2026-08-24 for every format, without restating history. The raw rows were never wrong; the metric's
  meaning had moved.
- **When a definition changes under you, keep the raw data, expose both denominators, and name the stable
  series.** Every ratio in the views exists on views and on engaged views, and the docs say which one to
  use for a trend that crosses the date.
- **Chase incidents to a mechanism, with evidence.** A missing day turned out to be the Data API quota,
  exhausted by another tool in the same cloud project ten minutes before the quota reset. The crash log
  also contained the API key inside a request URL, and the alert had not fired because it did not match a
  whole-pipeline crash. Three fixes came from one investigation: schedule moved, log redaction, alert
  string added.
- **A signed total can hide two errors.** The cross-source reconciliation read 0.14 percent because a
  partial day overcounted on one side exactly as revised days undercounted on the other. Fixing the
  partial day exposed it. The check is now a mean absolute daily difference.
- **Own the mistakes in the record.** Moving the nightly schedule cancelled the run in between, which I had
  been told would not happen. The day was recovered from the staging copy, the runbook now carries the
  rule, and the review record says what went wrong.
- **Reversibility is what lets a promotion run overnight.** Snapshots of the original tables, a kill
  switch on the new function, an archive that can replay any day, and a runbook that stops at the first
  failure with the switch off.
- **Hand the next session its decisions, not just its tasks.** The Phase 4 prompt lists what was decided
  and why, so the next agent does not relitigate the header-only policy or the AVD definition.
- **Public repo, private values.** Project, channel and job ids live in a gitignored folder; a tracked
  file that held a channel id from the original build was cleaned up on the way through.

## Do this for your own channel

Nothing in this repo is tied to my channel. The full path for another channel owner is
[docs/adopt-for-your-channel.md](adopt-for-your-channel.md): what you need, the order of steps and why each
one comes before the next, what to change and what to leave alone, and the traps found during the build.
It ends with this prompt, which you paste into your coding agent in your clone of the repo:

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
  then walk me through docs/studio-comparison.md so I can check three numbers in YouTube
  Studio myself.
```

## Where to look

- `README.md` for the architecture, the deployment order and the schema.
- `docs/diagrams/` for the diagram used on screen.
- `docs/studio-comparison.md` for what will and will not match YouTube Studio, with sources.
- `docs/adopt-for-your-channel.md` to run this for your own channel, including a prompt for your agent.
- `sql/verification/` for the checks, written so you can paste them into the console yourself.
