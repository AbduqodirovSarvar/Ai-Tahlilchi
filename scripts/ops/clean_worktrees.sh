#!/usr/bin/env bash
# Tidy up after parallel agent waves: merged wave/* branches and finished agent worktrees.
#
#   scripts/ops/clean_worktrees.sh [--apply] [--base BRANCH] [--min-idle-hours N]
#
# Without --apply nothing is changed: the script only lists what it WOULD do (dry run).
#
# It considers, relative to BASE (default: main):
#   - local branches wave/* and worktree-agent-* fully merged into BASE -> `git branch -d` (safe delete:
#     git refuses a branch that is not merged, so work can never be lost this way);
#   - worktrees under .claude/worktrees whose branch is merged into BASE, whose working tree is
#     clean and that had no git activity for N hours (default 6; the worktree of a running agent
#     looks "merged" too, the idle time protects it)  -> `git worktree remove`
#     (without --force: a dirty worktree is reported and kept);
#   - `git worktree prune` for worktree records whose directory is already gone.
# The branch checked out in the current directory and the main checkout are never touched.
# Unmerged branches and dirty worktrees are listed as "kept" with the reason.
#
# Exit codes: 0 ok, 1 a git command failed, 64 usage.
set -euo pipefail

APPLY=0
BASE="main"
MIN_IDLE_HOURS=6

log() { printf 'clean_worktrees: %s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --base) shift; BASE="${1:?--base needs a branch name}" ;;
        --min-idle-hours) shift; MIN_IDLE_HOURS="${1:?--min-idle-hours needs a number}" ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
    shift
done
case "$MIN_IDLE_HOURS" in ''|*[!0-9]*) echo "--min-idle-hours must be a whole number" >&2; exit 64 ;; esac

git rev-parse --verify --quiet "refs/heads/$BASE" >/dev/null || {
    echo "base branch '$BASE' does not exist" >&2
    exit 64
}

# The main checkout is the parent of the common git directory.
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_root="$(dirname "$common_dir")"
here="$(git rev-parse --show-toplevel)"
current_branch="$(git symbolic-ref --quiet --short HEAD || true)"

merged="$(git branch --merged "$BASE" --format='%(refname:short)')"
# A here-string, not a pipe: `grep -q` exits early and `pipefail` would turn the SIGPIPE into a failure.
is_merged() { grep -Fxq -- "$1" <<<"$merged"; }

run() {
    if [ "$APPLY" -eq 1 ]; then
        "$@"
    else
        log "[dry-run] $*"
    fi
}

# Hours since the last git activity (commit, checkout, index change) in worktree $1; very large
# when unknown, so "unknown" never blocks a cleanup but a fresh worktree always does.
idle_hours() {
    local pointer admin newest now
    pointer="$(sed -n 's/^gitdir: //p' "$1/.git" 2>/dev/null || true)"
    admin="${pointer:-$1/.git}"
    [ -d "$admin" ] || { echo 999999; return; }
    newest="$(find "$admin" -maxdepth 2 -type f -printf '%T@\n' 2>/dev/null | sort -n | tail -n 1)"
    newest="${newest%.*}"
    now="$(date +%s)"
    if [ -z "$newest" ]; then echo 999999; else echo $(((now - newest) / 3600)); fi
}

removed_worktrees=0
kept_worktrees=0
removed_branches=0
kept_branches=0
declare -A worktree_of_branch=()

# ---------------------------------------------------------------- worktrees
path=""
branch=""
process_worktree() {
    [ -n "$path" ] || return 0
    case "$path" in
        "$main_root"|"$here") return 0 ;;
        */.claude/worktrees/*) ;;
        *) return 0 ;;
    esac
    [ -z "$branch" ] || worktree_of_branch["$branch"]="$path"
    if [ -z "$branch" ] || ! is_merged "$branch"; then
        log "keep worktree $path (branch '${branch:-detached}' is not merged into $BASE)"
        kept_worktrees=$((kept_worktrees + 1))
    elif [ "$(idle_hours "$path")" -lt "$MIN_IDLE_HOURS" ]; then
        log "keep worktree $path (git activity within the last ${MIN_IDLE_HOURS}h: may be in use)"
        kept_worktrees=$((kept_worktrees + 1))
    elif [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]; then
        log "keep worktree $path (uncommitted changes)"
        kept_worktrees=$((kept_worktrees + 1))
    else
        log "remove worktree $path (branch '$branch' merged into $BASE, clean)"
        run git worktree remove "$path"
        unset 'worktree_of_branch[$branch]'
        removed_worktrees=$((removed_worktrees + 1))
    fi
}

while IFS= read -r line; do
    case "$line" in
        "worktree "*) process_worktree; path="${line#worktree }"; branch="" ;;
        "branch refs/heads/"*) branch="${line#branch refs/heads/}" ;;
    esac
done < <(git worktree list --porcelain; echo)
process_worktree

# ---------------------------------------------------------------- branches
delete_branch() {
    local name="$1"
    if [ "$name" = "$current_branch" ]; then
        log "keep branch $name (checked out here)"
        kept_branches=$((kept_branches + 1))
    elif [ -n "${worktree_of_branch[$name]:-}" ]; then
        log "keep branch $name (checked out in ${worktree_of_branch[$name]})"
        kept_branches=$((kept_branches + 1))
    elif is_merged "$name"; then
        log "delete branch $name (merged into $BASE)"
        run git branch -d "$name"
        removed_branches=$((removed_branches + 1))
    else
        log "keep branch $name (not merged into $BASE)"
        kept_branches=$((kept_branches + 1))
    fi
}

while IFS= read -r name; do
    [ -n "$name" ] || continue
    delete_branch "$name"
done < <(git branch --list 'wave/*' 'worktree-agent-*' --format='%(refname:short)')

# ---------------------------------------------------------------- prune
log "prune records of worktrees whose directory is gone"
if [ "$APPLY" -eq 1 ]; then
    git worktree prune
else
    git worktree prune --dry-run | sed 's/^/clean_worktrees: [dry-run] would prune: /' >&2
fi

mode="dry run"
[ "$APPLY" -eq 0 ] || mode="applied"
log "summary ($mode): worktrees removed=$removed_worktrees kept=$kept_worktrees; wave branches deleted=$removed_branches kept=$kept_branches"
