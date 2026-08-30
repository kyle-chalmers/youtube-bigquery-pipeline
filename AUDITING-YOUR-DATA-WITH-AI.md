# Auditing your own data with AI

This pipeline ran green every morning for months and was quietly wrong the whole time. Nothing
errored. No alert fired. The numbers were just not what they claimed to be.

I found it by accident, asking an AI agent about my channel's performance and having it tell me the
numbers underneath the question did not look right. This document is what came out of fixing it:
the prompt, the standing rules, and the five things worth taking with you.

Everything here is method rather than configuration, so it should transfer to any warehouse and most
pipelines.

---

## Five takeaways

**1. Reviewing your code and interrogating your data are different questions.**
Pointing AI at this repository weeks earlier returned a sensible list of engineering improvements and
not one of the wrong numbers. Reading code tells you whether the code looks right; only querying the
data tells you whether the numbers are.

**2. A finding you have not reproduced yourself is still just a claim.**
The checks worth trusting at the end were the ones written before the fix existed, because those
cannot be tuned toward it. Watch for the zero that means "this rule found no exceptions" rather than
"there are none": my own duplicate check found 42 when there were 63, because a filter I had not
questioned was hiding rows.

**3. Operate on production data safely.**
Copy before anything writes, and verify by counting both sides rather than trusting that the job
reported success. The copy is itself a production write, so it needs the same approval as the change
it is protecting you from.

**4. Expect to find more than you went looking for, and push on research depth.**
The audit found six problems, and fixing them surfaced a seventh nobody had predicted. The planned
pagination fix then turned out to be wrong, because `startIndex` quietly returns zero rows with an
HTTP 200, and getting to the real answer took sending the agent back after it read two web pages and
stopped.

**5. Scope it tight and stay inside the scope.**
Data work hands you things you did not go looking for, so an unscoped session grows faster than you
can follow it. Write the good ideas down for another day, ship the one you started, and keep the
commits small enough that reversing a decision means reversing one thing.

---

## Making a safe copy, for takeaway 3

Most warehouses already do this natively, and it is worth checking what you have before reaching for
anything new: Snowflake zero-copy clones, BigQuery table snapshots, and Delta and Iceberg time travel
all hand you a production-shaped copy for roughly the cost of the metadata.

Beyond that there is a category of git-for-data tooling, all built on separating metadata from data
so a branch costs pointers rather than a full copy:

| Tool | Where it fits |
|---|---|
| [lakeFS](https://lakefs.io) | branching and commits over object storage data lakes |
| [Project Nessie](https://projectnessie.org) | git-like versioning for Apache Iceberg catalogs |
| [Dolt](https://www.dolthub.com) | a relational database with branch, diff and merge on tables |
| [Neon](https://neon.com) | branching for Postgres |
| [Bauplan](https://www.bauplanlabs.com) | branching and rollback for lakehouse pipelines |

I have had one of Bauplan's cofounders on this channel, so treat that one as a mention rather than an
impartial recommendation. I have no relationship with the others.

---

## The audit prompt

Paste this into an agent that has **both** your pipeline repo and your warehouse reachable, whether
through an MCP server, a CLI it can run, or a notebook it can execute. There is one bracketed part
to replace, the link to your source system's documentation.

> You have access to this data pipeline repo and to its warehouse. If you have questions about the
> data or connecting to it, please let me know. Audit the DATA for correctness problems. Do not
> review the code for style, and do not give me generic data-quality advice.
>
> Read-only everywhere. In the warehouse: read-only statements only, meaning SELECT, SHOW, DESCRIBE
> and information_schema, with no DML, no DDL and no temp tables. Against the source system: GET
> requests only. Do not modify, delete or backfill anything, and do not propose a fix until I have
> seen the findings.
>
> For each table this pipeline writes, work out its true grain, meaning what one row actually
> represents, proven by counting duplicate keys rather than inferred from the key definition or the
> table name. Then work out what each date column ACTUALLY means by reading the code that writes it,
> not by trusting what it is named. If the writer lives outside this repo, read the loader's
> configuration and a raw source payload instead.
>
> Then write and RUN SQL that would expose a mismatch between what a column is named and what it
> contains. Look specifically for:
> - a date column whose meaning differs between two writers
> - a join that silently drops rows instead of returning zeros
> - gaps in a series that is supposed to be continuous
> - the same real-world event stored more than once under different keys
> - an API extract or export that could silently truncate as the source grows
> - a table whose history changed after it was collected: check partition or file last-modified times
>   against collection dates, and name every process that writes to the table. Two writers with
>   different delete keys will silently destroy each other's rows.
>
> The truncation check cannot be answered with warehouse SQL alone. Warehouse queries only show you
> what arrived. Read the extract and pagination code directly, and reconcile what landed against a
> documented source total or a sampled live call to the source.
>
> For every finding, give me the query that proves it and the actual numbers it returned. If a result
> would expose personal or customer data, give me the aggregate and the row count instead of the rows,
> suppress or combine any group small enough to identify someone, and ask me before returning
> identifiers or individual rows.
>
> Before you call anything a defect, check the behavior against the official documentation for the
> system that produced the data [link the source docs here], and tell me whether it is documented
> behavior my code mishandles or genuinely unexpected. Where the documentation is ambiguous or merely
> describes intent, probe the live behavior and report what you observed, not what the docs imply.
>
> For any category where you find nothing, state the rule you tested, what a violation would have
> looked like, and one query showing the check can actually detect a violation you construct on
> purpose. A clean result with no stated rule does not count as a clean result.
>
> If a completeness check cannot be performed because there is no documented source total and no live
> call available, say so. Do not infer completeness from the warehouse.
>
> If you find nothing in a category, say so plainly rather than inventing a concern.

If it comes back with generic advice instead of findings, reply: **"You have not run a single query
yet. Run them."**

---

## Building it into your agent

The prompt above is a one-off. This block is the durable version: add it to your `CLAUDE.md`,
`AGENTS.md`, or whichever instruction file your agent reads, so this is the default posture on data
work rather than something you have to remember to ask for.

```markdown
## Working with data

- Distinguish observed from inferred. A number you ran a query to get is observed. Anything else is
  inferred, and must be labelled as such.
- Every claim about the data comes with the query that produced it and the actual result. A finding
  without a query is a hypothesis.
- Read what writes a column before trusting what it is named. Column names describe intent; the
  writing code describes contents.
- State the grain of every table you touch, and say how you established it.
- A count of zero means "this rule found no exceptions", not "there are no exceptions". Show the
  rule beside the count.
- Documentation describes intent. Where behavior matters, probe it and report what you observed.
- Warehouse SQL shows you what arrived, never what was dropped on the way in. Anything about
  completeness at the source needs the extract code and a reconciliation against the source itself.
- Investigation is read-only. Any write to production data needs explicit approval first, including
  the backup itself, and having a backup is not approval.
- Aggregates are not automatically safe. Suppress or combine groups small enough to identify someone,
  and ask before returning identifiers or individual rows.
- Never delete or replace the original until the replacement exists and both sides are counted.
- Surface findings outside the requested scope rather than fixing them silently, and do not act on
  them in the current task. New findings get their own review.
- Ship one logical change per commit, so a rollback is surgical rather than all-or-nothing.
```

---

## What this found here

For the record, run against this repo and its warehouse:

| | |
|---|---|
| One date column meaning two different things | across four date ranges, not one boundary |
| Rows double counted | 237, in two tables |
| Days of history destroyed by an earlier repair job | 3 |
| Days never collected | 4 |
| Days recovered | 7 |
| Days still missing after the fix | 2, both genuinely zero-activity |

The second AI mattered too. The conclusions here went past a different model for adversarial review,
and several first-pass conclusions did not survive it, including one root cause that a single live
probe disproved outright.
