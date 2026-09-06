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

## Five things to do on your own pipeline

1. **Archive the raw source before you write the loader.** Reporting API files expire 60 days after
   YouTube generates them. The first thing that shipped was a copy of every file to Cloud Storage, keyed by
   report id, before a line of loader code existed. When the loader later needed a replay, the archive was
   there. Whatever your source is, if it can expire or be revised, keep the bytes.

2. **Put assertions in front of every delete, inside one transaction.** Rows present, exactly the expected
   day, the configured channel, unique grain, not already loaded, no newer generation loaded. If any fails,
   nothing changes, and the ledger row that records the load commits with the data. And decide up front that
   an empty file never deletes a populated day; this repo once lost three days by trusting an empty response.

3. **Make verification something you can paste, and something that can fail.** Every phase shipped a SQL
   file with the expected result written above each query, plus a script that runs them and treats a failed
   or empty query as a failure. Review found a check that printed PASS when its query errored, and a test
   whose regex could never match the code it guarded. Test the tests.

4. **Run independent reviewers at every gate, and treat what they say as leads.** A second model and five
   review agents looked at each phase's diff. They found a header-only day that read as "no report", rolling
   windows that counted rows instead of days, and a metric that let two errors cancel. Each finding was
   reproduced before it was fixed, one was recorded as a disagreement, and a reviewer that hit its usage
   limit was recorded as not having run rather than as an approval.

5. **Check three numbers against the source of truth, then read the documentation.** Three videos compared
   in YouTube Studio showed that "average view duration" is watch time over engaged views, not views, and
   that YouTube changed what a view means on 2026-08-24 without restating history. YouTube's help pages
   confirmed both. The raw rows were never wrong; the metric's meaning had moved, so the views now expose
   every ratio on both denominators and name the stable series.

## Smaller lessons

- One copy of anything that can destroy data: the old pipeline's two hand-copied writers had drifted.
- Chase an incident to a mechanism. One missing day led to a quota fix, log redaction and a new alert string.
- A signed total can hide two errors; the reconciliation is now a mean absolute daily difference.
- Moving a scheduler's time cancels the run in between. It is in the runbook now because it bit here.
- Reversibility (snapshots, a kill switch, the archive, a runbook that stops) is what lets a promotion run
  overnight.
- Hand the next session its decisions, not just its tasks, so nothing gets relitigated.

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
