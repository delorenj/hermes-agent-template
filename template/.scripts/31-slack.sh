#!/usr/bin/env bash
# Opt-in, profile-local Slack Socket Mode provisioning.
#
# Slack credentials are invocation-only inputs.  Capture them before _lib.sh
# sources fleet.env so an accidentally shared fleet token can never provision a
# profile.  The non-secret allowed-user policy may still come from fleet.env.
INVOCATION_SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN-}"
INVOCATION_SLACK_APP_TOKEN="${SLACK_APP_TOKEN-}"
INVOCATION_SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS-}"
INVOCATION_ENABLE_SLACK="${ENABLE_SLACK-${WIRE_SLACK-}}"

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

SLACK_BOT_TOKEN="$INVOCATION_SLACK_BOT_TOKEN"
SLACK_APP_TOKEN="$INVOCATION_SLACK_APP_TOKEN"
if [[ -n "$INVOCATION_SLACK_ALLOWED_USERS" ]]; then
  SLACK_ALLOWED_USERS="$INVOCATION_SLACK_ALLOWED_USERS"
else
  SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS-}"
fi
ENABLE_SLACK="$INVOCATION_ENABLE_SLACK"
unset INVOCATION_SLACK_BOT_TOKEN INVOCATION_SLACK_APP_TOKEN

slack_yaml_update() {
  python3 - "$ROLE_YAML" "$@" <<'PYEOF'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
updates = dict(zip(sys.argv[2::2], sys.argv[3::2]))
text = path.read_text(encoding="utf-8")
match = re.search(r"(?ms)^slack:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", text)
if not match:
    raise SystemExit(f"slack metadata block missing from {path}")
body = match.group("body")
for key, value in updates.items():
    replacement = f"  {key}: {json.dumps(value)}"
    body, count = re.subn(
        rf"(?m)^\s+{re.escape(key)}:\s*.*$", lambda _: replacement, body, count=1
    )
    if count != 1:
        raise SystemExit(f"Slack metadata key {key!r} missing from {path}")
path.write_text(text[: match.start("body")] + body + text[match.end("body") :], encoding="utf-8")
PYEOF
}

PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
slack_status="$(yaml_get slack.provisioning_status)"
if [[ "$slack_status" != "verified" ]]; then
  profile_channel_enabled_set "$PROFILE_HOME" slack false \
    || die "Slack could not be disabled in the profile override"
fi

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# Preserve and reconcile durable wiring across transient 1Password outages.
# An explicit token remains an intentional rotation request and bypasses this
# adoption path.
if [[ -z "${SLACK_BOT_TOKEN:-}" && -z "${SLACK_APP_TOKEN:-}" \
      && "$slack_status" == "verified" ]] \
   && profile_onepassword_ref_exists "$PROFILE_HOME" SLACK_BOT_TOKEN \
   && profile_onepassword_ref_exists "$PROFILE_HOME" SLACK_APP_TOKEN; then
  slack_bot_reference_rc=0
  slack_app_reference_rc=0
  profile_onepassword_ref_validate "$PROFILE_HOME" SLACK_BOT_TOKEN \
    || slack_bot_reference_rc=$?
  profile_onepassword_ref_validate "$PROFILE_HOME" SLACK_APP_TOKEN \
    || slack_app_reference_rc=$?
  if [[ $slack_bot_reference_rc -eq 0 && $slack_app_reference_rc -eq 0 ]]; then
    profile_channel_enabled_set "$PROFILE_HOME" slack true \
      || die "Slack could not be enabled in the profile override"
    mark_done 31-slack
    log "[31] slack — existing verified 1Password wiring reconciled"
    exit 0
  fi
  if [[ $slack_bot_reference_rc -eq 75 || $slack_app_reference_rc -eq 75 ]]; then
    die "Slack 1Password reference validation is temporarily unavailable; preserved existing verified wiring for retry"
  fi
fi

if [[ "${SKIP_SLACK:-0}" == "1" ]]; then
  profile_channel_enabled_set "$PROFILE_HOME" slack false \
    || die "Slack could not be disabled in the profile override"
  slack_yaml_update provisioning_status deferred
  clear_done 31-slack
  log "[31] slack — DEFERRED (SKIP_SLACK=1; no verified 1Password reference pair)"
  exit 0
fi

if already_done 31-slack; then
  clear_done 31-slack
  log "[31] stale Slack completion marker cleared — reconciling credentials"
fi

have_bot=0
have_app=0
[[ -n "$SLACK_BOT_TOKEN" ]] && have_bot=1
[[ -n "$SLACK_APP_TOKEN" ]] && have_app=1

if ! truthy "${ENABLE_SLACK:-0}" && (( ! have_bot && ! have_app )); then
  profile_channel_enabled_set "$PROFILE_HOME" slack false \
    || die "Slack could not be disabled in the profile override"
  slack_yaml_update provisioning_status deferred
  log "[31] slack — deferred (opt in with ENABLE_SLACK=1 or supply both Slack tokens)"
  exit 0
fi

if (( have_bot != have_app )) && ! truthy "${ENABLE_SLACK:-0}"; then
  die "Slack provisioning requires a dedicated SLACK_BOT_TOKEN and SLACK_APP_TOKEN pair"
fi

if truthy "${ENABLE_SLACK:-0}" && [[ -t 0 ]]; then
  if [[ -z "$SLACK_BOT_TOKEN" ]]; then
    read -r -s -p "Slack Bot User OAuth Token (xoxb-...): " SLACK_BOT_TOKEN
    echo >&2
  fi
  if [[ -z "$SLACK_APP_TOKEN" ]]; then
    read -r -s -p "Slack App-Level Socket Mode Token (xapp-...): " SLACK_APP_TOKEN
    echo >&2
  fi
fi

[[ -n "$SLACK_BOT_TOKEN" && -n "$SLACK_APP_TOKEN" ]] \
  || die "Slack provisioning requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN"
[[ "$SLACK_BOT_TOKEN" =~ ^xoxb-[A-Za-z0-9-]+$ ]] || die "SLACK_BOT_TOKEN must be a Bot User OAuth token (xoxb-...)"
[[ "$SLACK_APP_TOKEN" =~ ^xapp-[A-Za-z0-9-]+$ ]] || die "SLACK_APP_TOKEN must be an App-Level Socket Mode token (xapp-...)"

SLACK_ALLOWED_USERS="${SLACK_ALLOWED_USERS//[[:space:]]/}"
if [[ -n "$SLACK_ALLOWED_USERS" && "$SLACK_ALLOWED_USERS" != "*" \
      && ! "$SLACK_ALLOWED_USERS" =~ ^[UW][A-Z0-9]+(,[UW][A-Z0-9]+)*$ ]]; then
  die "SLACK_ALLOWED_USERS must be '*' or comma-separated Slack member IDs"
fi

RUNTIME="$ROLE_DIR/runtime"
ENVF="$RUNTIME/.env"
mkdir -p "$RUNTIME"
[[ ! -L "$ENVF" ]] || die "refusing to write Slack credentials through symlink: $ENVF"

log "[31] verifying Slack bot identity via auth.test"
auth_response="$({
  printf '%s\n' 'url = "https://slack.com/api/auth.test"'
  printf 'header = "Authorization: Bearer %s"\n' "$SLACK_BOT_TOKEN"
  printf '%s\n' 'header = "Content-Type: application/x-www-form-urlencoded"'
  printf '%s\n' 'request = "POST"' 'fail' 'silent' 'show-error'
} | curl --config -)" \
  || die "Slack auth.test request failed"

identity=$(printf '%s' "$auth_response" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit("Slack auth.test returned an invalid response")
if not data.get("ok"):
    error = str(data.get("error") or "unknown_error")
    safe = "".join(c for c in error if c.isalnum() or c in "_-.")[:80]
    raise SystemExit("Slack auth.test rejected the bot token ({})".format(safe or "unknown_error"))
values = [data.get(k, "") for k in ("team_id", "team", "user_id", "bot_id", "user")]
if not values[0] or not values[2]:
    raise SystemExit("Slack auth.test response omitted required identity fields")
print("\t".join(str(v).replace("\t", " ").replace("\n", " ") for v in values))
') || die "Slack bot identity verification failed"
IFS=$'\t' read -r slack_team_id slack_team_name slack_bot_user_id slack_bot_id slack_bot_username <<< "$identity"

# Socket Mode requires a separately authenticated app-level token. A valid bot
# token says nothing about the xapp credential, so prove that credential can
# open a connection before claiming ownership or persisting either secret. The
# token and Slack's returned WebSocket URL stay on anonymous pipes: neither is
# exposed through curl argv, child environments, logs, or durable files.
log "[31] verifying Slack Socket Mode app token via apps.connections.open"
if ! {
  printf '%s\n' 'url = "https://slack.com/api/apps.connections.open"'
  printf 'header = "Authorization: Bearer %s"\n' "$SLACK_APP_TOKEN"
  printf '%s\n' 'header = "Content-Type: application/x-www-form-urlencoded"'
  printf '%s\n' 'request = "POST"' 'fail' 'silent' 'show-error'
} | curl --config - | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit("Slack apps.connections.open returned an invalid response")
if not isinstance(data, dict) or not data.get("ok"):
    error = str(data.get("error") if isinstance(data, dict) else "unknown_error")
    safe = "".join(c for c in error if c.isalnum() or c in "_-." )[:80]
    raise SystemExit("Slack apps.connections.open rejected the app token ({})".format(safe or "unknown_error"))
url = data.get("url")
if not isinstance(url, str) or not url.startswith("wss://"):
    raise SystemExit("Slack apps.connections.open response omitted a Socket Mode URL")
'; then
  die "Slack Socket Mode app-token verification failed"
fi

# Reject credential reuse, token rotation onto an identity owned by another
# agent, and credentials parked in shared env files. The scan, durable identity
# claim, and profile credential write share one fleet-wide flock.
fleet_lock_acquire
trap 'fleet_lock_release' EXIT
python3 /dev/fd/3 "$REGISTRY_FILE" "$FLEET_ENV" "$ENVF" "$AGENT_ID" \
  "$slack_team_id" "$slack_bot_user_id" "$slack_bot_id" \
  "$ROLE_DIR" "$PROFILE_NAME" "$slack_team_name" "$slack_bot_username" \
  3<<'PYEOF' <<<"${SLACK_BOT_TOKEN}"$'\n'"${SLACK_APP_TOKEN}"
import errno
import os
import pathlib
import re
import sys
import tempfile
try:
    import yaml  # type: ignore
except ImportError:
    raise SystemExit("PyYAML is required for Slack fleet claims")

(
    registry_path,
    fleet_path,
    target_path,
    agent_id,
    team_id,
    user_id,
    bot_id,
    role_dir,
    profile_name,
    team_name,
    bot_username,
) = sys.argv[1:]
credential_lines = sys.stdin.read().splitlines()
if len(credential_lines) != 2 or not all(credential_lines):
    raise SystemExit("Slack ownership scan received an invalid credential pair")
bot_token, app_token = credential_lines
target = pathlib.Path(target_path).resolve(strict=False)

def env_values(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    values = {}
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        match = re.search(rf"(?m)^\s*(?:export\s+)?{key}\s*=\s*(.*)$", text)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values

owners = [("shared fleet environment", pathlib.Path(fleet_path))]
registry = pathlib.Path(registry_path)
if registry.is_symlink():
    raise SystemExit(f"refusing to update registry symlink: {registry}")
data = {"schema_version": 1, "agents": {}}
if registry.is_file():
    try:
        data = yaml.safe_load(registry.read_text(encoding="utf-8")) or data
    except Exception as exc:
        raise SystemExit(f"cannot safely inspect Slack ownership registry: {type(exc).__name__}")
if not isinstance(data, dict) or not isinstance(data.get("agents", {}), dict):
    raise SystemExit("cannot safely inspect Slack ownership registry: invalid agents mapping")
agents = data.setdefault("agents", {})
for other_id, entry in agents.items():
    if other_id == agent_id or not isinstance(entry, dict):
        continue
    slack = entry.get("slack") or {}
    if isinstance(slack, dict):
        same_user = user_id and slack.get("bot_user_id") == user_id
        same_bot = bot_id and slack.get("bot_id") == bot_id
        same_team_user = team_id and same_user and slack.get("team_id") == team_id
        if same_bot or same_team_user:
            raise SystemExit(f"Slack bot identity is already assigned to agent {other_id}")
    other_role_dir = entry.get("role_dir")
    if other_role_dir:
        owners.append((f"agent {other_id}", pathlib.Path(str(other_role_dir)) / "runtime" / ".env"))

home_value = os.environ.get("HOME", "")
if home_value:
    home = pathlib.Path(home_value)
    owners.append(("shared Hermes root", home / ".hermes" / ".env"))
    profiles = home / ".hermes" / "profiles"
    if profiles.is_dir():
        for profile in profiles.iterdir():
            owners.append((f"profile {profile.name}", profile / ".env"))

seen = set()
for owner, path in owners:
    resolved = path.resolve(strict=False)
    if resolved == target or resolved in seen:
        continue
    seen.add(resolved)
    values = env_values(path)
    if values.get("SLACK_BOT_TOKEN") == bot_token:
        raise SystemExit(f"Slack bot token is already assigned to {owner}")
    if values.get("SLACK_APP_TOKEN") == app_token:
        raise SystemExit(f"Slack app token is already assigned to {owner}")

claim = agents.setdefault(agent_id, {})
if not isinstance(claim, dict):
    raise SystemExit(f"registry entry for {agent_id} is not a mapping")
claim["role_dir"] = role_dir
claim["profile_name"] = profile_name
claim["slack"] = {
    "provisioning_status": "verified",
    "team_id": team_id,
    "team_name": team_name,
    "bot_user_id": user_id,
    "bot_id": bot_id,
    "bot_username": bot_username,
}
registry.parent.mkdir(parents=True, exist_ok=True)
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

fd, temporary = tempfile.mkstemp(prefix=f".{registry.name}.slack-", dir=registry.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, registry)
    os.chmod(registry, 0o600)
    fsync_parent(registry)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF

# Persist both credentials directly to 1Password, then map only op://
# references into the named profile's override config.
ONEPASSWORD_ITEM_PREFIX="${HERMES_ONEPASSWORD_ITEM_PREFIX:-$(config_get fleet.onepassword_item_prefix 'hermes-agent')}"
slack_bot_reference="$(store_onepassword_secret \
  "${ONEPASSWORD_ITEM_PREFIX}-${AGENT_ID}-slack-bot-token" \
  "$SLACK_BOT_TOKEN")" \
  || die "Slack bot credential could not be stored in 1Password"
slack_app_reference="$(store_onepassword_secret \
  "${ONEPASSWORD_ITEM_PREFIX}-${AGENT_ID}-slack-app-token" \
  "$SLACK_APP_TOKEN")" \
  || die "Slack app credential could not be stored in 1Password"
profile_onepassword_ref_set "$PROFILE_HOME" SLACK_BOT_TOKEN "$slack_bot_reference" \
  || die "Slack bot 1Password reference could not be mapped into the named profile"
profile_onepassword_ref_set "$PROFILE_HOME" SLACK_APP_TOKEN "$slack_app_reference" \
  || die "Slack app 1Password reference could not be mapped into the named profile"
profile_channel_enabled_set "$PROFILE_HOME" slack true \
  || die "Slack could not be enabled in the profile override"

# Keep only the non-secret allow-list in runtime/.env and scrub literals left
# by any older template.
export SLACK_ALLOWED_USERS
python3 - "$ENVF" <<'PYEOF'
import errno
import json
import os
import pathlib
import re
import tempfile

path = pathlib.Path(__import__("sys").argv[1])
if path.is_symlink():
    raise SystemExit(f"refusing to write Slack credentials through symlink: {path}")
path.parent.mkdir(parents=True, exist_ok=True)
text = path.read_text(encoding="utf-8") if path.exists() else ""
for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS"):
    text = re.sub(rf"(?m)^\s*(?:export\s+)?#?\s*{key}\s*=.*(?:\n|$)", "", text)
text = text.rstrip("\n")
if text:
    text += "\n"
text += f"SLACK_ALLOWED_USERS={json.dumps(os.environ.get('SLACK_ALLOWED_USERS', ''))}\n"

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

fd, temporary = tempfile.mkstemp(prefix=".env.slack-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
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
unset SLACK_BOT_TOKEN SLACK_APP_TOKEN

slack_yaml_update \
  provisioning_status verified \
  team_id "$slack_team_id" \
  team_name "$slack_team_name" \
  bot_user_id "$slack_bot_user_id" \
  bot_id "$slack_bot_id" \
  bot_username "$slack_bot_username"

fleet_lock_release
trap - EXIT

if [[ -z "$SLACK_ALLOWED_USERS" ]]; then
  warn "    Slack is wired but denies all inbound users until SLACK_ALLOWED_USERS is set"
fi
log "    verified Slack bot $slack_bot_username in $slack_team_name (profile-local credentials)"
mark_done 31-slack
