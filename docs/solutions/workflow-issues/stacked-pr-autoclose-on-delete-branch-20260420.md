---
title: "Stacked PR auto-closes when upstream merges with --delete-branch"
date: 2026-04-20
category: workflow-issues
tags: [git, github, gh-cli, stacked-prs, compound-engineering, merge-discipline]
components: [.github, merge workflow]
severity: medium
symptoms:
  - "Downstream PR shows `state: CLOSED` within seconds of upstream PR merge"
  - "`gh pr edit <downstream> --base main` fails with `GraphQL: Cannot change the base branch of a closed pull request`"
  - "`gh pr reopen <downstream>` fails with `GraphQL: Could not open the pull request`"
  - "`git diff origin/main..downstream-branch` shows negative line counts for files unrelated to the downstream work (artifact of stacked base divergence)"
root_cause: "Passing `--delete-branch` to `gh pr merge <upstream>` deletes the ref that served as the stacked downstream PR's base. GitHub is documented to auto-retarget such a downstream to `main` (changelog, 2020-05-19), but in practice the delete event can race ahead of the retarget, leaving the downstream PR closed with a deleted base ref. Once closed with a missing base, neither `gh pr edit --base main` nor `gh pr reopen` works — GitHub treats base-change and reopen as mutually exclusive with a deleted base ref."
---

# Stacked PR auto-closes when upstream merges with `--delete-branch`

## What we observed

On 2026-04-20, two stacked PRs were open against this repo:

- **PR #13** — `tests/eval-harness-grounded-golden-dataset`, base `main`.
- **PR #14** — `docs/testing-and-kb-layer-strategy`, base `tests/eval-harness-grounded-golden-dataset`.

Both were `MERGEABLE` / `CLEAN`. PR #13 was squash-merged with:

```bash
gh pr merge 13 --squash --delete-branch --subject "..." --body "..."
```

Within three seconds of PR #13's merge (`mergedAt: 22:08:26Z`), PR #14 transitioned to `CLOSED` (`closedAt: 22:08:29Z`) with its base still pointing at the now-deleted branch. Two recovery attempts failed:

```
$ gh pr edit 14 --base main
GraphQL: Cannot change the base branch of a closed pull request. (updatePullRequest)

$ gh pr reopen 14
GraphQL: Could not open the pull request. (reopenPullRequest)
```

The *branch* and its commits were intact on origin — only the PR object was orphaned.

## Why this is surprising

GitHub is documented to **auto-retarget** stacked downstream PRs, not close them. Per [GitHub's 2020-05-19 Pull Request Retargeting changelog](https://github.blog/changelog/2020-05-19-pull-request-retargeting/): when PR #A (base `main`, head `feat-1`) is merged and `feat-1` is deleted, any open PR #B with base `feat-1` has its base **rewritten to `main`** automatically. That's the promised behavior.

What happened here matches the **pre-2020 behavior** the changelog said was fixed: the downstream PR was closed outright. The known triggers for seeing this regression are:

1. **Race between `--delete-branch` and the retargeter.** `gh pr merge --squash --delete-branch` deletes the ref via API immediately on merge; if the retarget job hasn't fired yet, the delete wins and GitHub closes the downstream. Intermittently reported in [cli/cli#1096](https://github.com/cli/cli/issues/1096).
2. **Cross-repo / fork PRs.** Retargeting is documented only for same-repo PRs. Fork-to-upstream stacks get no protection. *(Not our case — both PRs were same-repo.)*
3. **Squash-merge path may bypass retarget.** Less clearly documented; may be a factor.

For a solo developer using `gh pr merge --squash --delete-branch` as the default flow, assume the race is possible every time.

## The fix (what was recovered)

Once PR #14 was closed with base = deleted ref, the recovery was:

```bash
# 1. Switch to the orphaned branch locally
git checkout docs/testing-and-kb-layer-strategy

# 2. Rebase onto the new main (drops the upstream commits that are already
#    part of main as the squash). SHA below is the base of the now-deleted
#    upstream branch.
git rebase --onto origin/main <upstream-head-sha> docs/testing-and-kb-layer-strategy

# 3. Force-push (safe — we're on a feature branch, not main)
git push --force-with-lease origin docs/testing-and-kb-layer-strategy

# 4. Open a new PR targeting main. The old PR number is gone; the new
#    number is whatever GitHub assigns next. Copy the body from the
#    closed PR so discussion context survives.
gh pr create --base main --head docs/testing-and-kb-layer-strategy \
  --title "..." --body "..."
```

The commits, review comments, and CI history of the closed PR remain visible on the old PR URL; only the live merge path is gone.

## Prevention (the actual lesson)

**Retarget every downstream PR to `main` *before* merging the upstream PR.** This is the one rule every stacked-PR tool (Graphite, ghstack, spr, git-town, depo.io) implements, and it's the only rule that makes the race impossible instead of improbable.

### The two-line guard for solo flows

Before merging an upstream PR, run:

```bash
# Retarget every open PR that stacks on <upstream-branch> to main
gh pr list --base <upstream-branch> --json number --jq '.[].number' \
  | xargs -I{} gh pr edit {} --base main

# Only then merge + delete the upstream branch
gh pr merge <upstream-pr> --squash --delete-branch
```

If the first command prints nothing, the upstream has no stacked downstreams and it's safe to merge. If it prints numbers, each gets retargeted before the delete fires — no race window.

### Alternatives considered and rejected (for this project)

- **Drop `--delete-branch`.** Works, but leaves dead branches accumulating on origin. Solo-dev tolerable but hides the problem rather than fixing it. Prefer the explicit retarget.
- **Adopt Graphite / ghstack / spr.** All solve this atomically server-side. Worth it for a team; overkill for a one-person research project where stacked PRs happen once a week at most.
- **Never stack PRs.** Would work but discards the review-scoping benefit (PR #13 = harness design, PR #15 = docs design — two distinct review focuses).

### When the rule kicks in

Check for downstream stack before *any* `gh pr merge --delete-branch` invocation:

```bash
gh pr list --base <the-pr-head-branch> --state open --json number,title
```

Empty output → safe to merge with `--delete-branch`. Non-empty → retarget first.

## What this does *not* explain

Why GitHub's 2020-era retarget didn't fire here is still not fully pinned down. Candidates are (a) the race, (b) squash-merge specifically bypassing retarget, or (c) something about `gh`'s order-of-operations when combining `--squash` and `--delete-branch`. The prevention rule works regardless — but if future debugging wants to characterize the bug itself, open an issue at [cli/cli](https://github.com/cli/cli/issues) with repro steps and the `gh --debug` trace from the merge call.

## References

- [GitHub Changelog — Pull Request Retargeting (2020-05-19)](https://github.blog/changelog/2020-05-19-pull-request-retargeting/) — names the auto-retarget rule.
- [Dave Pacheco — My workflow for stacked PRs on GitHub (2025)](https://www.davepacheco.net/blog/2025/stacked-prs-on-github/) — the solo-dev idiom this doc adopts.
- [Graphite — Merge Pull Requests](https://graphite.com/docs/merge-pull-requests) — atomic retarget mechanics (for reference).
- [isaacs/github#1557](https://github.com/isaacs/github/issues/1557) — "can't edit base of a closed PR" limitation.
- Related local doc: [../compound-engineering-four-artifacts-20260417.md](../compound-engineering-four-artifacts-20260417.md) — CE artifact separation, including issue / PR role boundaries.
