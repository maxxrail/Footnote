---
description: Build one part of the project from its spec (usage: /build-part 1)
argument-hint: <part number>
---

Build Part $ARGUMENTS of this project.

1. Read `CLAUDE.md`, `PROGRESS.md` and `docs/parts/PART-$ARGUMENTS.md` in full.
   If PROGRESS.md says a different part is current, stop and ask me first.
2. List anything that needs me (accounts, API keys, Docker, money) **before** you start.
3. Follow the "Workflow for every part" section of CLAUDE.md: new branch, small commits
   with tests, mutation checks, ADRs, CHANGELOG, PR.
4. Run every acceptance check in the spec and paste the results into PROGRESS.md.
5. Before opening the PR, use the `part-reviewer` subagent to check the work against the
   spec independently, and fix whatever it finds.
6. Stop and report: what was built, how I can try it, what was NOT verified, and what I
   need to do by hand.
