# AGENTS.md

**This file is a pointer. The project context lives in [`CLAUDE.md`](./CLAUDE.md). Read that.**

`AGENTS.md` used to be a full copy of `CLAUDE.md`, maintained by hand. The two diverged:
when `CLAUDE.md` was corrected on 2026-07-26 and again on 2026-08-29, this file was not,
so any agent loading `AGENTS.md` got the older and wronger of the two. It carried a
"Known Limitations" section that had been superseded twice, including a claim that
thumbnail-impression metrics were "not yet wired in" when they had in fact been probed
and rejected on the Analytics API.

Two copies of the same context is a bug, not redundancy. Keeping one file means the next
correction cannot land in only half the repo.

Everything an agent needs, for Claude Code, Codex, Gemini CLI or anything else:
**[`CLAUDE.md`](./CLAUDE.md)**.
