#!/usr/bin/env bash
# board-sync.sh - reconcile the GitHub Project Kanban board against this repo's issues.
#
# The "Auto-add to project" workflow handles new issues going forward, but it only
# fires on create/update events. Anything filed while it was off, added by an import
# that hit a rate limit, or stranded by the async status race stays invisible. This
# script is the sweep that catches those.
#
# Usage:
#   bash scripts/board-sync.sh              # show what is out of sync, change nothing
#   bash scripts/board-sync.sh --apply      # actually add and set status
#
# Runbook: docs/runbooks/github-project-kanban-board.md

set -uo pipefail
export LC_ALL=C

PROJECT_NUMBER="${BOARD_PROJECT_NUMBER:-8}"
OWNER="${BOARD_OWNER:-dzivkovi}"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null || die "gh not found"
command -v jq >/dev/null || die "jq not found"

# ---------------------------------------------------------------- rate limit
# Projects are GraphQL-only with a 5,000 point hourly budget, and a throttled
# gh returns EMPTY OUTPUT rather than an error. Refuse to run rather than
# report a healthy-looking but silently truncated board.
REM=$(gh api rate_limit -q '.resources.graphql.remaining' 2>/dev/null || echo 0)
if [ "${REM:-0}" -lt 200 ]; then
  RESET=$(gh api rate_limit -q '.resources.graphql.reset' 2>/dev/null || echo 0)
  die "GraphQL budget too low ($REM points). Resets at $(date -d "@$RESET" 2>/dev/null || echo "epoch $RESET"). Re-run then."
fi

# ------------------------------------------------------------------- lookups
say "Reading project $OWNER/$PROJECT_NUMBER ..."
PROJECT_ID=$(gh project view "$PROJECT_NUMBER" --owner "$OWNER" --format json -q .id) \
  || die "cannot read project $PROJECT_NUMBER"
[ -n "$PROJECT_ID" ] || die "empty project id (throttled?)"

# Resolve the Status field and its option ids at runtime, so the script keeps
# working if the board is ever rebuilt with fresh ids.
FIELDS=$(gh api graphql -f query='
query($id: ID!) { node(id: $id) { ... on ProjectV2 {
  fields(first: 30) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } } } } }' \
  -f id="$PROJECT_ID")

STATUS_FIELD=$(echo "$FIELDS" | jq -r '.data.node.fields.nodes[]|select(.name=="Status")|.id')
OPT_BACKLOG=$(echo "$FIELDS" | jq -r '.data.node.fields.nodes[]|select(.name=="Status")|.options[]|select(.name=="Backlog")|.id')
OPT_DONE=$(echo "$FIELDS"    | jq -r '.data.node.fields.nodes[]|select(.name=="Status")|.options[]|select(.name=="Done")|.id')
[ -n "$STATUS_FIELD" ] && [ -n "$OPT_BACKLOG" ] && [ -n "$OPT_DONE" ] \
  || die "could not resolve the Status field or its Backlog/Done options"

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)

# --------------------------------------------------------------- fetch state
# Fetch the board ONCE. item-list paginates the whole board and is the single
# most expensive call here; never put it inside a loop.
BOARD=$(gh project item-list "$PROJECT_NUMBER" --owner "$OWNER" --limit 400 --format json)
BOARD_COUNT=$(echo "$BOARD" | jq '.items|length')
[ "$BOARD_COUNT" -gt 0 ] || die "board came back empty - almost always throttling, not an empty board"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "$BOARD" | jq -r '.items[]|select(.content.number != null)|.content.number' | tr -d '\r' | sort -u > "$TMP/on_board"

# Every board issue NOT in Done. A completed issue parked anywhere else is a
# stale card, whichever column it sits in: the async "Item added" workflow
# racing an explicit status write leaves it in Backlog, and a card set by hand
# before its PR merged leaves it in In Review. Both shapes were live on this
# board on 2026-09-12, which is why this is not scoped to Backlog alone.
echo "$BOARD" | jq -r '.items[]|select(.status!="Done" and .content.number != null)|.content.number' | tr -d '\r' | sort -u > "$TMP/not_done"

gh issue list --state open --limit 500 --json number -q '.[].number' \
  | tr -d '\r' | sort -u > "$TMP/open"
gh issue list --state closed --limit 800 --json number,stateReason \
  -q '.[]|select(.stateReason=="COMPLETED")|.number' | tr -d '\r' | sort -u > "$TMP/completed"

# Issues closed as NOT_PLANNED are deliberately never added: listing abandoned
# work as Done overstates what shipped, and it is the first thing a reviewer
# notices. They are REPORTED when already present, never removed automatically:
# deleting a card is the only thing this sweep could do that looks
# irreversible, so it stays the operator's call.
gh issue list --state closed --limit 800 --json number,stateReason \
  -q '.[]|select(.stateReason=="NOT_PLANNED")|.number' | tr -d '\r' | sort -u > "$TMP/not_planned"

comm -23 "$TMP/open" "$TMP/on_board" > "$TMP/missing_open"
comm -23 "$TMP/completed" "$TMP/on_board" > "$TMP/missing_done"
comm -12 "$TMP/not_done" "$TMP/completed" > "$TMP/stale"
comm -12 "$TMP/not_planned" "$TMP/on_board" > "$TMP/abandoned"

N_OPEN=$(wc -l < "$TMP/missing_open"); N_DONE=$(wc -l < "$TMP/missing_done")
N_STALE=$(wc -l < "$TMP/stale"); N_ABANDONED=$(wc -l < "$TMP/abandoned")

say ""
say "Board: $BOARD_COUNT items   Repo: $(wc -l < "$TMP/open") open, $(wc -l < "$TMP/completed") completed"
say "  missing from board (open, -> Backlog): $N_OPEN   $(tr '\n' ' ' < "$TMP/missing_open")"
say "  missing from board (done, -> Done):    $N_DONE   $(tr '\n' ' ' < "$TMP/missing_done")"
say "  completed but not in Done:             $N_STALE   $(tr '\n' ' ' < "$TMP/stale")"
say ""

if [ "$N_ABANDONED" -gt 0 ]; then
  say "  NOT PLANNED but on the board:          $N_ABANDONED   $(tr '\n' ' ' < "$TMP/abandoned")"
  say "  Abandoned work under Done overstates what shipped. Remove by hand, per issue:"
  say "    ID=\$(gh project item-list $PROJECT_NUMBER --owner $OWNER --limit 400 --format json |"
  say "         jq -r '.items[]|select(.content.number==<N>)|.id')"
  say "    gh project item-delete $PROJECT_NUMBER --owner $OWNER --id \"\$ID\""
  say ""
fi

if [ $((N_OPEN + N_DONE + N_STALE)) -eq 0 ]; then
  say "Board is in sync. Nothing to add or move."
  exit 0
fi

if [ "$APPLY" -eq 0 ]; then
  say "Dry run. Re-run with --apply to make these changes."
  exit 0
fi

# ------------------------------------------------------------------- apply
set_status() { # item_id option_id
  gh project item-edit --id "$1" --project-id "$PROJECT_ID" \
    --field-id "$STATUS_FIELD" --single-select-option-id "$2" >/dev/null
}

add_issue() { # number option_id
  local num="$1" opt="$2" id
  id=$(gh project item-add "$PROJECT_NUMBER" --owner "$OWNER" \
        --url "https://github.com/$REPO/issues/$num" --format json 2>/dev/null | jq -r .id)
  if [ -z "$id" ] || [ "$id" = "null" ]; then
    say "  FAILED to add #$num"; return 1
  fi
  set_status "$id" "$opt" || { say "  added #$num but FAILED to set status"; return 1; }
  say "  #$num"
  sleep 0.4   # stay clear of secondary rate limits
}

FAILED=0
if [ "$N_OPEN" -gt 0 ]; then
  say "Adding open issues to Backlog:"
  while read -r n; do [ -n "$n" ] && { add_issue "$n" "$OPT_BACKLOG" || FAILED=$((FAILED+1)); }; done < "$TMP/missing_open"
fi
if [ "$N_DONE" -gt 0 ]; then
  say "Adding completed issues to Done:"
  while read -r n; do [ -n "$n" ] && { add_issue "$n" "$OPT_DONE" || FAILED=$((FAILED+1)); }; done < "$TMP/missing_done"
fi
if [ "$N_STALE" -gt 0 ]; then
  say "Repairing completed issues sitting outside Done:"
  while read -r n; do
    [ -n "$n" ] || continue
    id=$(echo "$BOARD" | jq -r --argjson n "$n" '.items[]|select(.content.number==$n)|.id')
    if [ -n "$id" ] && [ "$id" != "null" ]; then
      set_status "$id" "$OPT_DONE" && say "  #$n -> Done" || { say "  FAILED #$n"; FAILED=$((FAILED+1)); }
    fi
    sleep 0.4
  done < "$TMP/stale"
fi

say ""
if [ "$FAILED" -gt 0 ]; then
  say "Done, but $FAILED operation(s) failed. If gh started returning empty output you were throttled: check"
  say "  gh api rate_limit -q '.resources.graphql'"
  say "and re-run this script after the reset. It is idempotent."
  exit 1
fi
say "Board synced. Re-run without --apply to confirm."
