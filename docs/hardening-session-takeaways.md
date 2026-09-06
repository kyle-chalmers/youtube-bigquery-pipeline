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

## Five data engineering takeaways

1. **Land the raw source immutably before you transform anything.** Sources expire, get revised, and
   change shape. Keep the original bytes, keyed by whatever identifies a delivery, so any day can be
   replayed and any schema change can be re-parsed from the original. Here: every Reporting API file went to
   Cloud Storage before a line of loader code existed, and that archive later rebuilt production.

2. **Never mutate a table without asserting first, and do the assert and the mutate in one transaction.**
   Check that the incoming data is non-empty, covers exactly the slice you are about to replace, belongs to
   the right owner, is unique on its grain, and is newer than what is there. Write the load record in the same
   transaction so the table and its bookkeeping cannot disagree. Decide in advance that "empty" is a
   question for a human, not a delete. Here: six assertions in front of every partition replace, a ledger row
   committed with the data, and header-only files that alert instead of deleting.

3. **Verification must be runnable by hand and must be able to fail.** Ship the checks as plain SQL with
   the expected result next to each query, and wrap them in a script that treats a failed or empty query as a
   failure, never as a pass. Then test the tests: a guard that cannot match the code it guards is decoration.
   Here: review found a check that printed PASS when its query errored, and a regex test that matched
   nothing in the views it protected.

4. **Gate every phase with independent review, and treat findings as leads to reproduce.** Reviewers from
   a different model and specialised agents see what the author cannot. Reproduce each finding before
   acting, write down disagreements instead of smoothing them over, and record a reviewer that failed to run
   as exactly that, never as approval. Here: six reviewers per phase found a day that read as missing when
   it was empty, windows that counted rows instead of days, and a metric that let two errors cancel.

5. **Reconcile against the system of record, then read the definitions before you trust the numbers.**
   Two sources that both look right can disagree by a few percent for documented reasons, and a metric can
   change meaning under you without history being restated. Keep the raw values, expose ratios on every
   defensible denominator, and name the series that is stable across the change. Here: checking specific
   videos against YouTube Studio showed average view duration uses engaged views, and that "view" changed meaning
   on 2026-08-24; both confirmed in YouTube's own documentation.

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
