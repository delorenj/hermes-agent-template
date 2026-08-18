#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_ENV_LIBRARY_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/fleet-env.sh"
FLEET_ENV_PARSER_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/parse-fleet-env.py"
ROLE_LIBRARY_SOURCE="$SCRIPT_DIR/../template/.scripts/_lib.sh"
HEARTBEAT_SOURCE="$SCRIPT_DIR/../template/.scripts/heartbeat.sh"
WRAPPER_TEMPLATE="$SCRIPT_DIR/../template/hermes.jinja"
BACKFILL_DRIVER_SOURCE="$SCRIPT_DIR/backfill-fleet-sot.py"
for trusted_asset in \
  "$FLEET_ENV_LIBRARY_SOURCE" \
  "$FLEET_ENV_PARSER_SOURCE" \
  "$ROLE_LIBRARY_SOURCE" \
  "$HEARTBEAT_SOURCE" \
  "$WRAPPER_TEMPLATE" \
  "$BACKFILL_DRIVER_SOURCE"
do
  if [[ ! -f "$trusted_asset" || -L "$trusted_asset" ]]; then
    echo "backfill-fleet-sot: trusted template asset is unavailable" >&2
    exit 2
  fi
done
# shellcheck source=../template/.scripts/lib/fleet-env.sh
builtin source "$FLEET_ENV_LIBRARY_SOURCE"
scrub_subprocess_interpreter_injection

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      builtin printf '%s\n' \
        'Usage: backfill-fleet-sot.sh [--dry-run]' \
        'Preflight every fleet, registry, role, launcher, scaffold, and unit operation before applying.'
      exit 0
      ;;
    *)
      builtin printf 'backfill-fleet-sot: unknown flag %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

# Distributable config — same source of truth the template's _lib.sh reads.
HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
cfg() {  # cfg <dotted.key> <default>  — read a value from config.toml (always exits 0)
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
load_fleet_environment "$FLEET_ENV" "$FLEET_ENV_PARSER_SOURCE"

REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
HERMES_FLEET_BIN="${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes")}"
HERMES_FLEET_REPO="${HERMES_FLEET_REPO:-$(cfg fleet.hermes_repo "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1")}"
HERMES_FLEET_OAUTH_FILE="${HERMES_FLEET_OAUTH_FILE:-$(cfg fleet.oauth_file "$HOME/.hermes/auth.json")}"
HERMES_FLEET_CODEX_HOME="${HERMES_FLEET_CODEX_HOME:-$(cfg fleet.codex_home "$HOME/.codex")}"
SCAFFOLD_SRC="$(cd "$(dirname "$0")/../template/.runtime-scaffold" && pwd)"
SYSTEMD_USER_DIR="${HERMES_FLEET_SYSTEMD_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
PYTHON_BIN="$(builtin type -P python3)" || {
  builtin printf 'backfill-fleet-sot: python3 is unavailable\n' >&2
  exit 2
}

driver_args=(
  --registry "$REGISTRY_FILE"
  --fleet-env "$FLEET_ENV"
  --fleet-bin "$HERMES_FLEET_BIN"
  --fleet-repo "$HERMES_FLEET_REPO"
  --oauth-file "$HERMES_FLEET_OAUTH_FILE"
  --codex-home "$HERMES_FLEET_CODEX_HOME"
  --systemd-dir "$SYSTEMD_USER_DIR"
  --scaffold-source "$SCAFFOLD_SRC"
  --fleet-library-source "$FLEET_ENV_LIBRARY_SOURCE"
  --fleet-parser-source "$FLEET_ENV_PARSER_SOURCE"
  --role-library-source "$ROLE_LIBRARY_SOURCE"
  --heartbeat-source "$HEARTBEAT_SOURCE"
  --wrapper-template "$WRAPPER_TEMPLATE"
)
if [[ "$DRY_RUN" == "1" ]]; then
  driver_args+=(--dry-run)
fi
"$PYTHON_BIN" -I "$BACKFILL_DRIVER_SOURCE" "${driver_args[@]}"
