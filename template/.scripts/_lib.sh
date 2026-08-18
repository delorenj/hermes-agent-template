# shellcheck shell=bash
# Common helpers sourced by every numbered provisioning step.

set -euo pipefail

# These three are set by Copier into the rendered role.yaml; we re-derive them
# here so each script is callable in isolation (e.g. for repair runs).
ROLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE_YAML="$ROLE_DIR/role.yaml"
PROV_LOG="$ROLE_DIR/.scripts/.provision.log"

mkdir -p "$ROLE_DIR/.scripts"

# Logging
log()  { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[36m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
warn() { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[33m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
err()  { local msg="[$(date +%H:%M:%S)] $*"; printf '\033[31m%s\033[0m\n' "$msg" >&2; printf '%s\n' "$msg" >> "$PROV_LOG"; }
die()  { err "$*"; exit 1; }

# Read a single field from role.yaml. Requires python3 (no yaml dep).
yaml_get() {
  # yaml_get  KEY[.SUBKEY]    e.g.  yaml_get role,  yaml_get telegram.bot_username
  local key="$1"
  python3 - "$ROLE_YAML" "$key" <<'PYEOF'
import sys, re, pathlib
path, key = sys.argv[1:3]
text = pathlib.Path(path).read_text()
parts = key.split(".")
# Trivial YAML walker — handles flat and one-level nested keys.
indent = -1
prefix = ""
for part in parts[:-1]:
    indent += 2
    prefix += part + ":"
    m = re.search(rf"(?m)^{re.escape(part)}:\s*$", text)
    if not m:
        sys.exit(0)
    text = text[m.end():]
key = parts[-1]
m = re.search(rf'(?m)^\s*{re.escape(key)}:\s*"?([^"\n]*)"?\s*$', text)
if m:
    print(m.group(1).strip())
PYEOF
}

# Apply a sed substitution to role.yaml in-place. Used to record IDs after
# external provisioning steps return them.
yaml_set() {
  # yaml_set KEY VALUE   (only updates the first match; key must already exist)
  local key="$1" val="$2"
  python3 - "$ROLE_YAML" "$key" "$val" <<'PYEOF'
import sys, re, pathlib
path, key, val = sys.argv[1:4]
p = pathlib.Path(path); text = p.read_text()
# Match `<indent><key>:<...>` and rewrite the value (last leaf only).
leaf = key.split(".")[-1]
new = re.sub(rf'(?m)^(\s*{re.escape(leaf)}:\s*)("?)[^"\n]*("?)\s*$',
             lambda m: f'{m.group(1)}"{val}"', text, count=1)
if new == text:
    sys.exit(f"yaml_set: leaf '{leaf}' not found in {path}")
p.write_text(new)
PYEOF
}

# ─── Distributable config (~/.config/hermes-agent-template/config.toml) ──────
# Single source of truth for environment-specific defaults so this template can
# be handed to someone else without editing any script. Ship config.example.toml
# is copied here on first provision (see .scripts/01-config.sh).
HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
export HERMES_TEMPLATE_CONFIG

# config_get <dotted.key> [default]   — print a value from config.toml (paths are
# tilde-expanded; arrays are space-joined). Falls back to [default] when the file,
# python3, or the key is missing. Always exits 0 so it's safe under `set -e`.
config_get() {
  local key="$1" def="${2:-}"
  if [[ ! -f "$HERMES_TEMPLATE_CONFIG" ]] || ! command -v python3 >/dev/null 2>&1; then
    printf '%s' "$def"; return 0
  fi
  python3 - "$HERMES_TEMPLATE_CONFIG" "$key" "$def" <<'PYEOF' || printf '%s' "$def"
import sys, os
try:
    import tomllib
except ModuleNotFoundError:  # python < 3.11
    print(sys.argv[3], end=""); sys.exit(0)
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "rb") as f:
        cur = tomllib.load(f)
except Exception:
    print(default, end=""); sys.exit(0)
for part in key.split("."):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print(default, end=""); sys.exit(0)
if isinstance(cur, list):
    cur = " ".join(str(x) for x in cur)
else:
    cur = str(cur)
print(os.path.expanduser(cur), end="")
PYEOF
  return 0
}

# Re-export role fields into the environment for the rest of the script.
load_role_env() {
  ROLE=$(yaml_get role)
  REPO=$(yaml_get repo)
  AGENT_ID=$(yaml_get agent_id)
  DISPLAY_NAME=$(yaml_get display_name)
  BOT_HANDLE=$(yaml_get telegram.bot_username)
  PROFILE_NAME=$(yaml_get profile)

  # Plane workspace: empty in role.yaml -> resolve from config.toml.
  PLANE_WORKSPACE=$(yaml_get plane.workspace)
  [[ -n "$PLANE_WORKSPACE" ]] || PLANE_WORKSPACE=$(config_get plane.workspace "33god")

  # Runtime repo: role.yaml stores the bare repo name plus an optional owner.
  # Older manifests stored "owner/name" directly in github_repo; honor both.
  RUNTIME_REPO=$(yaml_get runtime.github_repo)
  if [[ "$RUNTIME_REPO" != */* ]]; then
    local owner; owner=$(yaml_get runtime.github_owner)
    [[ -n "$owner" ]] || owner=$(config_get github.runtime_repo_owner "delorenj")
    RUNTIME_REPO="$owner/$RUNTIME_REPO"
  fi

  export ROLE REPO AGENT_ID DISPLAY_NAME BOT_HANDLE \
         PLANE_WORKSPACE RUNTIME_REPO PROFILE_NAME
}

# Skip a step if previously completed (idempotent reruns).
already_done() {
  local marker="$ROLE_DIR/.scripts/.done-$1"
  [[ -f "$marker" ]]
}
mark_done() {
  touch "$ROLE_DIR/.scripts/.done-$1"
}
clear_done() {
  # Remove only the marker for the explicitly deferred step.  This lets a
  # later activation reconcile that phase without disturbing completed,
  # unrelated provisioning state.
  rm -f -- "$ROLE_DIR/.scripts/.done-$1"
}

# fleet.env is shared configuration, not authority to inject code into Python,
# shell, Node, or dynamic-loader children. PATH remains intact so explicitly
# configured Hermes/PJangler/provider tools still resolve.
subprocess_injection_key_is_unsafe() {
  local key="$1" loader_key="$1"
  case "$key" in
    PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|PYTHONUSERBASE|\
    BASH_ENV|ENV|BASHOPTS|SHELLOPTS|BASH_COMPAT|BASH_LOADABLES_PATH|\
    BASH_XTRACEFD|PROMPT_COMMAND|PS0|PS1|PS2|PS3|PS4|\
    NODE_OPTIONS|NODE_PATH|GLIBC_TUNABLES|BASH_FUNC_*|DYLD_*)
      return 0
      ;;
  esac

  # GNU and multilib loaders consume the same control stem with an optional
  # _32/_64 ABI suffix. Do not delete unrelated application keys such as
  # LD_SDK_KEY merely because they share the LD_ prefix.
  case "$loader_key" in
    LD_*_32|LD_*_64) loader_key="${loader_key%_*}" ;;
  esac
  case "$loader_key" in
    LD_ASSUME_KERNEL|LD_AUDIT|LD_BIND_NOT|LD_BIND_NOW|LD_DEBUG|\
    LD_DEBUG_OUTPUT|LD_DYNAMIC_WEAK|LD_HWCAP_MASK|LD_LIBRARY_PATH|\
    LD_ORIGIN_PATH|LD_POINTER_GUARD|LD_PREFER_MAP_32BIT_EXEC|LD_PRELOAD|\
    LD_PROFILE|LD_PROFILE_OUTPUT|LD_SHOW_AUXV|LD_TRACE_LOADED_OBJECTS|\
    LD_TRACE_PRELINKING|LD_USE_LOAD_BIAS|LD_VERBOSE|LD_WARN)
      return 0
      ;;
  esac
  return 1
}

scrub_subprocess_interpreter_injection() {
  local key declaration function_name

  # A fleet file may export Bash's readonly option variables or enable tracing.
  # Disable trace output first, then remove the export attribute where Bash
  # cannot unset the variable itself. BASH_XTRACEFD is also unexported rather
  # than unset because unsetting it can close the referenced descriptor.
  builtin set +x +v
  while IFS= read -r key; do
    if subprocess_injection_key_is_unsafe "$key"; then
      case "$key" in
        BASHOPTS|SHELLOPTS|BASH_XTRACEFD)
          builtin export -n "$key" 2>/dev/null || true
          ;;
        *)
          builtin unset -v "$key" 2>/dev/null || true
          ;;
      esac
    fi
  done < <(builtin compgen -A variable)

  # Bash imports arbitrary exported functions before script evaluation and
  # keeps them as live functions, not ordinary BASH_FUNC_* variables. Remove
  # the whole exported-function family so a fleet value cannot shadow python3,
  # git, systemctl, or any later helper in this provisioning shell.
  while IFS= read -r declaration; do
    function_name="${declaration##* }"
    [[ -n "$function_name" ]] && builtin unset -f -- "$function_name"
  done < <(builtin declare -Fx)

  builtin export PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1
}

# Apply staged records as one transaction. Existing variables always win. New
# variables are tracked as they are created and removed in reverse order if a
# later assignment/export fails, so no prefix of a failed import can escape.
apply_fleet_environment_records() {
  local -n __pjangler_fleet_keys_ref="$1"
  local -n __pjangler_fleet_values_ref="$2"
  local __pjangler_fleet_index __pjangler_fleet_key
  local -a __pjangler_fleet_applied=()

  if (( ${#__pjangler_fleet_keys_ref[@]} != ${#__pjangler_fleet_values_ref[@]} )); then
    builtin printf 'fleet environment apply failed: mismatched staging arrays\n' >&2
    return 1
  fi

  for ((__pjangler_fleet_index = 0; __pjangler_fleet_index < ${#__pjangler_fleet_keys_ref[@]}; __pjangler_fleet_index++)); do
    __pjangler_fleet_key="${__pjangler_fleet_keys_ref[__pjangler_fleet_index]}"
    if [[ ! "$__pjangler_fleet_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || subprocess_injection_key_is_unsafe "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment apply failed: rejected variable name\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi

    # A declared-but-unset or readonly caller value is still caller state and
    # therefore wins over fleet configuration.
    if builtin declare -p "$__pjangler_fleet_key" >/dev/null 2>&1; then
      continue
    fi
    if ! builtin printf -v "$__pjangler_fleet_key" '%s' \
      "${__pjangler_fleet_values_ref[__pjangler_fleet_index]}"; then
      builtin printf 'fleet environment apply failed: assignment rejected\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi
    __pjangler_fleet_applied+=("$__pjangler_fleet_key")
    if ! builtin export "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment apply failed: export rejected\n' >&2
      for ((__pjangler_fleet_index = ${#__pjangler_fleet_applied[@]} - 1; __pjangler_fleet_index >= 0; __pjangler_fleet_index--)); do
        builtin unset -v "${__pjangler_fleet_applied[__pjangler_fleet_index]}" 2>/dev/null || true
      done
      return 1
    fi
  done
}

# Consume a parser child through a complete, double-NUL-terminated protocol.
# Nothing reaches the provisioning shell until the child exits successfully and
# every record, name, duplicate, unsafe family, footer, and final terminator has
# been validated.
import_fleet_environment_stream() {
  local __pjangler_fleet_fd="$1" __pjangler_fleet_pid="$2"
  local __pjangler_fleet_count __pjangler_fleet_index
  local __pjangler_fleet_record __pjangler_fleet_key __pjangler_fleet_value
  local -a __pjangler_fleet_records=() __pjangler_fleet_keys=() __pjangler_fleet_values=()
  local -A __pjangler_fleet_seen=()

  builtin mapfile -d '' -t -u "$__pjangler_fleet_fd" __pjangler_fleet_records || true
  exec {__pjangler_fleet_fd}<&-
  if ! builtin wait "$__pjangler_fleet_pid"; then
    builtin printf 'fleet environment frame rejected: parser child failed\n' >&2
    return 1
  fi

  __pjangler_fleet_count="${#__pjangler_fleet_records[@]}"
  if (( __pjangler_fleet_count < 3 )) \
    || [[ "${__pjangler_fleet_records[0]}" != "PJANGLER_FLEET_ENV_V1" ]] \
    || [[ "${__pjangler_fleet_records[__pjangler_fleet_count - 2]}" != "PJANGLER_FLEET_ENV_END" ]] \
    || [[ -n "${__pjangler_fleet_records[__pjangler_fleet_count - 1]}" ]]; then
    builtin printf 'fleet environment frame rejected: incomplete framing\n' >&2
    return 1
  fi

  for ((__pjangler_fleet_index = 1; __pjangler_fleet_index < __pjangler_fleet_count - 2; __pjangler_fleet_index++)); do
    __pjangler_fleet_record="${__pjangler_fleet_records[__pjangler_fleet_index]}"
    if [[ "$__pjangler_fleet_record" != *=* ]]; then
      builtin printf 'fleet environment frame rejected: malformed record\n' >&2
      return 1
    fi
    __pjangler_fleet_key="${__pjangler_fleet_record%%=*}"
    __pjangler_fleet_value="${__pjangler_fleet_record#*=}"
    if [[ ! "$__pjangler_fleet_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || subprocess_injection_key_is_unsafe "$__pjangler_fleet_key"; then
      builtin printf 'fleet environment frame rejected: unsafe variable\n' >&2
      return 1
    fi
    if [[ -n "${__pjangler_fleet_seen[$__pjangler_fleet_key]+present}" ]]; then
      builtin printf 'fleet environment frame rejected: duplicate variable\n' >&2
      return 1
    fi
    __pjangler_fleet_seen["$__pjangler_fleet_key"]=1
    __pjangler_fleet_keys+=("$__pjangler_fleet_key")
    __pjangler_fleet_values+=("$__pjangler_fleet_value")
  done

  apply_fleet_environment_records __pjangler_fleet_keys __pjangler_fleet_values
}

# fleet.env is parsed as data by an isolated Python interpreter. The supported
# grammar is blank/comment lines plus optional `export` KEY=value assignments
# with unquoted, single-quoted, double-quoted, or ANSI-C quoted literal values.
# One legacy data-only expansion is accepted in unquoted values:
# `$HERMES_FLEET_HOME` (or its braced spelling) plus a literal path suffix.
# All other expansions, functions, readonly attributes, substitutions, and
# commands are rejected rather than executed.
import_fleet_environment() {
  local __pjangler_fleet_path="$1"
  local __pjangler_fleet_parser="$ROLE_DIR/.scripts/lib/parse-fleet-env.py"
  local __pjangler_fleet_python __pjangler_fleet_fd __pjangler_fleet_pid

  if [[ ! -f "$__pjangler_fleet_parser" || -L "$__pjangler_fleet_parser" ]]; then
    builtin printf 'fleet environment frame rejected: trusted parser is unavailable\n' >&2
    return 1
  fi
  __pjangler_fleet_python="$(builtin type -P python3)" || {
    builtin printf 'fleet environment frame rejected: python3 is unavailable\n' >&2
    return 1
  }

  exec {__pjangler_fleet_fd}< <(
    "$__pjangler_fleet_python" -I "$__pjangler_fleet_parser" "$__pjangler_fleet_path"
  )
  __pjangler_fleet_pid=$!
  import_fleet_environment_stream "$__pjangler_fleet_fd" "$__pjangler_fleet_pid"
}

# Fleet source-of-truth (shared across all wrappers/provisioners).
# Every default below resolves as: env var > fleet.env > config.toml > fallback.
# Invocation authority is not fleet configuration. Capture the caller's board
# gate before importing fleet.env so that file cannot weaken an MCP/CLI
# SKIP_PLANE decision or re-enable credentials by assigning SKIP_PLANE=0.
PJANGLER_INVOCATION_SKIP_PLANE="${SKIP_PLANE:-0}"
FLEET_ENV="${HERMES_FLEET_ENV:-$(config_get fleet.fleet_env "$HOME/.hermes/fleet.env")}"

# A direct CLI caller may not have passed through the MCP parent boundary.
# Harden the extractor's inherited environment, then repeat the scrub after
# import as defense in depth before any later child process can run.
scrub_subprocess_interpreter_injection
if [[ -f "$FLEET_ENV" ]] && ! import_fleet_environment "$FLEET_ENV"; then
  builtin printf 'fleet environment import failed: %s\n' "$FLEET_ENV" >&2
  return 1
fi
SKIP_PLANE="$PJANGLER_INVOCATION_SKIP_PLANE"
unset PJANGLER_INVOCATION_SKIP_PLANE
scrub_subprocess_interpreter_injection

# fleet.env is allowed to supply provider credentials only for an explicitly
# board-authorized invocation. A no-board/deferred phase must remove every
# supported provider alias after sourcing and before any Python, Hermes,
# systemd, provider, or other child process can inherit it.
scrub_ticket_provider_authority() {
  local key
  unset PLANE_API_KEY TRELLO_KEY TRELLO_TOKEN LINEAR_API_KEY
  while IFS= read -r key; do
    case "$key" in
      PLANE_*_API_KEY) unset "$key" ;;
    esac
  done < <(compgen -A variable PLANE_)
}
if [[ "$SKIP_PLANE" == "1" ]]; then
  scrub_ticket_provider_authority
fi
# Identity-bearing chat credentials are never fleet-scoped. Platform wiring
# steps capture explicit invocation values before sourcing this library and
# restore only those values afterward; all other provisioning steps stay clean.
unset TELEGRAM_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN

# Tools we expect on the host
HERMES_BIN="${HERMES_BIN:-${HERMES_FLEET_BIN:-$(config_get fleet.hermes_bin "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes")}}"
HERMES_AGENT_REPO="${HERMES_AGENT_REPO:-${HERMES_FLEET_REPO:-$(config_get fleet.hermes_repo "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1")}}"
PJANGLER_BIN="${PJANGLER_BIN:-$(config_get fleet.pjangler_bin "pj")}"
HERMES_RUNTIME_GIT_URL="${HERMES_RUNTIME_GIT_URL:-$(config_get fleet.hermes_git_url 'https://github.com/delorenj/hermes-agent.git')}"
HERMES_RUNTIME_GIT_REF="${HERMES_RUNTIME_GIT_REF:-$(config_get fleet.hermes_git_ref 'main')}"
HERMES_RUNTIME_GIT_SHA="${HERMES_RUNTIME_GIT_SHA:-$(config_get fleet.hermes_git_sha '0408fec7a153e6c32c064acd2b8053917f1525f1')}"
HERMES_OAUTH_FILE="${HERMES_OAUTH_FILE:-${HERMES_FLEET_OAUTH_FILE:-$(config_get fleet.oauth_file "$HOME/.hermes/auth.json")}}"
CODEX_HOME="${CODEX_HOME:-${HERMES_FLEET_CODEX_HOME:-$(config_get fleet.codex_home "$HOME/.codex")}}"
# Prefer a scaffold vendored into this agent directory; fall back to the configured template path.
RUNTIME_SCAFFOLD_DIR="${RUNTIME_SCAFFOLD_DIR:-$ROLE_DIR/.runtime-scaffold}"
if [[ ! -d "$RUNTIME_SCAFFOLD_DIR" ]]; then
  RUNTIME_SCAFFOLD_DIR="${HERMES_TEMPLATE_RUNTIME_SCAFFOLD:-$(config_get fleet.runtime_scaffold_dir "$HOME/code/hermes-agent-template/runtime-scaffold")}"
fi
REGISTRY_FILE="${REGISTRY_FILE:-${HERMES_FLEET_REGISTRY_FILE:-$(config_get fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}}"

# Cross-process serialization for fleet identity claims and registry updates.
# The lock file may remain on disk; flock ownership is kernel-scoped, so a
# crashed provisioner cannot leave a permanently stale lock.
FLEET_LOCK_FD=""
fleet_lock_acquire() {
  command -v flock >/dev/null 2>&1 || die "flock is required for safe fleet registry updates"
  local lock_file="${REGISTRY_FILE}.lock"
  mkdir -p "$(dirname "$lock_file")"
  [[ ! -L "$lock_file" ]] || die "refusing fleet lock symlink: $lock_file"
  exec {FLEET_LOCK_FD}>"$lock_file"
  chmod 600 "$lock_file"
  flock -w "${FLEET_LOCK_TIMEOUT_SECONDS:-30}" "$FLEET_LOCK_FD" \
    || die "timed out waiting for fleet registry lock: $lock_file"
}

fleet_lock_release() {
  if [[ "${FLEET_LOCK_FD:-}" =~ ^[0-9]+$ ]]; then
    flock -u "$FLEET_LOCK_FD" 2>/dev/null || true
    eval "exec ${FLEET_LOCK_FD}>&-"
  fi
  FLEET_LOCK_FD=""
}

# Bloodbank / NATS
BLOODBANK_NATS_HOST="${BLOODBANK_NATS_HOST:-$(config_get bloodbank.nats_host '127.0.0.1')}"
BLOODBANK_NATS_PORT="${BLOODBANK_NATS_PORT:-$(config_get bloodbank.nats_port '4222')}"
BLOODBANK_COMPOSE_DIR="${BLOODBANK_COMPOSE_DIR:-$(config_get bloodbank.compose_dir "$HOME/code/33GOD/bloodbank")}"

# Plane
PLANE_BASE="${PLANE_BASE:-$(config_get plane.base 'https://plane.delo.sh')}"
if [[ "$SKIP_PLANE" != "1" ]]; then
  PLANE_API_KEY="${PLANE_API_KEY:-${PLANE_33GOD_API_KEY:-}}"
fi

export FLEET_ENV HERMES_BIN HERMES_AGENT_REPO PJANGLER_BIN HERMES_RUNTIME_GIT_URL \
       HERMES_RUNTIME_GIT_REF HERMES_RUNTIME_GIT_SHA HERMES_OAUTH_FILE CODEX_HOME \
       RUNTIME_SCAFFOLD_DIR REGISTRY_FILE \
       BLOODBANK_NATS_HOST BLOODBANK_NATS_PORT BLOODBANK_COMPOSE_DIR \
       PLANE_BASE SKIP_PLANE
if [[ "$SKIP_PLANE" != "1" ]]; then
  export PLANE_API_KEY
fi

# systemd --user health check. Accept running/degraded/starting — only one
# broken unit shouldn't disqualify the rest of the user manager.
systemd_user_available() {
  command -v systemctl >/dev/null || return 1
  local state; state=$(systemctl --user is-system-running 2>&1)
  [[ "$state" =~ ^(running|degraded|starting|maintenance)$ ]]
}

# Query one systemd user-unit state without conflating every non-zero exit with
# an inactive/disabled unit. Output is `ok|<state>` only for documented
# state/exit-code pairs; D-Bus, manager, and arbitrary query failures become
# `error|<safe summary>` so retirement callers can fail closed.
systemctl_user_unit_state() {
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

# Resolve project repo path (the repo that holds agents/hermes/<role>/).
# Walk up from $ROLE_DIR until we find a git root that isn't us.
project_repo_path() {
  # Structured provisioners know the project root even before a fresh target
  # receives its own .git directory. Accept only a root that contains this
  # exact role path; otherwise fail closed instead of walking into a parent
  # checkout and mutating its manifest.
  if [[ -n "${PJANGLER_PROJECT_ROOT:-}" ]]; then
    local explicit role_real
    explicit="$(cd "$PJANGLER_PROJECT_ROOT" 2>/dev/null && pwd -P)" || return 1
    role_real="$(cd "$ROLE_DIR" 2>/dev/null && pwd -P)" || return 1
    case "$role_real" in
      "$explicit"/agents/hermes/*) printf '%s\n' "$explicit"; return 0 ;;
      *) return 1 ;;
    esac
  fi
  local d="$ROLE_DIR"
  [[ -d "$d/.git" || -f "$d/.git" ]] && { echo "$d"; return 0; }
  for _ in 1 2 3 4 5; do
    d="$(dirname "$d")"
    [[ -d "$d/.git" || -f "$d/.git" ]] && { echo "$d"; return 0; }
  done
  return 1
}
