# Runbook: the GitHub Project Kanban board over this repo's issues

**Last verified:** 2026-09-12, against [Video-Intel project 8](https://github.com/users/dzivkovi/projects/8). Tracked as issue #223.

Turns the issue list into a Jira-style Kanban board (Backlog / In Progress / In Review / Done) with GitHub's own automation moving cards. Almost all of it is scriptable; exactly one step is not. The layout is the one from the `AI-assisted-SDLC-Project-scaffolding` README ("Step 3: Set Up Project Tracking"), which is a 2023-era click-through whose three stale claims are corrected inline below.

This board already existed before the runbook did. It was imported in 2026-08 with a five-column `Inbox / Todo / In Progress / Review / Done` layout and never written down, so on 2026-09-12 it was reshaped to the standard four columns and audited against the rules here. Section "Reshaping a board that already exists" records how that was done without losing a single card's column.

## Prerequisite: token scope

The `gh` token needs the `project` scope on top of `repo`. Check first, because every mutation below fails opaquely without it:

```bash
gh auth status        # look for 'project' in "Token scopes"
```

If missing: `gh auth refresh -s project`.

## Step 1: Create the project and link it (all CLI)

```bash
gh project create --owner dzivkovi --title "Video-Intel" --format json
```

Keep the returned `id` (`PVT_...`) and `number`. Then link the repo:

```bash
gh project link <number> --owner dzivkovi --repo video-intel
```

**Board visibility follows the repo, not the project.** `gh project create` makes the project private by default. This repo is public, so project 8 is public too, and that is the right setting: the board is part of the talk-track for the repo and every title on it is already visible in the issue list. For a private repo, leave the project private; a public board over a private repo leaks issue titles to anyone with the link.

## Step 2: Replace the default columns (CLI)

A new project ships with a `Status` single-select of Todo / In Progress / Done. Replace the option set in one mutation. Get the field id first:

```bash
gh api graphql -f query='
query { node(id: "<PROJECT_ID>") { ... on ProjectV2 {
  fields(first: 20) { nodes {
    ... on ProjectV2SingleSelectField { id name options { id name } } } } } } }'
```

Then overwrite the option set. `singleSelectOptions` is a full replacement, not a merge, so list every column you want:

```bash
gh api graphql -f query='
mutation { updateProjectV2Field(input: {
  fieldId: "<STATUS_FIELD_ID>"
  singleSelectOptions: [
    {name: "Backlog",     color: GRAY,   description: "Items to be worked on in the future"},
    {name: "In Progress", color: YELLOW, description: "Current work in development"},
    {name: "In Review",   color: PURPLE, description: "Pull requests awaiting feedback"},
    {name: "Done",        color: GREEN,  description: "Completed work"}
  ]}) { projectV2Field { ... on ProjectV2SingleSelectField { options { id name } } } } }'
```

Record the returned option ids; step 4 needs them. `description` is required by the API, so it is not optional decoration.

### Reshaping a board that already exists

The same mutation renames columns **in place** when each option carries its existing `id`. `ProjectV2SingleSelectFieldOptionInput` accepts an optional `id`; an option sent with its old id keeps every card assigned to it, an option sent without one is created fresh, and any old option not listed is dropped (and its cards lose their status). So the safe move is: keep every id that holds cards, rename freely, drop only empty options. This is what reshaped project 8 on 2026-09-12 (`Inbox` -> `Backlog`, `Review` -> `In Review`, `Todo` dropped because it held nothing), verified afterwards with the item-list breakdown in "Verify": 195 items before, 195 after, every one still in its column.

Check the per-column counts BEFORE the mutation. An option that looks unused may not be, and there is no undo.

## Step 3: Make it an actual board, not a table (CLI)

**Correction 1 to the old doc, which says to pick the "Board" template by clicking.** `gh project create` always produces a single `TABLE_LAYOUT` view, and there is no template flag. But `updateProjectV2View` is in the public schema, so the conversion is scriptable:

```bash
gh api graphql -f query='
mutation { updateProjectV2View(input: {
  viewId: "<VIEW_ID>", name: "Board", layout: BOARD_LAYOUT
}) { projectV2View { name layout } } }'
```

Get `<VIEW_ID>` from `views(first: 5) { nodes { id name layout number } }` on the project. A board view groups by `Status` automatically, so the four options from step 2 become the four columns. The same mutation renames a view: project 8's view was called `View 2` until 2026-09-12.

## Step 4: Add the issues (CLI, slow)

Two calls per item: add, then set status. There is no bulk endpoint.

```bash
ITEM_ID=$(gh project item-add <number> --owner dzivkovi --url "<issue_url>" --format json | jq -r .id)
gh project item-edit --id "$ITEM_ID" --project-id "<PROJECT_ID>" \
  --field-id "<STATUS_FIELD_ID>" --single-select-option-id "<OPTION_ID>"
```

Drive it from a script over `gh issue list --json url,stateReason`, with `sleep 0.4` between items to stay clear of secondary rate limits. `item-add` is idempotent (it returns the existing item rather than duplicating), so a failed run is safe to re-run. In practice, do not hand-roll this: `scripts/board-sync.sh --apply` (below) is exactly this loop with the guards already in place.

### The rate limit is the real constraint, and it is easy to blow

**Measured 2026-08-14 on a sibling repo: a single 169-item import exhausted the whole 5,000-point/hour GraphQL budget**, stranding the last 18 items and blanking every board query for 34 minutes. Projects are a GraphQL-only surface, so there is no REST fallback; `gh project` subcommands simply return empty, which reads exactly like "the board is empty" rather than "you are throttled".

Check the budget before and during any bulk run. This is REST and does not itself consume GraphQL points:

```bash
gh api rate_limit -q '.resources.graphql'   # {"limit":5000,"remaining":N,"reset":<epoch>}
date -d @<reset>                            # when it comes back
```

Two things burn it much faster than the arithmetic suggests:

- **`gh project item-add` / `item-edit` are not one point each.** `gh` resolves the project, its fields, and the item on each call, so budget well above the naive 2-calls-per-item.
- **Progress polling is the silent killer.** `gh project item-list --limit 400` paginates the whole board and costs far more than a mutation. Polling it during an import can outweigh the import itself. **Fetch the list ONCE into a variable and derive every number from that:**

```bash
J=$(gh project item-list 8 --owner dzivkovi --limit 400 --format json)
echo "$J" | jq -r '[.items[].status]|group_by(.)|map({s:.[0],n:length})|.[]'
echo "$J" | jq '.items|length'
```

If a run does get throttled, stop every watcher immediately, otherwise they re-burn the fresh budget the instant it resets, before the retry can use it.

**Map honestly.** Open issues -> Backlog, open PRs -> In Review, issues closed as `COMPLETED` -> Done. **Exclude issues closed as `NOT_PLANNED`**: putting abandoned work in Done misreports it as shipped, and it is the first thing a reviewer notices. Filter with `select(.stateReason=="COMPLETED")`. The 2026-08 import of this board did not apply that rule and left 12 abandoned issues under Done; they were removed on 2026-09-12 (#17-#23, #49, #52, #53, #121, #165). Removing a card is:

```bash
gh project item-delete 8 --owner dzivkovi --id "<ITEM_ID>"
```

Note the signature: the project NUMBER is positional and there is no `--project-id` flag, unlike `item-edit`. The two subcommands do not share a shape.

Expect `In Progress` to come out empty after an import, because nothing in a freshly-imported backlog is genuinely mid-flight. That is the correct result; drag cards in by hand.

## Step 5: Enable the automation (UI ONLY, the one manual step)

**Correction 2: this cannot be scripted.** GitHub exposes `deleteProjectV2Workflow` and nothing else; there is no create or update mutation for project workflows. The CLI can read their state but not set it.

The workflows already exist on every new project, disabled. Verify with:

```bash
gh api graphql -f query='
query { node(id: "<PROJECT_ID>") { ... on ProjectV2 {
  workflows(first: 30) { nodes { name number enabled } } } } }'
```

Then, in a logged-in browser, at `https://github.com/users/dzivkovi/projects/8/workflows`:

1. **Item added to project** -> Edit -> Set value -> **Backlog** -> "Save and turn on workflow"
2. **Item closed** -> Edit -> Set value -> **Done** -> "Save and turn on workflow"
3. **Pull request merged** -> Set value -> **Done** (this repo merges PRs squash-style straight to main, so a merged PR is finished work)

The Save button stays disabled until a value is picked. The sidebar's "Workflows (N enabled)" counter lags by a page load; do not trust it as confirmation. Re-run the `workflows` query above instead; `enabled: true` is the only reliable check. Project 8 has all seven built-in workflows enabled.

**Correction 3: that workflows URL is not dead.** It returns a GitHub 404 when the browser is signed out, because a private project is invisible to an anonymous visitor. A 404 here means "log in", not "the URL moved"; `/settings/workflows` does not exist and is not the fix. (Project 8 is public, so this bites less here than on a private board, but the edit page itself still requires login.)

## Step 6: Keep the board current (two different workflows, easily confused)

**"Item added to project" does NOT add anything.** It fires when an item is already being added and only sets its Status. The workflow that actually watches the repo is **"Auto-add to project"**, and it ships with a default filter of:

```text
is:issue,pr is:open label:bug
```

That matches almost nothing in a repo that uses `bug` sparingly, so it looks enabled-but-broken. Edit it to the filter you want before saving. For "every open issue and PR":

```text
is:issue,pr is:open
```

The edit panel shows a live "See N existing items that match this query" count. If that reads 0, the filter is wrong; do not save it. The workflow only auto-adds going forward, so it never backfills what already exists.

### The sweep script, for everything automation misses

Auto-add covers new issues, but not issues filed while it was off, items stranded by a rate limit mid-import, or cards left in the wrong column. [`scripts/board-sync.sh`](../../scripts/board-sync.sh) reconciles the board against the repo:

```bash
bash scripts/board-sync.sh            # report what is out of sync, change nothing
bash scripts/board-sync.sh --apply    # add missing items, move stale cards to Done
```

It resolves the Status field and its option ids at runtime rather than hardcoding them, so it survives the board being rebuilt. It **refuses to run** when the GraphQL budget is under 200 points, and treats an empty board response as throttling rather than as an empty board, which is the failure mode that makes a truncated sync look successful. Four checks:

| Check | What it catches | On `--apply` |
|---|---|---|
| open issue missing from board | filed while auto-add was off, or stranded mid-import | added to Backlog |
| completed issue missing from board | same, closed side | added to Done |
| completed issue not in Done | the async status race (Backlog), or a card set by hand before its PR merged (In Review); both were live on this board 2026-09-12 (#135, #188) | moved to Done |
| `NOT_PLANNED` issue on the board | an import that skipped the map-honestly rule | **reported only**, with the exact `item-delete` command; removal stays the operator's call |

`BOARD_PROJECT_NUMBER` and `BOARD_OWNER` override the defaults (8, `dzivkovi`) for running the same script against another board.

## Ordering trap: enable the workflows AFTER the bulk import

Do step 5 last. "Item added to project -> Backlog" fires asynchronously on every add, so if it is live during step 4 it races the explicit `item-edit`. A closed issue can land in Done and then get stomped back to Backlog milliseconds later, silently. If the order slips, do not assume the counts are right: run `scripts/board-sync.sh`, whose third check is exactly this repair.

## Verify

```bash
gh project item-list 8 --owner dzivkovi --limit 400 --format json \
  | jq -r '[.items[].status] | group_by(.) | map({status:.[0], n:length}) | .[]'
```

A card with `null` status is on the board but in no column; it means its `item-edit` failed and needs a re-run. State on 2026-09-12 after the reshape and audit: 184 items, Backlog 11, In Progress 1 (#223), In Review 0, Done 172.
