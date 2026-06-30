#!/usr/bin/env bash
set -euo pipefail

# fleet-sync — reconcile every deployed Hermes agent with the current
# template and fleet contract. The registry (~/.hermes/agents-registry.yaml)
# is the iteration source; the vendored template is the content source.
#
# Per agent:
#   wrapper   <role_dir>/hermes regenerated from template/hermes.jinja
#   contract  runtime/profile.yaml opts into config.inherit_from: default
#   registry  ~/.hermes/profiles/<profile_name> -> <role_dir>/runtime
#   role      <role_dir>/role.yaml profile: <profile_name>
#   services  systemd gateway/consumer units restarted when something changed
#
# Default is a DRY-RUN drift report (exit 1 when drift exists, 0 when clean).
# --apply writes the fixes and restarts the changed agents' services.
# Anything that would require destroying or merging existing data (a real
# directory where a symlink belongs, a profile.yaml with foreign content)
# is reported as MANUAL and never touched.
#
# Usage: fleet-sync.sh [--apply] [--no-restart] [--agent <id>]...

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRAPPER_TEMPLATE="$SCRIPT_DIR/../template/hermes.jinja"

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
if [[ -f "$FLEET_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$FLEET_ENV"
fi
REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
PROFILES_DIR="${HERMES_FLEET_HOME:-$HOME/.hermes}/profiles"
VOXXY_PLUGIN_DIR="${VOXXY_PLUGIN_DIR:-$(cfg fleet.voxxy_plugin_dir "$HOME/code/voxxy/plugins/tts/voxxy")}"
VOX_URL_VALUE="${VOX_URL:-$(cfg fleet.vox_url 'https://vox.delo.sh')}"

APPLY=0
RESTART=1
declare -a ONLY_AGENTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --no-restart) RESTART=0 ;;
    --agent) shift; ONLY_AGENTS+=("$1") ;;
    -h|--help) sed -n '3,21p' "$0"; exit 0 ;;
    *) echo "fleet-sync: unknown flag $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$REGISTRY_FILE" ]] || { echo "fleet-sync: registry not found: $REGISTRY_FILE" >&2; exit 2; }
[[ -f "$WRAPPER_TEMPLATE" ]] || { echo "fleet-sync: wrapper template not found: $WRAPPER_TEMPLATE" >&2; exit 2; }

# Registry -> TSV: agent_id, role_dir, profile_name, gateway_unit, consumer_unit
read_registry() {
  python3 - "$REGISTRY_FILE" <<'PYEOF'
import sys

try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
except ModuleNotFoundError:
    sys.stderr.write("fleet-sync: python3-yaml is required to read the registry\n")
    sys.exit(2)

for agent_id, a in sorted((data.get("agents") or {}).items()):
    if not isinstance(a, dict):
        continue
    systemd = a.get("systemd") or {}
    print("\t".join([
        agent_id,
        str(a.get("role_dir") or ""),
        str(a.get("profile_name") or agent_id),
        str(systemd.get("gateway_unit") or ""),
        str(systemd.get("consumer_unit") or ""),
    ]))
PYEOF
}

render_wrapper() {  # render_wrapper <agent_id>
  sed "s/{{ agent_id }}/$1/g" "$WRAPPER_TEMPLATE"
}

DRIFT=0
FIXED=0
declare -a RESTART_UNITS=()

# Registry agents sharing a role_dir would overwrite each other's wrapper
# and role.yaml on --apply; refuse to write into contested dirs.
CONTESTED_DIRS="$(read_registry | cut -f2 | sort | uniq -d)"

note() {  # note <agent> <status> <message>
  printf '%-34s %-7s %s\n' "$1" "$2" "$3"
}

ensure_env_key() {  # ensure_env_key <path> <key> <value>
  python3 - "$1" "$2" "$3" <<'PYEOF'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
needle = f"{key}="
for idx, line in enumerate(lines):
    if line.startswith(needle):
        lines[idx] = f'{key}="{value}"'
        break
else:
    lines.append(f'{key}="{value}"')
path.write_text("\n".join(lines) + "\n")
PYEOF
}

wanted_agent() {
  [[ ${#ONLY_AGENTS[@]} -eq 0 ]] && return 0
  local a
  for a in "${ONLY_AGENTS[@]}"; do [[ "$a" == "$1" ]] && return 0; done
  return 1
}

while IFS=$'\t' read -r agent_id role_dir profile_name gateway_unit consumer_unit; do
  wanted_agent "$agent_id" || continue
  if [[ -z "$role_dir" || ! -d "$role_dir" ]]; then
    note "$agent_id" DRIFT "role_dir missing: ${role_dir:-<unset>} (MANUAL: deprovision or fix registry)"
    DRIFT=$((DRIFT + 1))
    continue
  fi
  if [[ -n "$CONTESTED_DIRS" ]] && grep -qx "$role_dir" <<<"$CONTESTED_DIRS"; then
    note "$agent_id" DRIFT "role_dir claimed by multiple registry agents: $role_dir (MANUAL: fix registry)"
    DRIFT=$((DRIFT + 1))
    continue
  fi
  runtime="$role_dir/runtime"
  changed=0

  # 1. Launcher wrapper regenerated from the current template.
  expected_wrapper="$(render_wrapper "$agent_id")"
  if [[ ! -f "$role_dir/hermes" ]] || ! diff -q <(printf '%s\n' "$expected_wrapper") "$role_dir/hermes" >/dev/null 2>&1; then
    if [[ $APPLY -eq 1 ]]; then
      printf '%s\n' "$expected_wrapper" > "$role_dir/hermes"
      chmod +x "$role_dir/hermes"
      note "$agent_id" FIXED "wrapper regenerated from template"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "wrapper differs from template"
      DRIFT=$((DRIFT + 1))
    fi
  fi

  # 2. Inherited-config contract in runtime/profile.yaml.
  profile_yaml="$runtime/profile.yaml"
  if [[ ! -d "$runtime" ]]; then
    note "$agent_id" DRIFT "runtime dir missing: $runtime (MANUAL: re-provision)"
    DRIFT=$((DRIFT + 1))
  elif [[ ! -f "$profile_yaml" ]]; then
    if [[ $APPLY -eq 1 ]]; then
      printf 'config:\n  inherit_from: default\n  save_mode: delta\n' > "$profile_yaml"
      note "$agent_id" FIXED "profile.yaml written (inherit_from: default)"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "profile.yaml missing (no inherited config)"
      DRIFT=$((DRIFT + 1))
    fi
  elif ! grep -q 'inherit_from: default' "$profile_yaml"; then
    note "$agent_id" DRIFT "profile.yaml lacks inherit_from: default (MANUAL: merge contract)"
    DRIFT=$((DRIFT + 1))
  fi

  # 3. Registry symlink ~/.hermes/profiles/<profile_name> -> runtime.
  link="$PROFILES_DIR/$profile_name"
  if [[ -L "$link" ]]; then
    if [[ "$(readlink -f "$link")" != "$(readlink -f "$runtime")" ]]; then
      if [[ $APPLY -eq 1 ]]; then
        ln -sfn "$runtime" "$link"
        note "$agent_id" FIXED "profile symlink repointed -> $runtime"
        changed=1; FIXED=$((FIXED + 1))
      else
        note "$agent_id" DRIFT "profile symlink points at $(readlink "$link")"
        DRIFT=$((DRIFT + 1))
      fi
    fi
  elif [[ -e "$link" ]]; then
    note "$agent_id" DRIFT "$link is a real directory, expected symlink (MANUAL: merge state)"
    DRIFT=$((DRIFT + 1))
  else
    if [[ $APPLY -eq 1 ]]; then
      mkdir -p "$PROFILES_DIR"
      ln -sfn "$runtime" "$link"
      note "$agent_id" FIXED "profile symlink created -> $runtime"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "profile symlink missing"
      DRIFT=$((DRIFT + 1))
    fi
  fi

  # 4. role.yaml binds the registry profile name.
  role_yaml="$role_dir/role.yaml"
  if [[ -f "$role_yaml" ]] && ! grep -q "^profile: $profile_name$" "$role_yaml"; then
    if [[ $APPLY -eq 1 ]] && grep -q '^profile: ' "$role_yaml"; then
      sed -i "s/^profile: .*/profile: $profile_name/" "$role_yaml"
      note "$agent_id" FIXED "role.yaml profile -> $profile_name"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "role.yaml profile != $profile_name"
      DRIFT=$((DRIFT + 1))
    fi
  fi

  # 5. PM fleet voice contract: runtime plugin symlink + Voxxy config + VOX_URL.
  role_name="$(basename "$role_dir")"
  if [[ "$role_name" == "pm" && -d "$runtime" ]]; then
    plugin_link="$runtime/plugins/tts/voxxy"
    if [[ -d "$VOXXY_PLUGIN_DIR" ]]; then
      if [[ -L "$plugin_link" ]]; then
        if [[ "$(readlink -f "$plugin_link")" != "$(readlink -f "$VOXXY_PLUGIN_DIR")" ]]; then
          if [[ $APPLY -eq 1 ]]; then
            mkdir -p "$runtime/plugins/tts"
            ln -sfn "$VOXXY_PLUGIN_DIR" "$plugin_link"
            note "$agent_id" FIXED "runtime Voxxy plugin relinked"
            changed=1; FIXED=$((FIXED + 1))
          else
            note "$agent_id" DRIFT "runtime Voxxy plugin points at $(readlink "$plugin_link")"
            DRIFT=$((DRIFT + 1))
          fi
        fi
      elif [[ -e "$plugin_link" ]]; then
        note "$agent_id" DRIFT "$plugin_link exists and is not a symlink (MANUAL: merge runtime plugin state)"
        DRIFT=$((DRIFT + 1))
      else
        if [[ $APPLY -eq 1 ]]; then
          mkdir -p "$runtime/plugins/tts"
          ln -sfn "$VOXXY_PLUGIN_DIR" "$plugin_link"
          note "$agent_id" FIXED "runtime Voxxy plugin linked"
          changed=1; FIXED=$((FIXED + 1))
        else
          note "$agent_id" DRIFT "runtime Voxxy plugin missing"
          DRIFT=$((DRIFT + 1))
        fi
      fi
    else
      note "$agent_id" DRIFT "configured Voxxy plugin dir missing: $VOXXY_PLUGIN_DIR (MANUAL: install voxxy repo)"
      DRIFT=$((DRIFT + 1))
    fi

    runtime_env="$runtime/.env"
    if [[ -n "$VOX_URL_VALUE" ]]; then
      vox_env_status="$(python3 - "$runtime_env" "$VOX_URL_VALUE" <<'PYEOF'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
if not path.exists():
    print('missing-file')
    raise SystemExit(0)
for line in path.read_text().splitlines():
    if line.startswith('VOX_URL='):
        current = line.split('=', 1)[1].strip().strip('"').strip("'")
        print('ok' if current == expected else 'mismatch')
        raise SystemExit(0)
print('missing-key')
PYEOF
)"
      case "$vox_env_status" in
        ok) ;;
        *)
          if [[ $APPLY -eq 1 ]]; then
            ensure_env_key "$runtime_env" VOX_URL "$VOX_URL_VALUE"
            chmod 600 "$runtime_env" 2>/dev/null || true
            note "$agent_id" FIXED "runtime .env VOX_URL -> $VOX_URL_VALUE"
            changed=1; FIXED=$((FIXED + 1))
          else
            note "$agent_id" DRIFT "runtime .env missing/incorrect VOX_URL"
            DRIFT=$((DRIFT + 1))
          fi
          ;;
      esac
    fi

    config_status="$(python3 - "$runtime/config.yaml" <<'PYEOF'
import pathlib, sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    print('missing')
    raise SystemExit(0)
text = path.read_text()
provider = 'voxxy' if 'provider: voxxy' in text else 'other'
plugin = 'yes' if 'tts/voxxy' in text else 'no'
voice = 'rick' if 'voice: rick' in text else 'other'
print(f'{provider}|{plugin}|{voice}')
PYEOF
)"
    if [[ "$APPLY" -eq 1 ]]; then
      HERMES_HOME="$runtime" "${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.hermes/hermes-agent/.venv/bin/hermes")}" config set plugins.enabled.0 tts/voxxy >/dev/null 2>&1 || true
      HERMES_HOME="$runtime" "${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.hermes/hermes-agent/.venv/bin/hermes")}" config set tts.provider voxxy >/dev/null 2>&1 || true
      HERMES_HOME="$runtime" "${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.hermes/hermes-agent/.venv/bin/hermes")}" config set tts.voice rick >/dev/null 2>&1 || true
      note "$agent_id" FIXED "runtime TTS config enforced -> voxxy/rick"
      changed=1; FIXED=$((FIXED + 1))
    elif [[ "$config_status" != "voxxy|yes|rick" ]]; then
      note "$agent_id" DRIFT "runtime Voxxy config missing from config.yaml"
      DRIFT=$((DRIFT + 1))
    fi
  fi

  if [[ $changed -eq 1 && $RESTART -eq 1 ]]; then
    [[ -n "$gateway_unit" ]] && RESTART_UNITS+=("$gateway_unit")
    [[ -n "$consumer_unit" ]] && RESTART_UNITS+=("$consumer_unit")
  fi
done < <(read_registry)

# 5. Stale registry symlinks: entries pointing into fleet runtimes under a
# name the registry does not know. Reported only — removal is a human call.
known_names="$(read_registry | cut -f3 | sort -u)"
if [[ -d "$PROFILES_DIR" ]]; then
  for link in "$PROFILES_DIR"/*; do
    [[ -L "$link" ]] || continue
    name="$(basename "$link")"
    grep -qx "$name" <<<"$known_names" && continue
    target="$(readlink "$link")"
    if [[ "$target" == */agents/hermes/* ]]; then
      note "$name" STALE "registry symlink not in agents-registry -> $target (MANUAL: remove or register)"
      DRIFT=$((DRIFT + 1))
    fi
  done
fi

if [[ ${#RESTART_UNITS[@]} -gt 0 ]]; then
  echo
  echo "Restarting: ${RESTART_UNITS[*]}"
  systemctl --user try-restart "${RESTART_UNITS[@]}" || true
fi

echo
if [[ $APPLY -eq 1 ]]; then
  echo "fleet-sync: $FIXED fixed, $DRIFT unresolved (manual)"
else
  echo "fleet-sync: $DRIFT drift item(s). Re-run with --apply to fix."
fi
[[ $DRIFT -eq 0 ]] || exit 1
