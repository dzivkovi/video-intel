---
title: "Only one writer per path: restore from a cp backup, give each mutating agent its own tree, stop the loop before the child"
date: 2026-10-31
category: workflow-issues
module: agent-workflow
problem_type: logic_error
component: development_workflow
symptoms:
  - "Uncommitted edits vanish after a revert-check restore done with `git checkout -- <file>`"
  - A guardrail deleted hours earlier reappears after a conflict was resolved with a blanket checkout of one side
  - A commit message claims three applied fixes but the diff holds one
  - "A reviewer's verdict describes code that no longer exists in the tree"
  - A good transcript is replaced by a few-hundred-byte stub that looks like a tool bug
root_cause: concurrency
resolution_type: workflow_improvement
severity: high
tags: [git, revert-check, worktree, concurrency, data-loss, review-agents, batch-loop]
---

# Only one writer per path: restore from a cp backup, give each mutating agent its own tree, stop the loop before the child

## Problem
Several workflow steps in this repo mutate files that someone else also holds: the revert-check that [core: real-caller] in `CLAUDE.md` mandates ("neuter the guard and confirm exactly the relevant test fails"), conflict resolution, parallel review agents, and an operator's shell loop over videos. Each time two writers touched one path, work was lost silently and nothing in the test suite could see it.

## Symptoms
- A revert-check restore done with `git checkout -- <file>` threw away uncommitted work at least twice.
- In one case, a blanket `git checkout --theirs .` during a conflict reinstated a guardrail that had been deleted on purpose hours earlier. No test can detect a returned guardrail paragraph.
- A falsifying reviewer sharing the implementer's worktree restored from its own backup, reverting two applied fixes; the next commit claimed three fixes while holding one.
- Edits made while an executing reviewer was mid-pass can leave its verdict describing code that was gone.
- In one batch loop, killing the hung child let the loop move on to a video that was being redone by hand, and the loop overwrote the good transcript with a stub.

## What Didn't Work
- `git checkout -- <file>` as the restore step. It restores from the index, and the index does not hold in-progress edits, so the "restore" silently drops them.
- `git reset --hard` "back to the last green state", as `specs/agent-rules.md` sec. 4 used to say (it now says to restore from a `cp` backup). It carries the same hazard for an agent's own uncommitted work.
- Repo-wide `ruff format .` (the former sec. 3 validation command) on a drifted tree. It reformats unrelated files, which breaks sec. 1 (surgical changes) and invites a `checkout` cleanup that then eats real edits.
- Reviewers working in the implementer's tree, and stopping only the stuck process of a batch.

## Solution
- **Back up, then mutate.** Immediately before any revert-check swap or pre-fix experiment, `cp file file.bak` and restore from that copy. To get the committed version without touching the working tree, use `git show HEAD:path > copy`.
- **Format only the diff.** Run `ruff format` on the files you changed, not the whole repo; the rules now say this too.
- **One mutating agent per tree.** Give each implementer or falsifying reviewer its own detached worktree (`git worktree add --detach <dir>`) or a copied scratch tree. Sec. 4 already isolates parallel sessions with worktrees (`git worktree add ../<repo>-<feature> <branch>`); the detached form is this doc's recommendation for agents inside one session.
- **Collect, then apply.** Gather all review findings before applying any, so no reviewer is mid-pass while the tree changes. Have reviewers pin their results to a `git archive` snapshot so a verdict names the exact code it judged.
- **Verify your own edit after any concurrent writer.** Grep the file for the line you added before you claim it in a commit.
- **Stop the loop, not the child.** When one item in a shell batch hangs, stop the whole loop first, then deal with the child. Re-running the clobbered video with `--force` repaired it.

## Why This Works
Every incident had the same shape: two writers, one path, and a restore or overwrite that trusted state another writer had changed. A `cp` backup is private to the writer that took it, a separate tree removes the shared path entirely, and stopping the loop removes the second writer before it reaches the item you are fixing by hand. The losses were silent because the surviving file was well-formed; only a writer that owns its path can tell a correct file from a stale one.

## Prevention
- Treat `git checkout --`, `git checkout --theirs .` and `git reset --hard` as banned restore steps in agent briefs; name the `cp` backup instead.
- Brief every review agent with its own tree and a snapshot reference.
- Add a `docs/troubleshooting.md` row for "transcript replaced by a tiny stub after a batch loop was interrupted" (manual: stop the loop, re-run that video with `--force`). No row exists yet.

## Related Issues
- `specs/agent-rules.md` sec. 1 (surgical changes), sec. 3 (validation command), sec. 4 (revert on failure, worktrees for parallel sessions)
- `CLAUDE.md` [core: real-caller], which mandates the revert-check this doc makes safe
