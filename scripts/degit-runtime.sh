#!/usr/bin/env bash
# degit-runtime — make a repo's Hermes agent runtime(s) PURE LOCAL state.
#
# Run from (or point --root at) the ROOT of a project repo. Discovers every
# agents/hermes/*/runtime under it and, in one shot, removes it from git and
# gitignores it so it never re-enters version control.
#
# Policy = pure-local (fleet decision D2, 2026-07-21): the runtime is no longer
# a git-tracked submodule NOR its own checkpoint repo. Per-agent memory
# durability moves to the per-repo Hindsight bank. The
# github.com/delorenj/agent-hm-*-pm remotes are left intact (recoverable) but
# are no longer pushed to. This is the INVERSE of repair-runtime-checkpoint.sh,
# which keeps the checkpoint repo alive.
#
# Per runtime:
#   Layer A (project repo, if one exists):
#     - gitlink submodule   -> git rm --cached <rel>
#                              + drop [submodule "<rel>"] from .gitmodules
#                              + rm -rf .git/modules/<rel>
#     - plain tracked files -> git rm -r --cached <rel>
#     - ensure .gitignore ignores agents/hermes/*/runtime/
#   Layer B (the runtime itself):
#     - if it has unpushed commits, push once (best-effort) unless --force
#     - rm -rf <runtime>/.git            (retire the checkpoint repo)
#     - write <runtime>/.gitignore = "*\n!.gitignore"   (pure-local marker)
#
# REPORTED but not touched here (fleet-level; handled by fleet-prune-debris.sh):
#   ~/.hermes/profiles/<profile> symlink, agents-registry.yaml runtime_repo,
#   and the now-dead -checkpoint.service/.timer units.
#
# Dry-run by default (prints the plan). --apply mutates. Idempotent: every step
# checks state first, so a second run is a clean no-op.
#
# Usage: degit-runtime.sh [--apply] [--force] [--root DIR]
set -euo pipefail

APPLY=0; FORCE=0; ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --root)  shift; ROOT="${1:-}" ;;
    -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
    *) printf 'degit-runtime: unknown arg: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note(){ printf '  %s\n' "$*"; }
# run CMD... — execute on --apply, otherwise print the plan line.
run(){ if [ "$APPLY" -eq 1 ]; then "$@"; else printf '  [would] %s\n' "$*"; fi; }

ROOT="${ROOT:-$PWD}"
[ -d "$ROOT" ] || die "no such directory: $ROOT"
ROOT="$(cd "$ROOT" && pwd)"
[ -d "$ROOT/agents/hermes" ] || die "no agents/hermes under $ROOT — run from a project repo root (or pass --root)"

MODE="dry-run"; [ "$APPLY" -eq 1 ] && MODE="APPLY"
printf '== degit-runtime (%s) :: %s ==\n' "$MODE" "$ROOT"

# Project repo root (the superproject). Empty when the project has no git at all
# (e.g. DeLoDocs, pjangler) — Layer A is then skipped.
PROJ="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$PROJ" ]; then note "project repo: $PROJ"; else note "project repo: (none — Layer A skipped)"; fi

# Discover runtimes: agents/hermes/<role>/runtime (depth 2 under agents/hermes).
mapfile -t RUNTIMES < <(find "$ROOT/agents/hermes" -mindepth 2 -maxdepth 2 -type d -name runtime 2>/dev/null | sort)
[ "${#RUNTIMES[@]}" -gt 0 ] || die "no agents/hermes/*/runtime found under $ROOT"
note "runtimes: ${#RUNTIMES[@]}"

CHANGED=0

ensure_project_ignore(){ # $1 = relpath of runtime from project root
  local rel="$1" gi="$PROJ/.gitignore"
  # Prefer a broad, role-agnostic rule; fall back to the exact path.
  if git -C "$PROJ" check-ignore -q "$rel" 2>/dev/null; then
    note ".gitignore already ignores $rel"; return 0
  fi
  local rule="agents/hermes/*/runtime/"
  if [ -f "$gi" ] && grep -qxF "$rule" "$gi" 2>/dev/null; then
    note ".gitignore already has rule: $rule"; return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    printf '\n# Hermes agent runtime — pure-local state, never tracked (degit-runtime).\n%s\n' "$rule" >> "$gi"
    note "appended ignore rule to .gitignore: $rule"
  else
    printf '  [would] append %s to %s\n' "$rule" "$gi"
  fi
  CHANGED=1
}

layer_a(){ # $1 = absolute runtime dir
  local rt="$1" rel
  [ -n "$PROJ" ] || return 0
  rel="${rt#"$PROJ"/}"
  # Is it a gitlink submodule (mode 160000) in the index?
  if git -C "$PROJ" ls-files --stage -- "$rel" 2>/dev/null | grep -q '^160000 '; then
    note "Layer A: '$rel' is a gitlink submodule -> de-submodule"
    run git -C "$PROJ" rm --cached -q -- "$rel"
    if [ -f "$PROJ/.gitmodules" ] && git -C "$PROJ" config -f "$PROJ/.gitmodules" --get "submodule.$rel.url" >/dev/null 2>&1; then
      run git -C "$PROJ" config -f "$PROJ/.gitmodules" --remove-section "submodule.$rel"
      # Drop .gitmodules entirely once no [submodule] sections remain.
      # (In dry-run the section is still present, so this stays conservative.)
      if [ ! -f "$PROJ/.gitmodules" ] || ! grep -q '^\[submodule' "$PROJ/.gitmodules" 2>/dev/null; then
        run rm -f "$PROJ/.gitmodules"
        run git -C "$PROJ" rm --cached -q --ignore-unmatch -- .gitmodules
      fi
    fi
    # The real gitdir (runtime layer already removed the runtime/.git pointer).
    [ -e "$PROJ/.git/modules/$rel" ] && run rm -rf "$PROJ/.git/modules/$rel"
    CHANGED=1
  elif git -C "$PROJ" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
    note "Layer A: '$rel' is tracked as plain content -> unstage"
    run git -C "$PROJ" rm -r --cached -q -- "$rel"
    CHANGED=1
  else
    note "Layer A: '$rel' is not tracked in the project repo (good)"
  fi
  ensure_project_ignore "$rel"
}

layer_b(){ # $1 = absolute runtime dir
  local rt="$1"
  # hard guard before any rm -rf
  case "$rt" in */runtime) : ;; *) die "refusing: '$rt' does not end in /runtime" ;; esac
  [ -d "$rt" ] || die "refusing: runtime dir vanished: $rt"

  if [ -e "$rt/.git" ]; then
    # Guard unpushed local commits: push once (best-effort) unless --force.
    local ahead=0
    if git -C "$rt" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
      ahead="$(git -C "$rt" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
    else
      ahead="unknown(no-upstream)"
    fi
    note "Layer B: runtime has .git (unpushed ahead: $ahead)"
    if [ "$ahead" != "0" ]; then
      if [ "$FORCE" -eq 1 ]; then
        note "  --force: discarding local .git without pushing ($ahead ahead)"
      else
        note "  attempting one final push before retiring .git (--force to skip)"
        if [ "$APPLY" -eq 1 ]; then
          if GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=20' \
               timeout 45 git -C "$rt" push 2>/dev/null; then
            note "  final push OK"
          else
            note "  MANUAL: final push FAILED and commits are unpushed — skipping .git removal for this runtime (re-run with --force to discard, or push by hand)"
            return 0
          fi
        else
          printf '  [would] git -C %s push   (final backup)\n' "$rt"
        fi
      fi
    fi
    run rm -rf "$rt/.git"
    CHANGED=1
  else
    note "Layer B: runtime already has no .git (good)"
  fi

  # Pure-local marker: ignore everything except the marker itself.
  local gi="$rt/.gitignore" want=$'*\n!.gitignore'
  if [ -f "$gi" ] && [ "$(cat "$gi" 2>/dev/null)" = "$want" ]; then
    note "Layer B: runtime/.gitignore already pure-local"
  else
    if [ "$APPLY" -eq 1 ]; then printf '%s\n' "$want" > "$gi"; note "wrote pure-local runtime/.gitignore"
    else printf '  [would] write pure-local %s\n' "$gi"; fi
    CHANGED=1
  fi
}

for rt in "${RUNTIMES[@]}"; do
  printf -- '-- %s\n' "$rt"
  # Runtime layer FIRST: any final push needs the gitdir, which the project
  # layer's `rm -rf .git/modules/<rel>` would otherwise remove.
  layer_b "$rt"
  layer_a "$rt"
done

echo
if [ "$CHANGED" -eq 0 ]; then
  printf 'RESULT: already pure-local — nothing to do.\n'
elif [ "$APPLY" -eq 1 ]; then
  printf 'RESULT: applied. Commit the project-repo changes (.gitignore / .gitmodules / unstaged runtime) when ready.\n'
  printf '        Fleet-level cleanup (dead -checkpoint units, profile symlink) -> fleet-prune-debris.sh\n'
else
  printf 'RESULT: dry-run only. Re-run with --apply to mutate.\n'
fi
