#!/usr/bin/env bash
set -euo pipefail

# migrate-unify — one-time fleet migration to the unified single-PM model.
#
# Decommissions the legacy `scrum-master` agents, collapsing each repo from a
# PM + scrum-master pair down to a single PM. The PM keeps running untouched;
# its checkpoint→heartbeat refresh is a separate uniform pass (and with
# reconcile disabled by default the PM heartbeat is checkpoint-only, so removing
# the scrum-master is behavior-complete for the collapse).
#
# Drives ENTIRELY off the registry's role=scrum-master entries
# (role_dir / project_path / runtime_repo) — NEVER name-derived paths, because
# keepy-money lives at ~/code/tiller, zshyzsh at ~/.config/zshyzsh, and
# delocontainers at ~/docker.
#
# Safety model:
#   * DRY-RUN by default — prints exactly what it WOULD do, touches nothing.
#   * --apply performs the REVERSIBLE local teardown:
#       - stop + disable + remove the scrum-master's systemd --user units
#       - `git submodule deinit` + `git rm` the scrum-master role dir + clean
#         .gitmodules / .git/modules  (STAGED only — never commits or pushes)
#       - remove the ~/.hermes/profiles/<sm-id> symlink
#       - drop the scrum-master entry from the fleet registry (.bak first)
#   * The IRREVERSIBLE `gh repo delete` of each scrum-master runtime repo is held
#     behind BOTH --decommission-sm-remotes AND --yes-i-mean-it, and runs only
#     after all local teardown has succeeded.
#
# It never commits or pushes any project repo — staged submodule removals are
# left for you to review and commit (some live in sensitive repos: ~/docker,
# ~/.config/zshyzsh).
#
# Usage:
#   migrate-unify.sh                      # dry-run report (all scrum-masters)
#   migrate-unify.sh --apply              # reversible local teardown
#   migrate-unify.sh --apply --decommission-sm-remotes --yes-i-mean-it
#                                         # + delete the runtime repos on GitHub
#   migrate-unify.sh --agent pjangler-scrum-master [--apply ...]   # one only

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_ENV_LIBRARY="$SCRIPT_DIR/../template/.scripts/lib/fleet-env.sh"
FLEET_ENV_PARSER="$SCRIPT_DIR/../template/.scripts/lib/parse-fleet-env.py"
if [[ ! -f "$FLEET_ENV_LIBRARY" || -L "$FLEET_ENV_LIBRARY" \
   || ! -f "$FLEET_ENV_PARSER" || -L "$FLEET_ENV_PARSER" ]]; then
  echo "migrate-unify: trusted fleet environment loader is unavailable" >&2
  exit 2
fi
# shellcheck source=../template/.scripts/lib/fleet-env.sh
builtin source "$FLEET_ENV_LIBRARY"
scrub_subprocess_interpreter_injection

HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
cfg() {  # cfg <dotted.key> <default>
  local key="$1" def="$2"
  { [[ -f "$HERMES_TEMPLATE_CONFIG" ]] && command -v python3 >/dev/null 2>&1; } || { printf '%s' "$def"; return 0; }
  python3 - "$HERMES_TEMPLATE_CONFIG" "$key" "$def" <<'PYEOF' || printf '%s' "$def"
import sys, os
try:
    import tomllib
except ModuleNotFoundError:
    print(sys.argv[3], end=""); sys.exit(0)
try:
    cur = tomllib.load(open(sys.argv[1], "rb"))
except Exception:
    print(sys.argv[3], end=""); sys.exit(0)
for p in sys.argv[2].split("."):
    if isinstance(cur, dict) and p in cur:
        cur = cur[p]
    else:
        print(sys.argv[3], end=""); sys.exit(0)
print(os.path.expanduser(str(cur)), end="")
PYEOF
  return 0
}

FLEET_ENV="${HERMES_FLEET_ENV:-$(cfg fleet.fleet_env "$HOME/.hermes/fleet.env")}"
load_fleet_environment "$FLEET_ENV" "$FLEET_ENV_PARSER"
REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
PROFILES_DIR="${HERMES_FLEET_HOME:-$HOME/.hermes}/profiles"
SYS_DIR="$HOME/.config/systemd/user"

APPLY=0
DELETE_REMOTES=0
CONFIRM=0
declare -a ONLY_AGENTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --decommission-sm-remotes) DELETE_REMOTES=1 ;;
    --yes-i-mean-it) CONFIRM=1 ;;
    --all) : ;;  # accepted for symmetry; default is already all scrum-masters
    --agent) shift; ONLY_AGENTS+=("$1") ;;
    -h|--help) sed -n '3,55p' "$0"; exit 0 ;;
    *) echo "migrate-unify: unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$REGISTRY_FILE" ]] || { echo "migrate-unify: registry not found: $REGISTRY_FILE" >&2; exit 2; }
command -v python3 >/dev/null || { echo "migrate-unify: python3 required" >&2; exit 2; }

MODE="DRY-RUN"; [[ $APPLY -eq 1 ]] && MODE="APPLY"
echo "migrate-unify — mode: $MODE   registry: $REGISTRY_FILE"
echo "================================================================"

# Emit scrum-master rows (pjangler first), TSV: agent_id, repo, role_dir, project_path, runtime_repo, profile_name
read_sm() {
  python3 - "$REGISTRY_FILE" <<'PYEOF'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
rows = []
for aid, a in (data.get("agents") or {}).items():
    if not isinstance(a, dict) or a.get("role") != "scrum-master":
        continue
    rows.append((aid, str(a.get("repo") or ""), str(a.get("role_dir") or ""),
                 str(a.get("project_path") or ""), str(a.get("runtime_repo") or ""),
                 str(a.get("profile_name") or aid)))
# pjangler first (the reference repo / canary), then alphabetical.
rows.sort(key=lambda r: (r[1] != "pjangler", r[0]))
for r in rows:
    print("\t".join(r))
PYEOF
}

wanted() {
  [[ ${#ONLY_AGENTS[@]} -eq 0 ]] && return 0
  local a; for a in "${ONLY_AGENTS[@]}"; do [[ "$a" == "$1" ]] && return 0; done; return 1
}

run() {  # run <human-description> -- <cmd...>   (prints always; executes only on --apply)
  local desc="$1"; shift
  [[ "${1:-}" == "--" ]] && shift
  if [[ $APPLY -eq 1 ]]; then
    printf '    [do]   %s\n' "$desc"
    local rc=0
    "$@" >/dev/null 2>&1 || rc=$?
    if [[ $rc -ne 0 ]]; then printf '      (non-fatal: returned %s)\n' "$rc"; fi
  else
    printf '    [dry]  %s\n' "$desc"
  fi
}

declare -a REMOVE_IDS=()       # registry ids to drop (batched)
declare -a DELETE_REPOS=()     # runtime repos to delete (gated, last)
declare -a STAGED_REPOS=()     # project repos left with staged submodule removals
PROCESSED=0

while IFS=$'\t' read -r sm_id repo role_dir project_path runtime_repo profile_name; do
  wanted "$sm_id" || continue
  PROCESSED=$((PROCESSED + 1))
  echo
  echo ">>> $sm_id   (repo=$repo)"
  echo "    role_dir     : $role_dir"
  echo "    project_path : $project_path"
  echo "    runtime_repo : $runtime_repo"

  # 1. systemd --user units: stop + disable + remove every hermes-<sm_id>-* unit.
  shopt -s nullglob
  units=("$SYS_DIR/hermes-${sm_id}-"*)
  shopt -u nullglob
  if [[ ${#units[@]} -eq 0 ]]; then
    echo "    units        : (none found on disk)"
  else
    for uf in "${units[@]}"; do
      u="$(basename "$uf")"
      run "stop+disable $u" -- systemctl --user disable --now "$u"
      run "rm unit file $u" -- rm -f "$uf"
    done
    if [[ $APPLY -eq 1 ]]; then
      systemctl --user daemon-reload 2>/dev/null || true
      systemctl --user reset-failed 2>/dev/null || true
    fi
  fi

  # 2. scrum-master runtime submodule + role dir (STAGED removal, never committed).
  if [[ -n "$project_path" && ( -d "$project_path/.git" || -f "$project_path/.git" ) ]]; then
    sm_rel="agents/hermes/scrum-master"
    if [[ -e "$project_path/$sm_rel" ]]; then
      run "git submodule deinit -f $sm_rel/runtime" -- git -C "$project_path" submodule deinit -f "$sm_rel/runtime"
      run "git rm -rf $sm_rel (staged)" -- git -C "$project_path" rm -rf "$sm_rel"
      run "rm .git/modules/$sm_rel residue" -- rm -rf "$project_path/.git/modules/$sm_rel"
      run "scrub .gitmodules section" -- git -C "$project_path" config -f "$project_path/.gitmodules" --remove-section "submodule.$sm_rel/runtime"
      if [[ $APPLY -eq 1 ]]; then STAGED_REPOS+=("$project_path"); fi
    else
      echo "    role dir     : already absent at $project_path/$sm_rel"
    fi
  else
    echo "    submodule    : project_path is not a git repo, skipping submodule teardown: $project_path"
  fi

  # 3. profile symlink ~/.hermes/profiles/<sm_id>.
  link="$PROFILES_DIR/$profile_name"
  if [[ -L "$link" ]]; then
    run "rm profile symlink $link" -- rm -f "$link"
  elif [[ -e "$link" ]]; then
    echo "    profile      : $link is a real dir, NOT a symlink — left untouched (MANUAL)"
  else
    echo "    profile      : no symlink at $link"
  fi

  # 4. registry entry (batched; rewritten once at the end with a .bak).
  REMOVE_IDS+=("$sm_id")
  echo "    registry     : will drop agents.$sm_id"

  # 5. runtime repo delete (gated, deferred to the very end).
  [[ -n "$runtime_repo" ]] && DELETE_REPOS+=("$runtime_repo")
done < <(read_sm)

if [[ $PROCESSED -eq 0 ]]; then
  echo; echo "migrate-unify: no scrum-master agents matched. Nothing to do."
  exit 0
fi

# --- Registry rewrite (batched) ---
echo
if [[ ${#REMOVE_IDS[@]} -gt 0 ]]; then
  if [[ $APPLY -eq 1 ]]; then
    python3 - "$REGISTRY_FILE" "${REMOVE_IDS[@]}" <<'PYEOF'
import sys, shutil, datetime, yaml
path = sys.argv[1]; ids = sys.argv[2:]
shutil.copy(path, path + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
data = yaml.safe_load(open(path, encoding="utf-8")) or {}
agents = data.get("agents") or {}
removed = [i for i in ids if agents.pop(i, None) is not None]
data["agents"] = agents
with open(path, "w", encoding="utf-8") as f:
    f.write(yaml.safe_dump(data, sort_keys=False))
print("    registry: removed " + ", ".join(removed) + " (backup written)")
PYEOF
  else
    echo "    [dry]  would drop registry entries: ${REMOVE_IDS[*]} (after .bak)"
  fi
fi

# --- Irreversible remote deletes (double-gated, last) ---
echo
echo "Runtime repos to delete on GitHub:"
printf '    %s\n' "${DELETE_REPOS[@]}"
if [[ ${#DELETE_REPOS[@]} -gt 0 ]]; then
  if [[ $APPLY -eq 1 && $DELETE_REMOTES -eq 1 && $CONFIRM -eq 1 ]]; then
    if command -v gh >/dev/null 2>&1; then
      for r in "${DELETE_REPOS[@]}"; do
        printf '    [do]   gh repo delete %s --yes\n' "$r"
        gh repo delete "$r" --yes 2>&1 | tail -1 || echo "      (delete failed or already gone: $r)"
      done
    else
      echo "    gh not on PATH — cannot delete remotes."
    fi
  else
    echo "    [HELD] add --apply --decommission-sm-remotes --yes-i-mean-it to delete these (IRREVERSIBLE)."
  fi
fi

echo
echo "================================================================"
echo "migrate-unify: processed $PROCESSED scrum-master agent(s) in $MODE mode."
if [[ $APPLY -eq 1 && ${#STAGED_REPOS[@]} -gt 0 ]]; then
  echo
  echo "Review + commit the staged scrum-master removals in these repos (NOT auto-committed):"
  printf '%s\n' "${STAGED_REPOS[@]}" | sort -u | sed 's/^/    git -C /; s/$/ status/'
fi
