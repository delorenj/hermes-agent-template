#!/usr/bin/env bash
set -euo pipefail

# fleet-sync — reconcile every deployed Hermes agent with the current
# template and fleet contract. The registry (~/.hermes/agents-registry.yaml)
# is the iteration source; the vendored template is the content source.
#
# Per agent:
#   wrapper   <role_dir>/hermes regenerated from template/hermes.jinja
#   contract  runtime/profile.yaml opts into config.inherit_from: default
#   profile   ~/.hermes/profiles/<profile_name> is a REAL dir whose shared
#             entries link to the fleet root and owned entries link to runtime
#   units     systemd HERMES_HOME points at that profile dir
#   role      <role_dir>/role.yaml profile: <profile_name>
#   services  stale per-profile Bloodbank consumers retired; gateways restarted
#
# SINGLETON-RUNTIME CONTRACT (supersedes the old profile-symlink model):
# ~/.hermes/profiles/<name> MUST be a real directory, NOT a symlink to the
# runtime. Hermes derives profile identity from the UNRESOLVED HERMES_HOME
# path, so a symlinked profile dir makes get_active_profile_name() report
# "default" and disables shared fleet auth. Shared entries (.env, skills)
# symlink up to the fleet root; config.yaml is a generated real file rendered
# from the fleet base plus config.delta.yaml; owned entries (memories, sessions,
# state.db, ...) symlink back into <role_dir>/runtime.
#
# Default is a DRY-RUN drift report (exit 1 when drift exists, 0 when clean).
# --apply writes the fixes and restarts the changed agents' services.
# Anything that would require destroying or merging existing data (real data
# where a symlink belongs, a profile.yaml with foreign content) is reported as
# MANUAL and never touched.
#
# Usage: fleet-sync.sh [--apply] [--no-restart] [--agent <id>]...

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRAPPER_TEMPLATE="$SCRIPT_DIR/../template/hermes.jinja"
HEARTBEAT_TEMPLATE="$SCRIPT_DIR/../template/.scripts/heartbeat.sh"
FLEET_ENV_LIBRARY_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/fleet-env.sh"
FLEET_ENV_PARSER_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/parse-fleet-env.py"
PROFILE_CONFIG_TOOL="$SCRIPT_DIR/hermes-profile-config.py"
if [[ ! -f "$FLEET_ENV_LIBRARY_SOURCE" || -L "$FLEET_ENV_LIBRARY_SOURCE" \
   || ! -f "$FLEET_ENV_PARSER_SOURCE" || -L "$FLEET_ENV_PARSER_SOURCE" \
   || ! -f "$HEARTBEAT_TEMPLATE" || -L "$HEARTBEAT_TEMPLATE" \
   || ! -f "$PROFILE_CONFIG_TOOL" || -L "$PROFILE_CONFIG_TOOL" ]]; then
  echo "fleet-sync: trusted fleet environment loader is unavailable" >&2
  exit 2
fi
# shellcheck source=../template/.scripts/lib/fleet-env.sh
builtin source "$FLEET_ENV_LIBRARY_SOURCE"
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
load_fleet_environment "$FLEET_ENV" "$FLEET_ENV_PARSER_SOURCE"
REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
FLEET_HOME="${HERMES_FLEET_HOME:-$HOME/.hermes}"
PROFILES_DIR="$FLEET_HOME/profiles"
SYSTEMD_USER_DIR="${HERMES_FLEET_SYSTEMD_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
VOX_URL_VALUE="${VOX_URL:-$(cfg fleet.vox_url 'https://vox.delo.sh')}"

# Vox TTS is an OPTIONAL capability. The checkout may still be named voxxy,
# but every runtime/provider/voice claim uses the canonical vox/carlin names.
# Resolve the name and dir from explicit config first, then from known layouts
# in the voxxy checkout. When voxxy is not installed at all, that is a missing
# optional dependency reported ONCE — not per-agent drift, which previously
# buried every real finding under 20+ lines of noise.
VOX_PLUGIN_NAME="${VOX_PLUGIN_NAME:-$(cfg fleet.vox_plugin_name vox)}"
VOX_VOICE="${VOX_VOICE:-$(cfg fleet.vox_voice carlin)}"
VOX_PLUGIN_DIR="${VOX_PLUGIN_DIR:-$(cfg fleet.vox_plugin_dir "")}"
if [[ -z "$VOX_PLUGIN_DIR" || ! -d "$VOX_PLUGIN_DIR" ]]; then
  for candidate in \
    "$FLEET_HOME/plugins/$VOX_PLUGIN_NAME" \
    "$HOME/code/voxxy/plugins/tts/$VOX_PLUGIN_NAME"
  do
    [[ -n "$candidate" && -d "$candidate" ]] && { VOX_PLUGIN_DIR="$candidate"; break; }
  done
fi
VOX_AVAILABLE=0
[[ -n "$VOX_PLUGIN_DIR" && -d "$VOX_PLUGIN_DIR" ]] && VOX_AVAILABLE=1

# Singleton-runtime profile contract (mirrors src/parity/rules.ts).
SHARED_PROFILE_ENTRIES=(.env skills)
OWNED_PROFILE_ENTRIES=(memories sessions workspace logs cron plans hooks pairing audio_cache image_cache)
OWNED_PROFILE_FILES=(SOUL.md state.db kanban.db)

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

# Registry -> TSV: agent_id, role_dir, profile_name, gateway_unit, legacy_consumer_unit
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
    print("\x1f".join([
        agent_id,
        str(a.get("role_dir") or ""),
        str(a.get("profile_name") or agent_id),
        str(systemd.get("gateway_unit") or ""),
        str(systemd.get("consumer_unit") or ""),
    ]))
PYEOF
}

render_wrapper() {  # render_wrapper <agent_id> <profile_name>
  # The template defaults PROFILE_NAME to the agent id (copier has no
  # profile_name variable); the registry is authoritative when they differ.
  sed "s/{{ agent_id }}/$1/g" "$WRAPPER_TEMPLATE" \
    | sed "s|^PROFILE_NAME=.*|PROFILE_NAME=\"\${HERMES_PROFILE_NAME:-$2}\"|"
}

DRIFT=0
FIXED=0
UNITS_TOUCHED=0
declare -a RESTART_UNITS=()

# Registry agents sharing a role_dir would overwrite each other's wrapper
# and role.yaml on --apply; refuse to write into contested dirs.
CONTESTED_DIRS="$(read_registry | cut -d $'\x1f' -f2 | sort | uniq -d)"

note() {  # note <agent> <status> <message>
  printf '%-34s %-7s %s\n' "$1" "$2" "$3"
}

systemctl_user_unit_state() {  # systemctl_user_unit_state <is-active|is-enabled> <unit>
  local query="$1" unit="$2" output rc first_line
  command -v systemctl >/dev/null 2>&1 \
    || { printf 'error|systemctl unavailable'; return 0; }
  set +e
  output="$(LC_ALL=C systemctl --user "$query" "$unit" 2>&1)"
  rc=$?
  set -e
  first_line="${output%%$'\n'*}"
  first_line="${first_line//$'\t'/ }"
  first_line="${first_line//|/}"
  case "$query:$rc:$first_line" in
    is-active:0:active|is-active:0:reloading|is-active:0:activating|is-active:0:deactivating)
      printf 'ok|%s' "$first_line" ;;
    is-active:3:inactive|is-active:3:failed)
      printf 'ok|%s' "$first_line" ;;
    is-active:4:inactive)
      printf 'ok|not-found' ;;
    is-enabled:0:enabled|is-enabled:0:enabled-runtime|is-enabled:0:linked|is-enabled:0:linked-runtime|is-enabled:0:alias|is-enabled:0:static|is-enabled:0:indirect|is-enabled:0:generated|is-enabled:0:transient)
      printf 'ok|%s' "$first_line" ;;
    is-enabled:1:disabled|is-enabled:1:masked|is-enabled:1:masked-runtime)
      printf 'ok|%s' "$first_line" ;;
    is-enabled:4:not-found)
      printf 'ok|not-found' ;;
    *)
      [[ -n "$first_line" ]] || first_line="exit $rc with no state"
      printf 'error|%s' "$first_line" ;;
  esac
}

retire_registry_consumer_metadata() {  # retire_registry_consumer_metadata <agent_id>
  local target_agent="$1" lock_file="${REGISTRY_FILE}.lock" registry_lock_fd=""
  command -v flock >/dev/null 2>&1 \
    || { echo "fleet-sync: flock is required for registry updates" >&2; return 1; }
  [[ ! -L "$lock_file" ]] \
    || { echo "fleet-sync: refusing registry lock symlink: $lock_file" >&2; return 1; }
  exec {registry_lock_fd}>"$lock_file"
  chmod 600 "$lock_file"
  flock -w 30 "$registry_lock_fd" \
    || { echo "fleet-sync: timed out waiting for registry lock" >&2; return 1; }
  python3 - "$REGISTRY_FILE" "$target_agent" <<'PYEOF'
import errno
import os
import pathlib
import sys
import tempfile

try:
    import yaml
except ImportError:
    raise SystemExit("fleet-sync: python3-yaml is required")

path = pathlib.Path(sys.argv[1])
agent_id = sys.argv[2]
if path.is_symlink():
    raise SystemExit(f"fleet-sync: refusing registry symlink: {path}")
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
entry = (data.get("agents") or {}).get(agent_id)
if isinstance(entry, dict) and isinstance(entry.get("systemd"), dict):
    entry["systemd"].pop("consumer_unit", None)
rendered = yaml.safe_dump(data, sort_keys=False)

def fsync_parent(target):
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(target.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)

fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.fleet-sync-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    fsync_parent(path)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF
  flock -u "$registry_lock_fd"
  exec {registry_lock_fd}>&-
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

while IFS=$'\x1f' read -r agent_id role_dir profile_name gateway_unit consumer_unit; do
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

  # Launcher dependencies are template-controlled lifecycle assets. Reconcile
  # them before the launcher so an applied wrapper is never left pointing at a
  # missing or weaker fleet.env consumer.
  role_lib_dir="$role_dir/.scripts/lib"
  if [[ -L "$role_dir/.scripts" || -L "$role_lib_dir" ]]; then
    note "$agent_id" DRIFT "role .scripts/lib is a symlink (MANUAL: replace with a real directory)"
    DRIFT=$((DRIFT + 1))
    continue
  fi
  fleet_assets_eligible=1
  for fleet_asset in fleet-env.sh parse-fleet-env.py; do
    source_asset="$SCRIPT_DIR/../template/.scripts/lib/$fleet_asset"
    target_asset="$role_lib_dir/$fleet_asset"
    if [[ -L "$target_asset" ]]; then
      note "$agent_id" DRIFT "$target_asset is a symlink (MANUAL: replace with an attested regular file)"
      DRIFT=$((DRIFT + 1))
      fleet_assets_eligible=0
      continue
    fi
    if [[ ! -f "$target_asset" ]] || ! cmp -s "$source_asset" "$target_asset"; then
      if [[ $APPLY -eq 1 ]]; then
        mkdir -p "$role_lib_dir"
        cp "$source_asset" "$target_asset"
        note "$agent_id" FIXED "fleet loader asset refreshed: $fleet_asset"
        changed=1; FIXED=$((FIXED + 1))
      else
        note "$agent_id" DRIFT "fleet loader asset differs: $fleet_asset"
        DRIFT=$((DRIFT + 1))
      fi
    fi
  done
  heartbeat_target="$role_dir/.scripts/heartbeat.sh"
  if [[ -L "$heartbeat_target" ]]; then
    note "$agent_id" DRIFT "$heartbeat_target is a symlink (MANUAL: replace with an attested regular file)"
    DRIFT=$((DRIFT + 1))
    fleet_assets_eligible=0
  elif [[ ! -f "$heartbeat_target" ]] || ! cmp -s "$HEARTBEAT_TEMPLATE" "$heartbeat_target"; then
    if [[ $APPLY -eq 1 ]]; then
      mkdir -p "$role_dir/.scripts"
      cp "$HEARTBEAT_TEMPLATE" "$heartbeat_target"
      chmod +x "$heartbeat_target"
      note "$agent_id" FIXED "fleet-aware heartbeat refreshed"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "fleet-aware heartbeat differs from template"
      DRIFT=$((DRIFT + 1))
    fi
  fi
  if [[ $APPLY -eq 1 ]]; then
    for attestation in \
      "$FLEET_ENV_LIBRARY_SOURCE|$role_lib_dir/fleet-env.sh" \
      "$FLEET_ENV_PARSER_SOURCE|$role_lib_dir/parse-fleet-env.py" \
      "$HEARTBEAT_TEMPLATE|$heartbeat_target"
    do
      source_asset="${attestation%%|*}"
      target_asset="${attestation#*|}"
      if [[ ! -f "$target_asset" || -L "$target_asset" ]] \
        || ! cmp -s "$source_asset" "$target_asset"; then
        fleet_assets_eligible=0
      fi
    done
  fi
  [[ $fleet_assets_eligible -eq 1 ]] || continue

  # Legacy per-profile Bloodbank consumers are unhealthy until fully retired.
  legacy_consumer_unit="${consumer_unit:-hermes-${agent_id}-consumer.service}"
  if [[ ! "$legacy_consumer_unit" =~ ^hermes-[A-Za-z0-9._-]+-consumer\.service$ ]]; then
    note "$agent_id" DRIFT "unsafe legacy consumer unit name in registry: $legacy_consumer_unit (MANUAL: fix registry)"
    DRIFT=$((DRIFT + 1))
  else
    legacy_consumer_path="$HOME/.config/systemd/user/$legacy_consumer_unit"
    legacy_consumer_present=0
    [[ -n "$consumer_unit" || -e "$legacy_consumer_path" || -L "$legacy_consumer_path" ]] \
      && legacy_consumer_present=1
    if command -v systemctl >/dev/null 2>&1; then
      legacy_active_result="$(systemctl_user_unit_state is-active "$legacy_consumer_unit")"
      legacy_enabled_result="$(systemctl_user_unit_state is-enabled "$legacy_consumer_unit")"
      if [[ "$legacy_active_result" == error\|* || "$legacy_enabled_result" == error\|* ]]; then
        note "$agent_id" DRIFT "legacy consumer state query failed; unit and metadata preserved"
        DRIFT=$((DRIFT + 1))
        legacy_consumer_query_failed=1
      else
        legacy_consumer_query_failed=0
        legacy_active_state="${legacy_active_result#*|}"
        legacy_enabled_state="${legacy_enabled_result#*|}"
        [[ "$legacy_active_state" == "not-found" && "$legacy_enabled_state" == "not-found" ]] \
          || legacy_consumer_present=1
      fi
    elif [[ $legacy_consumer_present -eq 1 ]]; then
      note "$agent_id" DRIFT "systemctl unavailable; legacy unit and metadata preserved"
      DRIFT=$((DRIFT + 1))
      legacy_consumer_query_failed=1
    else
      legacy_consumer_query_failed=0
    fi
    if [[ $legacy_consumer_query_failed -eq 0 && $legacy_consumer_present -eq 1 ]]; then
      if [[ $APPLY -eq 0 ]]; then
        note "$agent_id" DRIFT "legacy per-profile Bloodbank consumer remains: $legacy_consumer_unit"
        DRIFT=$((DRIFT + 1))
      else
        if ! systemctl --user disable --now "$legacy_consumer_unit" >/dev/null 2>&1; then
          note "$agent_id" DRIFT "legacy consumer disable failed; unit and metadata preserved"
          DRIFT=$((DRIFT + 1))
        else
          legacy_active_result="$(systemctl_user_unit_state is-active "$legacy_consumer_unit")"
          legacy_enabled_result="$(systemctl_user_unit_state is-enabled "$legacy_consumer_unit")"
          if [[ "$legacy_active_result" != "ok|inactive" || "$legacy_enabled_result" != "ok|disabled" ]]; then
            note "$agent_id" DRIFT "legacy consumer is not proven inactive and disabled; unit and metadata preserved"
            DRIFT=$((DRIFT + 1))
          else
            rm -f -- "$legacy_consumer_path"
            systemctl --user daemon-reload >/dev/null 2>&1 || true
            retire_registry_consumer_metadata "$agent_id"
            note "$agent_id" FIXED "legacy per-profile Bloodbank consumer retired"
            FIXED=$((FIXED + 1))
          fi
        fi
      fi
    fi
  fi

  # 1. Launcher wrapper regenerated from the current template.
  expected_wrapper="$(render_wrapper "$agent_id" "$profile_name")"
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

  # 3. Singleton-runtime profile ~/.hermes/profiles/<profile_name>.
  # LOAD-BEARING and NOT a symlink: Hermes derives the profile identity from
  # the unresolved HERMES_HOME path, so symlinking the profile dir itself makes
  # get_active_profile_name() report "default" and _global_auth_file_path()
  # return None — silently disabling shared fleet auth and giving the agent a
  # divergent config.yaml. The dir is real; its ENTRIES are the symlinks.
  profile_dir="$PROFILES_DIR/$profile_name"

  # ensure_profile_link <label> <link path> <target> <mkdir target dir?>
  ensure_profile_link() {
    local label="$1" path="$2" target="$3" mkdir_target="$4"
    if [[ -L "$path" ]]; then
      [[ "$(readlink -f "$path")" == "$(readlink -f "$target")" ]] && return 0
      if [[ $APPLY -eq 1 ]]; then
        [[ "$mkdir_target" == "dir" ]] && mkdir -p "$target"
        ln -sfn "$target" "$path"
        note "$agent_id" FIXED "profile $label repointed -> $target"
        changed=1; FIXED=$((FIXED + 1))
      else
        note "$agent_id" DRIFT "profile $label points at $(readlink "$path")"
        DRIFT=$((DRIFT + 1))
      fi
      return 0
    fi
    if [[ -e "$path" ]]; then
      note "$agent_id" DRIFT "profile $label holds real data, expected symlink -> $target (MANUAL: merge state)"
      DRIFT=$((DRIFT + 1))
      return 0
    fi
    if [[ $APPLY -eq 1 ]]; then
      [[ "$mkdir_target" == "dir" ]] && mkdir -p "$target"
      ln -sfn "$target" "$path"
      note "$agent_id" FIXED "profile $label linked -> $target"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "profile $label missing (expected symlink -> $target)"
      DRIFT=$((DRIFT + 1))
    fi
  }

  if [[ -L "$profile_dir" ]]; then
    # The superseded contract. Converting it back needs a data merge decision.
    note "$agent_id" DRIFT "$profile_dir is a symlink; the profile dir must be a REAL dir (MANUAL: pj migrate hermes.runtime-singleton)"
    DRIFT=$((DRIFT + 1))
  elif [[ ! -d "$profile_dir" ]]; then
    if [[ -e "$profile_dir" ]]; then
      note "$agent_id" DRIFT "$profile_dir exists and is not a directory (MANUAL: remove or merge)"
      DRIFT=$((DRIFT + 1))
    elif [[ $APPLY -eq 1 ]]; then
      mkdir -p "$profile_dir"
      note "$agent_id" FIXED "profile dir created: $profile_dir"
      changed=1; FIXED=$((FIXED + 1))
    else
      note "$agent_id" DRIFT "profile dir missing: $profile_dir"
      DRIFT=$((DRIFT + 1))
    fi
  fi

  if [[ -d "$profile_dir" && ! -L "$profile_dir" ]]; then
    for entry in "${SHARED_PROFILE_ENTRIES[@]}"; do
      ensure_profile_link "$entry" "$profile_dir/$entry" "$FLEET_HOME/$entry" \
        "$([[ "$entry" == "skills" ]] && echo dir || echo file)"
    done
    for entry in "${OWNED_PROFILE_ENTRIES[@]}"; do
      ensure_profile_link "$entry" "$profile_dir/$entry" "$runtime/$entry" dir
    done
    for entry in "${OWNED_PROFILE_FILES[@]}"; do
      ensure_profile_link "$entry" "$profile_dir/$entry" "$runtime/$entry" file
    done

    # config.yaml is GENERATED and must be a real file. Its only sources of
    # truth are the fleet base and this profile's config.delta.yaml. A healthy
    # dry run therefore checks semantic equality and never asks for the legacy
    # config.yaml -> fleet-base symlink.
    if [[ ! -f "$profile_dir/config.delta.yaml" || -L "$profile_dir/config.delta.yaml" ]]; then
      if [[ $APPLY -eq 1 && ! -e "$profile_dir/config.delta.yaml" ]]; then
        printf '%s\n' '# Override-only delta for this Hermes profile.' '{}' \
          > "$profile_dir/config.delta.yaml"
        chmod 600 "$profile_dir/config.delta.yaml"
        note "$agent_id" FIXED "empty config.delta.yaml seeded"
        changed=1; FIXED=$((FIXED + 1))
      else
        note "$agent_id" DRIFT "config.delta.yaml missing or unsafe (MANUAL: establish override source)"
        DRIFT=$((DRIFT + 1))
      fi
    fi
    if [[ -f "$profile_dir/config.delta.yaml" && ! -L "$profile_dir/config.delta.yaml" ]]; then
      if ! HERMES_FLEET_HOME="$FLEET_HOME" \
          python3 "$PROFILE_CONFIG_TOOL" check --profile "$profile_name" >/dev/null 2>&1; then
        if [[ $APPLY -eq 1 ]]; then
          HERMES_FLEET_HOME="$FLEET_HOME" \
            python3 "$PROFILE_CONFIG_TOOL" render --profile "$profile_name" >/dev/null \
            || { note "$agent_id" DRIFT "base+delta config render failed"; DRIFT=$((DRIFT + 1)); continue; }
          note "$agent_id" FIXED "config.yaml rendered from fleet base + delta"
          changed=1; FIXED=$((FIXED + 1))
        else
          note "$agent_id" DRIFT "config.yaml != deep_merge(fleet base, config.delta.yaml)"
          DRIFT=$((DRIFT + 1))
        fi
      fi
    fi
  fi

  # 3b. systemd units must point HERMES_HOME at that profile dir. This is the
  # split-brain that the old symlink rule masked: units ran against the profile
  # while the launcher ran against the raw runtime, so an agent's interactive
  # and daemon halves used different config, sessions, and auth.
  for unit in "hermes-${agent_id}-gateway.service" \
              "hermes-${agent_id}-heartbeat.service" \
              "hermes-${agent_id}-checkpoint.service"; do
    unit_path="$SYSTEMD_USER_DIR/$unit"
    [[ -f "$unit_path" ]] || continue
    current_home="$(sed -n 's/^Environment=HERMES_HOME=//p' "$unit_path" | head -1)"
    [[ -n "$current_home" ]] || continue
    [[ "$current_home" == "$profile_dir" ]] && continue
    # Never point a unit at a profile dir that is still a symlink or absent —
    # that is the very state that breaks profile identity. Migrate the layout
    # first, then the unit repoint becomes safe.
    if [[ ! -d "$profile_dir" || -L "$profile_dir" ]]; then
      note "$agent_id" DRIFT "$unit HERMES_HOME=$current_home (expected $profile_dir; blocked until the profile dir is a real dir)"
      DRIFT=$((DRIFT + 1))
      continue
    fi
    if [[ $APPLY -eq 1 ]]; then
      sed -i "s|^Environment=HERMES_HOME=.*|Environment=HERMES_HOME=$profile_dir|" "$unit_path"
      note "$agent_id" FIXED "$unit HERMES_HOME -> $profile_dir"
      changed=1; FIXED=$((FIXED + 1)); UNITS_TOUCHED=1
    else
      note "$agent_id" DRIFT "$unit HERMES_HOME=$current_home (expected $profile_dir)"
      DRIFT=$((DRIFT + 1))
    fi
  done

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

  # 5. PM fleet voice contract: the plugin lives at plugins/tts/$VOX_PLUGIN_NAME
  # under the agent's real HERMES_HOME (the profile dir) — linking it into the
  # raw runtime instead puts it somewhere Hermes never reads.
  role_name="$(basename "$role_dir")"
  if [[ "$role_name" == "pm" && -d "$runtime" ]]; then
    if [[ $VOX_AVAILABLE -eq 1 && -d "$profile_dir" && ! -L "$profile_dir" ]]; then
      plugin_link="$profile_dir/plugins/tts/$VOX_PLUGIN_NAME"
      if [[ -L "$plugin_link" ]]; then
        if [[ "$(readlink -f "$plugin_link")" != "$(readlink -f "$VOX_PLUGIN_DIR")" ]]; then
          if [[ $APPLY -eq 1 ]]; then
            ln -sfn "$VOX_PLUGIN_DIR" "$plugin_link"
            note "$agent_id" FIXED "profile $VOX_PLUGIN_NAME plugin relinked"
            changed=1; FIXED=$((FIXED + 1))
          else
            note "$agent_id" DRIFT "profile $VOX_PLUGIN_NAME plugin points at $(readlink "$plugin_link")"
            DRIFT=$((DRIFT + 1))
          fi
        fi
      elif [[ -e "$plugin_link" ]]; then
        note "$agent_id" DRIFT "$plugin_link exists and is not a symlink (MANUAL: merge plugin state)"
        DRIFT=$((DRIFT + 1))
      else
        if [[ $APPLY -eq 1 ]]; then
          mkdir -p "$profile_dir/plugins/tts"
          ln -sfn "$VOX_PLUGIN_DIR" "$plugin_link"
          note "$agent_id" FIXED "profile $VOX_PLUGIN_NAME plugin linked"
          changed=1; FIXED=$((FIXED + 1))
        else
          note "$agent_id" DRIFT "profile $VOX_PLUGIN_NAME plugin missing"
          DRIFT=$((DRIFT + 1))
        fi
      fi
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

    # The effective config is generated from base + delta. Enforce PM voice by
    # updating the override source, then rerender; never mutate generated
    # config.yaml through `hermes config set`.
    if [[ $VOX_AVAILABLE -eq 1 && -d "$profile_dir" && ! -L "$profile_dir" ]]; then
      config_status="$(python3 - "$profile_dir/config.yaml" "$profile_dir/config.delta.yaml" \
          "$VOX_PLUGIN_NAME" "$VOX_VOICE" <<'PYEOF'
import pathlib, sys
try:
    import yaml
except ImportError:
    print("invalid|invalid|invalid|invalid")
    raise SystemExit(0)

path, delta_path = map(pathlib.Path, sys.argv[1:3])
plugin, voice = sys.argv[3:5]
if not path.is_file() or path.is_symlink():
    print("missing|missing|missing|missing")
    raise SystemExit(0)
try:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    delta = yaml.safe_load(delta_path.read_text(encoding="utf-8")) or {}
except Exception:
    print("invalid|invalid|invalid|invalid")
    raise SystemExit(0)
if not isinstance(config, dict) or not isinstance(delta, dict):
    print("invalid|invalid|invalid|invalid")
    raise SystemExit(0)
plugins = config.get("plugins") or {}
if isinstance(plugins, dict):
    enabled_values = plugins.get("enabled") or []
else:
    enabled_values = []
tts = config.get("tts") or {}
if not isinstance(tts, dict):
    tts = {}
provider = "ok" if tts.get("provider") == plugin else "other"
enabled = "ok" if isinstance(enabled_values, list) and f"tts/{plugin}" in enabled_values else "no"
provider_config = tts.get(plugin) or {}
actual_voice = provider_config.get("voice") if isinstance(provider_config, dict) else None
actual_voice = actual_voice or tts.get("voice")
voice_ok = "ok" if actual_voice == voice else "other"
delta_plugins = delta.get("plugins") or {}
explicit = delta_plugins.get("enabled") if isinstance(delta_plugins, dict) else None
directive = delta.get("x-pjangler-merge") or {}
migrations = directive.get("migrations") if isinstance(directive, dict) else None
snapshot = migrations.get("plugins_enabled_snapshot") if isinstance(migrations, dict) else None
if snapshot is None:
    migration = "ok"
elif not isinstance(snapshot, dict):
    migration = "invalid"
elif snapshot.get("state", "pending") == "pending" and isinstance(explicit, list):
    migration = "pending"
elif snapshot.get("state") == "completed":
    migration = "ok"
else:
    migration = "invalid"
print(f"{provider}|{enabled}|{voice_ok}|{migration}")
PYEOF
)"
      if [[ "$config_status" != "ok|ok|ok|ok" ]]; then
        if [[ "$APPLY" -eq 1 ]]; then
          if python3 - "$FLEET_HOME/config.yaml" "$profile_dir/config.delta.yaml" \
              "$VOX_PLUGIN_NAME" "$VOX_VOICE" <<'PYEOF'
import os, pathlib, sys, tempfile
try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML is required")
base_path, delta_path = map(pathlib.Path, sys.argv[1:3])
plugin, voice = sys.argv[3:5]
base = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
delta_text = delta_path.read_text(encoding="utf-8")
delta = yaml.safe_load(delta_text) or {}
if not isinstance(base, dict) or not isinstance(delta, dict):
    raise SystemExit("base and delta must be mappings")
plugins = delta.get("plugins") or {}
if not isinstance(plugins, dict):
    raise SystemExit("plugins delta must be a mapping")
if "enabled" in plugins and not isinstance(plugins["enabled"], list):
    raise SystemExit("plugins.enabled delta must be a list")
directives = delta.setdefault("x-pjangler-merge", {})
if not isinstance(directives, dict):
    raise SystemExit("x-pjangler-merge delta must be a mapping")
patches = directives.setdefault("list_patches", {})
if not isinstance(patches, dict):
    raise SystemExit("x-pjangler-merge.list_patches delta must be a mapping")
patch = patches.setdefault("plugins.enabled", {})
if not isinstance(patch, dict):
    raise SystemExit("plugins.enabled list patch must be a mapping")
additions = patch.setdefault("add", [])
removals = patch.setdefault("remove", [])
if not isinstance(additions, list) or not isinstance(removals, list):
    raise SystemExit("plugins.enabled list patch values must be lists")
role_plugin = f"tts/{plugin}"
if not all(isinstance(entry, str) for entry in [*additions, *removals]):
    raise SystemExit("plugins.enabled list patch values must be string lists")
explicit_enabled = plugins.get("enabled")
if explicit_enabled is not None and not isinstance(explicit_enabled, list):
    raise SystemExit("plugins.enabled delta must be a list")
migrations = directives.get("migrations", {})
if not isinstance(migrations, dict):
    raise SystemExit("x-pjangler-merge.migrations delta must be a mapping")
snapshot = migrations.get("plugins_enabled_snapshot")
if snapshot is not None:
    if not isinstance(snapshot, dict):
        raise SystemExit("plugins_enabled_snapshot migration must be a mapping")
    source = snapshot.get("source")
    state = snapshot.get("state", "pending")
    inherited = snapshot.get("inherited")
    if source != "pjangler-52d9445":
        raise SystemExit("plugins_enabled_snapshot migration has unknown provenance")
    if not isinstance(inherited, list) or not inherited or not all(
        isinstance(entry, str) for entry in inherited
    ):
        raise SystemExit("plugins_enabled_snapshot.inherited must be a non-empty string list")
    if state == "pending":
        if not isinstance(explicit_enabled, list) or not all(
            isinstance(entry, str) for entry in explicit_enabled
        ):
            raise SystemExit("provenance-backed plugin snapshot is missing its replacement list")
        inherited_set = set(inherited)
        for entry in explicit_enabled:
            if (
                entry not in inherited_set
                and entry not in {role_plugin, "tts/voxxy"}
                and entry not in removals
                and entry not in additions
            ):
                additions.append(entry)
        plugins.pop("enabled")
        if not plugins:
            delta.pop("plugins", None)
        snapshot["state"] = "completed"
    elif state != "completed":
        raise SystemExit("plugins_enabled_snapshot migration state must be pending or completed")
additions[:] = [entry for entry in additions if entry not in {role_plugin, "tts/voxxy"}]
additions.append(role_plugin)
removals[:] = [entry for entry in removals if entry != role_plugin]
if "tts/voxxy" not in removals:
    removals.append("tts/voxxy")
tts = delta.setdefault("tts", {})
tts.pop("voxxy", None)
tts["provider"] = plugin
tts.setdefault(plugin, {})["voice"] = voice
tts["voice"] = voice
fd, temporary = tempfile.mkstemp(prefix=f".{delta_path.name}.", dir=delta_path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        comments = []
        for line in delta_text.splitlines():
            if line.lstrip().startswith("#") and line not in comments:
                comments.append(line)
        standard = "# Override-only delta for this Hermes profile."
        handle.write(standard + "\n")
        for line in comments:
            if line != standard:
                handle.write(line + "\n")
        handle.write(yaml.safe_dump(delta, sort_keys=False))
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, delta_path)
except BaseException:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
    raise
PYEOF
          then
            HERMES_FLEET_HOME="$FLEET_HOME" \
              python3 "$PROFILE_CONFIG_TOOL" render --profile "$profile_name" >/dev/null \
              || { note "$agent_id" DRIFT "Vox delta saved but config render failed"; DRIFT=$((DRIFT + 1)); continue; }
            note "$agent_id" FIXED "TTS delta enforced -> $VOX_PLUGIN_NAME/$VOX_VOICE"
            changed=1; FIXED=$((FIXED + 1))
          else
            note "$agent_id" DRIFT "TTS delta update failed"
            DRIFT=$((DRIFT + 1))
          fi
        else
          note "$agent_id" DRIFT "TTS config not $VOX_PLUGIN_NAME/$VOX_VOICE in $profile_dir/config.yaml ($config_status)"
          DRIFT=$((DRIFT + 1))
        fi
      fi
    fi
  fi

  if [[ $changed -eq 1 && $RESTART -eq 1 ]]; then
    [[ -n "$gateway_unit" ]] && RESTART_UNITS+=("$gateway_unit")
  fi
done < <(read_registry)

# 6. Stale profile entries: names in ~/.hermes/profiles the registry does not
# know. Reported only — removal is a human call.
known_names="$(read_registry | cut -d $'\x1f' -f3 | sort -u)"
if [[ -d "$PROFILES_DIR" ]]; then
  for link in "$PROFILES_DIR"/*; do
    [[ -e "$link" || -L "$link" ]] || continue
    name="$(basename "$link")"
    grep -qx "$name" <<<"$known_names" && continue
    if [[ -L "$link" ]]; then
      target="$(readlink "$link")"
      [[ "$target" == */agents/hermes/* ]] || continue
      note "$name" STALE "profile symlink not in agents-registry -> $target (MANUAL: remove or register)"
      DRIFT=$((DRIFT + 1))
    fi
  done
fi

# Vox is optional; say so once instead of once per PM.
if [[ $VOX_AVAILABLE -eq 0 ]]; then
  echo
  echo "note: Vox TTS plugin not installed (looked for plugins/tts/$VOX_PLUGIN_NAME)."
  echo "      PM voice checks skipped — this is an optional capability, not drift."
fi

if [[ $UNITS_TOUCHED -eq 1 ]]; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if [[ ${#RESTART_UNITS[@]} -gt 0 ]]; then
  echo
  echo "Restarting: ${RESTART_UNITS[*]}"
  systemctl --user try-restart "${RESTART_UNITS[@]}" || true
fi

echo
if [[ $APPLY -eq 1 ]]; then
  echo "fleet-sync: $FIXED fixed, $DRIFT unresolved (manual)"
  if [[ $DRIFT -gt 0 ]]; then
    echo "  Items marked MANUAL need a human decision — they would destroy or merge"
    echo "  existing state. Most profile-layout cases are handled by:"
    echo "      pj migrate hermes.runtime-singleton"
  fi
else
  echo "fleet-sync: $DRIFT drift item(s). Apply the fixable ones with:"
  echo "      mise run fleet:sync"
  echo "  (or run this script directly with --apply)"
fi
[[ $DRIFT -eq 0 ]] || exit 1
