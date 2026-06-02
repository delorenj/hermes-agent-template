#!/usr/bin/env bash
# Repair a failing runtime-checkpoint service caused by an UNPOPULATED runtime
# submodule (`git add -A` -> "fatal: in unpopulated submodule" -> exit 128).
#
# Dry-run by default (diagnose + print plan). Pass --apply to mutate. The mutate
# path only ever runs `git init` / `fetch` / `reset --mixed` / `.gitignore` edits
# / `rm --cached` — it NEVER rewrites working-tree files, so live runtime state
# (incl. a large state.db) is preserved. It does NOT commit or push; run the
# role's checkpoint.sh afterward.
#
# Docs: docs/runbooks/runtime-checkpoint-repair.md
# Usage: repair-runtime-checkpoint.sh [--apply] <runtime-dir>
set -euo pipefail

APPLY=0; RT=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) RT="$a" ;;
  esac
done

die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note(){ printf '  %s\n' "$*"; }

[ -n "$RT" ] || die "usage: $0 [--apply] <runtime-dir>  (e.g. agents/hermes/pm/runtime)"
[ -d "$RT" ] || die "no such directory: $RT"

PARENT="$(git -C "$RT" rev-parse --show-superproject-working-tree 2>/dev/null || true)"
[ -n "$PARENT" ] || PARENT="$(git -C "$RT" rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$PARENT" ] || die "$RT is not inside a git repository"
RTABS="$(cd "$RT" && pwd)"
RELPATH="${RTABS#"$PARENT"/}"

MODE="dry-run"; [ "$APPLY" -eq 1 ] && MODE="APPLY"
printf '== runtime-checkpoint repair (%s) ==\n' "$MODE"
note "runtime: $RTABS"
note "parent:  $PARENT"
note "subpath: $RELPATH"

if git -C "$RTABS" rev-parse --git-dir >/dev/null 2>&1 && [ -e "$RTABS/.git" ]; then
  POPULATED=1; note "state:   runtime already has its own .git (re-attach not needed)"
else
  POPULATED=0; note "state:   UNPOPULATED submodule (no .git) — re-attach required"
fi

URL="$(git -C "$PARENT" config -f "$PARENT/.gitmodules" --get "submodule.$RELPATH.url" 2>/dev/null || true)"
[ -n "$URL" ] || die "no .gitmodules url for submodule '$RELPATH'"
note "remote:  $URL"
GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15' git ls-remote "$URL" >/dev/null 2>&1 \
  || die "remote not reachable (check SSH/network): $URL"
note "remote reachable: yes"

IGNORE_MARK='NOT part of the durable brain backup'
IGNORE_BLOCK='# Runtime working state — NOT part of the durable brain backup.
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3
*.lock
.update_check
lsp/
checkpoints/
sessions/
cron/
*_cache.json'

if [ "$APPLY" -eq 0 ]; then
  cat <<EOF

PLAN (dry-run; re-run with --apply):
  cd $RELPATH
  $( [ "$POPULATED" -eq 0 ] && echo "git init -q; git symbolic-ref HEAD refs/heads/main; git remote add origin <url>; git fetch origin; git reset --mixed origin/main" || echo "(already populated — skip re-init)" )
  git lfs install --local
  git checkout origin/main -- .gitattributes README.md   # restore tracked config
  ensure runtime/.gitignore has the volatile-state block
  git rm -r --cached --ignore-unmatch state.db state.db-wal state.db-shm cron
  # safety gate: git add -A --dry-run must show no >1MB blobs / secrets / volatile paths
  then: run the role's checkpoint.sh and (re)start the systemd checkpoint unit.
EOF
  exit 0
fi

cd "$RTABS"
if [ "$POPULATED" -eq 0 ]; then
  git init -q
  git symbolic-ref HEAD refs/heads/main
  git remote add origin "$URL" 2>/dev/null || git remote set-url origin "$URL"
  GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=20' git fetch -q origin
  git reset --mixed -q origin/main
  git branch --set-upstream-to=origin/main main >/dev/null 2>&1 || true
  note "re-attached HEAD -> $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
fi
git lfs install --local >/dev/null 2>&1 || true
git checkout origin/main -- .gitattributes README.md 2>/dev/null || true

touch .gitignore
if grep -q "$IGNORE_MARK" .gitignore 2>/dev/null; then
  note ".gitignore already has the volatile-state block"
else
  printf '\n%s\n' "$IGNORE_BLOCK" >> .gitignore
  note "appended volatile-state ignore block to .gitignore"
fi
git rm -r --cached --quiet --ignore-unmatch state.db state.db-wal state.db-shm cron 2>/dev/null || true

# ---- mandatory safety gate ----
staged(){ git add -A --dry-run 2>/dev/null | sed -E "s/^[a-z]+ '?//; s/'?$//"; }
BIG="$(staged | while read -r f; do [ -f "$f" ] && s=$(stat -c%s "$f" 2>/dev/null) && [ "${s:-0}" -gt 1048576 ] && printf '%s (%sMB)\n' "$f" "$((s/1048576))"; done)"
SECRETS="$(staged | grep -iE '(^|/)(\.env|\.env\.|auth\.json|auth\.lock)([./]|$)|\.(pem|key)$' || true)"
VOL="$(staged | grep -E '(^|/)(state\.db|lsp|sessions|checkpoints)(/|$)' || true)"
if [ -n "$BIG$SECRETS$VOL" ]; then
  printf 'SAFETY GATE FAILED — fix .gitignore before committing.\n' >&2
  [ -n "$BIG" ]     && printf '  >1MB blobs:\n%s\n' "$BIG" >&2
  [ -n "$SECRETS" ] && printf '  secrets:\n%s\n' "$SECRETS" >&2
  [ -n "$VOL" ]     && printf '  volatile state:\n%s\n' "$VOL" >&2
  exit 2
fi
note "safety gate passed: staged set is brain-only."
note "DONE. Now run the role's checkpoint.sh, then:"
note "  systemctl --user reset-failed <checkpoint-service> && systemctl --user start <checkpoint-service>"
