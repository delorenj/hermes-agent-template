#!/usr/bin/env bash
set -euo pipefail

# unify-pm — Phase C (safe subset): move every deployed PM toward the unified
# single-PM model WITHOUT disrupting running services.
#
# Per PM (driven off registry role=pm, so specialized non-pm profiles are
# naturally skipped):
#   1. refresh .scripts/checkpoint.sh — applies the secret-scan gate to the live
#      hourly checkpoint (the existing checkpoint.service ExecStarts this file).
#   2. refresh the <role_dir>/hermes launcher to the runtime-only template
#      (drops the stale profile-fallback; used for interactive `hermes chat`,
#      never by the systemd units).
#   3. ensure ~/.hermes/profiles/<profile_name> symlinks to ./runtime. This is
#      LOAD-BEARING — hermes resolves `--profile <name>` through it (and recreates
#      it as a fresh standalone dir if missing, disconnecting the agent from its
#      runtime), so it is repaired/created, NEVER dropped.
#
# Does NOT touch gateway/consumer/checkpoint units, the registry, or runtimes.
# The checkpoint→heartbeat unit rename + sentinel install is a separate pass.
#
# DRY-RUN by default; --apply performs; --agent <id>... scopes; pjangler-pm first.
#
# Usage: unify-pm.sh [--apply] [--agent <pm-id>]...

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../template"
CHECKPOINT_SRC="$TEMPLATE_DIR/.scripts/checkpoint.sh"
WRAPPER_TEMPLATE="$TEMPLATE_DIR/hermes.jinja"

HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
cfg() {
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

REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
PROFILES_DIR="${HERMES_FLEET_HOME:-$HOME/.hermes}/profiles"
SYS_DIR="$HOME/.config/systemd/user"

APPLY=0
declare -a ONLY=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --agent) shift; ONLY+=("$1") ;;
    -h|--help) sed -n '3,30p' "$0"; exit 0 ;;
    *) echo "unify-pm: unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$REGISTRY_FILE" ]] || { echo "unify-pm: registry not found: $REGISTRY_FILE" >&2; exit 2; }
[[ -f "$CHECKPOINT_SRC" ]] || { echo "unify-pm: template checkpoint.sh not found: $CHECKPOINT_SRC" >&2; exit 2; }
[[ -f "$WRAPPER_TEMPLATE" ]] || { echo "unify-pm: wrapper template not found: $WRAPPER_TEMPLATE" >&2; exit 2; }

MODE="DRY-RUN"; [[ $APPLY -eq 1 ]] && MODE="APPLY"
echo "unify-pm — mode: $MODE   registry: $REGISTRY_FILE"
echo "================================================================"

# PM rows (pjangler first), TSV: agent_id, role_dir, profile_name
read_pm() {
  python3 - "$REGISTRY_FILE" <<'PYEOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
rows = [(aid, str(a.get("role_dir") or ""), str(a.get("profile_name") or aid))
        for aid, a in (d.get("agents") or {}).items()
        if isinstance(a, dict) and a.get("role") == "pm"]
rows.sort(key=lambda r: (r[0] != "pjangler-pm", r[0]))
for r in rows:
    print("\t".join(r))
PYEOF
}

wanted() { [[ ${#ONLY[@]} -eq 0 ]] && return 0; local a; for a in "${ONLY[@]}"; do [[ "$a" == "$1" ]] && return 0; done; return 1; }

CK=0; WR=0; SL=0; SKIP=0
while IFS=$'\t' read -r aid role_dir profile_name; do
  wanted "$aid" || continue
  echo
  echo ">>> $aid"
  if [[ -z "$role_dir" || ! -d "$role_dir" ]]; then
    echo "    role_dir missing: ${role_dir:-<unset>} — skipping"; SKIP=$((SKIP+1)); continue
  fi
  runtime="$role_dir/runtime"

  # 1. checkpoint.sh secret-gate refresh.
  dst="$role_dir/.scripts/checkpoint.sh"
  if [[ ! -f "$dst" ]] || ! diff -q "$CHECKPOINT_SRC" "$dst" >/dev/null 2>&1; then
    if [[ $APPLY -eq 1 ]]; then
      mkdir -p "$role_dir/.scripts"; cp "$CHECKPOINT_SRC" "$dst"; chmod +x "$dst"
      echo "    [do]  checkpoint.sh refreshed (secret-gate)"; CK=$((CK+1))
    else
      echo "    [dry] checkpoint.sh would be refreshed (secret-gate)"; CK=$((CK+1))
    fi
  else
    echo "    checkpoint.sh already current"
  fi

  # 2. launcher → runtime-only template.
  expected_wrapper="$(sed "s/{{ agent_id }}/$aid/g" "$WRAPPER_TEMPLATE")"
  if [[ ! -f "$role_dir/hermes" ]] || ! diff -q <(printf '%s\n' "$expected_wrapper") "$role_dir/hermes" >/dev/null 2>&1; then
    if [[ $APPLY -eq 1 ]]; then
      printf '%s\n' "$expected_wrapper" > "$role_dir/hermes"; chmod +x "$role_dir/hermes"
      echo "    [do]  launcher regenerated (runtime-only)"; WR=$((WR+1))
    else
      echo "    [dry] launcher would be regenerated (runtime-only)"; WR=$((WR+1))
    fi
  else
    echo "    launcher already current"
  fi

  # 3. ensure the profile symlink points at the runtime (load-bearing; never drop).
  link="$PROFILES_DIR/$profile_name"
  if [[ -L "$link" && "$(readlink -f "$link")" == "$(readlink -f "$runtime")" ]]; then
    echo "    profile symlink ok -> runtime"
  elif [[ -e "$link" && ! -L "$link" ]]; then
    echo "    profile entry is a REAL dir (MANUAL merge): $link — left untouched"; SKIP=$((SKIP+1))
  else
    if [[ $APPLY -eq 1 ]]; then
      mkdir -p "$PROFILES_DIR"; ln -sfn "$runtime" "$link"; echo "    [do]  profile symlink set -> runtime"; SL=$((SL+1))
    else
      echo "    [dry] profile symlink would be set -> runtime"; SL=$((SL+1))
    fi
  fi
done < <(read_pm)

echo
echo "================================================================"
echo "unify-pm: checkpoint=$CK launcher=$WR symlink-repair=$SL skipped=$SKIP  ($MODE)"