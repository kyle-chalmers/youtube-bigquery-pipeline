# Auditing your own data with AI

This pipeline ran green every morning for months and was quietly wrong the whole time. Nothing
errored. No alert fired. The numbers were just not what they claimed to be.

I found it by accident, asking an AI agent about my channel's performance and having it tell me the
numbers underneath the question did not look right. This document is what came out of fixing it:
the prompt, the standing rules, and the five things worth taking with you.

Everything here is method rather than configuration, so it transfers to any warehouse and any
pipeline.

---

## Five takeaways

### 1. Reviewing your code and interrogating your data are different questions

I had already pointed AI at this repository weeks earlier and asked it to review the code. It came
back with a sensible list of engineering improvements and did not mention a single one of the wrong
numbers. That is not a failure of the tool. Reading code tells you whether the code looks right.
Only asking the data tells you whether the numbers are right.

The prompt in the next section is the one that found the problems, and the difference is that it has
access to the warehouse as well as the repo, and it is told to run queries rather than form opinions.

### 2. A finding you have not reproduced yourself is still just a claim

At the end of the repair the agent produced a clean report saying everything reconciled. A report
that grades its own homework is not proof, and a test the agent designs can pass for the same reason
the fix is wrong.

So the checks I actually trusted were the ones written before the fix existed, from the audit at the
start. Those cannot be tuned to pass, because they were written by someone who did not yet know what
the answer was going to be. Same queries, run again, different answer.

One specific trap worth naming: a duplicate check returning zero means "this matching rule found no
exceptions", not "there are no exceptions". A weaker or wrongly-keyed rule produces exactly the same
zero. Show the rule beside the number.

### 3. Operate on production data safely

Take a copy before anything writes, and verify the copy row for row rather than trusting that it
completed. If a step later goes wrong, that copy is the only thing standing between you and a bad
afternoon.

That is the floor. Tools like [Neon](https://neon.com) and [Bauplan](https://www.bauplanlabs.com)
go further, giving you branching and versioning for data so you can develop against production-shaped
data without risking production itself. Either way the principle is the same: never let the only
copy of the truth be the one you are editing.

The related habit, which matters more than it sounds: never delete the old thing until the
replacement exists and you have counted both. That single check is the difference between a
migration and an incident.

### 4. Expect to find more than you went looking for, and push on research depth

The audit found four problems. The repair surfaced a fifth nobody had predicted, 174 duplicate rows
in a second table, discovered only because fixing the first problem made them visible.

More instructive was the last fix. The plan called for adding `startIndex` pagination to the API
extract. Straightforward, already scoped, already agreed. It turned out to be wrong: `startIndex` is
documented on that endpoint as a pagination mechanism, and probing it live returns zero rows with an
HTTP 200. Following the documented fix would have shipped silent truncation, which is the exact class
of bug this whole audit existed to find.

Getting to the real answer took pushing. When I asked the agent to check the official documentation,
it read two web pages and stopped. Before AI I would have read far more than two pages for a problem
like this: the reference docs, Stack Overflow, whatever else existed. So I sent it back and asked for
real research, and the answer that came back was a different mechanism entirely, sharding the request
by video id rather than paging through results.

Two things follow from that. Ask what an agent actually read before you accept a conclusion. And when
it hands you extra improvements mid-task, write them down for later rather than following them,
because a session that chases every rabbit hole never ships the thing you started.

### 5. Ship one logical change per commit

The temptation with an agent that can do ten things at once is to let it. Resist it. The pagination
work was in the same plan as everything else and it shipped as its own commit afterwards.

The reason is rollback. If something turns out wrong next week, you want to reverse one change rather
than unpick a single commit that did five unrelated things.

---

## The audit prompt

Paste this into an agent that has **both** your pipeline repo and your warehouse connected. Replace
the bracketed parts.

> You have access to this data pipeline repo and to its [BigQuery / Snowflake / Postgres] warehouse.
> Audit the DATA for correctness problems. Do not review the code for style, and do not give me
> generic data-quality advice.
>
> This audit is read-only. Run SELECT queries only. Do not modify, delete or backfill anything, and
> do not propose a fix until I have seen the findings.
>
> For each table, work out its true grain, and work out what each date column ACTUALLY means by
> reading the code that writes it, not by trusting what it is named.
>
> Then write and RUN SQL that would expose a mismatch between what a column is named and what it
> contains. Look specifically for:
> - a date column whose meaning differs between two writers
> - a join that silently drops rows instead of returning zeros
> - gaps in a series that is supposed to be continuous
> - the same real-world event stored more than once under different keys
> - an API extract or export that could silently truncate as the source grows
>
> The truncation check cannot be answered with warehouse SQL alone. Warehouse queries only show you
> what arrived. Read the extract and pagination code directly, and reconcile what landed against a
> documented source total or a sampled live call to the source.
>
> For every finding, give me the query that proves it and the actual numbers it returned. If a result
> would expose personal or customer data, give me the aggregate and the row count instead of the rows.
>
> Before you call anything a defect, check the behavior against the official documentation for the
> system that produced the data [link the source docs here], and tell me whether it is documented
> behavior my code mishandles or genuinely unexpected. Where the documentation is ambiguous or merely
> describes intent, probe the live behavior and report what you observed, not what the docs imply.
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
- Investigation is read-only. Any write to production data needs explicit approval first, and a
  backup is not approval.
- Never delete or replace the original until the replacement exists and both sides are counted.
- Surface findings outside the requested scope rather than fixing them silently. New findings get
  their own review.
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

The second AI mattered too. Every conclusion here went past a different model for adversarial review,
and several first-pass conclusions did not survive it, including one root cause that a single live
probe disproved outright.
