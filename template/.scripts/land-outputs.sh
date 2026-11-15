#!/usr/bin/env bash
# Land the PM pass's own outputs (no dangling work, 2026-11-15).
#
# The sentinel pass writes evidence files (per-ticket reports, hourly
# sentinel-pass-*.md, launch readouts) into the PROJECT checkout. Nothing ever
# committed them: by 2026-11-15 gruvato's main checkout held 338 untracked
# evidence files, and every worker that later touched that checkout had to
# step around them. heartbeat.sh now runs this after every full pass.
#
# What it does, deterministically (no model involved):
#   1. Only on the default branch (main), never on a detached HEAD or a side
#      branch, never mid-merge/rebase.
#   2. Collects modified and untracked, non-ignored files under the land paths
#      ONLY (role.yaml `land.paths`, space-separated; default: the BMAD issue
#      evidence dir and docs/operations/launch-readouts). Anything else in the
#      checkout belongs to someone else and is left alone.
#   3. Skips a file written in the last LAND_QUIET_SECONDS (default 30): it may
#      still be mid-write. The next pass picks it up.
#   4. Commits exactly those paths (`git commit --only`), so nothing another
#      agent staged rides along. The global guard hooks run; a refusal is
#      logged, the paths are unstaged, and the pass still succeeds.
#   5. Fetches, merges origin/<branch> if it moved (a merge works with other
#      agents' dirty files; a rebase would not), and pushes. A failed merge is
#      aborted and the commit stays local for the next pass to push.
#
# It never deletes, stashes, resets or reverts anything, and it never fails the
# heartbeat: every exit is 0. LAND_OUTPUTS=0 turns it off for one run.
set -uo pipefail

ROLE_DIR="${ROLE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ROLE_YAML="$ROLE_DIR/role.yaml"
ROLE_YAML_READER="$ROLE_DIR/.scripts/lib/role-yaml.py"
QUIET="${LAND_QUIET_SECONDS:-30}"

log() { printf '[land-outputs] %s\n' "$*"; }

[[ "${LAND_OUTPUTS:-1}" == 0 ]] && { log "disabled (LAND_OUTPUTS=0)"; exit 0; }

repo="${REPO_ROOT:-}"
if [[ -z "$repo" ]]; then
  repo="$(git -C "$ROLE_DIR" rev-parse --show-toplevel 2>/dev/null)" || { log "not in a git repo; nothing to land"; exit 0; }
fi
cd "$repo" || exit 0

agent="$(python3 "$ROLE_YAML_READER" "$ROLE_YAML" agent_id 2>/dev/null || true)"
agent="${agent:-pm}"
paths_cfg="$(python3 "$ROLE_YAML_READER" "$ROLE_YAML" land.paths 2>/dev/null || true)"
paths_cfg="${LAND_PATHS:-${paths_cfg:-_bmad-output/implementation-artifacts/issue-evidence docs/operations/launch-readouts}}"

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" || { log "detached HEAD; not landing"; exit 0; }
default="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)"
default="${default#origin/}"; default="${default:-main}"
[[ "$branch" == "$default" ]] || { log "on $branch, not $default; not landing"; exit 0; }
gitdir="$(git rev-parse --git-dir)"
for marker in MERGE_HEAD rebase-merge rebase-apply CHERRY_PICK_HEAD REVERT_HEAD; do
  [[ -e "$gitdir/$marker" ]] && { log "$marker in progress; not landing"; exit 0; }
done

declare -a present=()
for p in $paths_cfg; do
  case "$p" in /*|*..*) log "refusing land path $p (must be repo-relative)"; continue;; esac
  [[ -e "$p" ]] && present+=("$p")
done
(( ${#present[@]} )) || { log "no land paths exist here; nothing to land"; exit 0; }

now="$(date +%s)"
declare -a files=()
while IFS= read -r -d '' f; do
  [[ -f "$f" && ! -L "$f" ]] || continue
  m="$(stat -c %Y -- "$f" 2>/dev/null || stat -f %m -- "$f" 2>/dev/null || echo 0)"
  (( now - m >= QUIET )) || continue
  files+=("$f")
done < <(git ls-files -z -m -o --exclude-standard -- "${present[@]}" | sort -zu)

if (( ${#files[@]} )); then
  if git add -- "${files[@]}" && git commit -q --only -m "evidence($agent): land pass outputs (${#files[@]} files)

Committed by .scripts/land-outputs.sh after the board pass, so the pass's own
evidence never dangles in the checkout. Paths: $paths_cfg" -- "${files[@]}"; then
    log "committed ${#files[@]} file(s)"
  else
    log "commit refused (guard hook or error); unstaging and leaving the files for a person"
    git reset -q -- "${files[@]}" 2>/dev/null || true
  fi
else
  log "nothing new under: $paths_cfg"
fi

# Push whatever is ahead, including an earlier pass's commit that failed to push.
git fetch -q origin "$default" 2>/dev/null || { log "fetch failed; will push next pass"; exit 0; }
ahead="$(git rev-list --count "origin/$default..HEAD" 2>/dev/null || echo 0)"
(( ahead > 0 )) || exit 0
if ! git merge-base --is-ancestor "origin/$default" HEAD; then
  if ! git merge -q --no-edit "origin/$default" >/dev/null 2>&1; then
    git merge --abort >/dev/null 2>&1 || true
    log "merge with origin/$default failed; commit kept locally for the next pass"
    exit 0
  fi
fi
if git push -q origin "HEAD:$default" 2>/dev/null; then
  log "pushed $ahead commit(s) to origin/$default"
else
  log "push failed; commit kept locally for the next pass"
fi
exit 0
