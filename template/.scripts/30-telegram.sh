#!/usr/bin/env bash
# Capture a BotFather token and wire it into the runtime profile.
#
# Telegram credentials are invocation-only inputs. Capture them before _lib.sh
# sources fleet.env so a token parked in shared fleet state can never provision
# or be silently reused by a profile. The non-secret allow-list may be shared.
INVOCATION_TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN-}"
INVOCATION_TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS-}"

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

TELEGRAM_BOT_TOKEN="$INVOCATION_TELEGRAM_BOT_TOKEN"
if [[ -n "$INVOCATION_TELEGRAM_ALLOWED_USERS" ]]; then
  TELEGRAM_ALLOWED_USERS="$INVOCATION_TELEGRAM_ALLOWED_USERS"
else
  TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS-}"
fi
unset INVOCATION_TELEGRAM_BOT_TOKEN INVOCATION_TELEGRAM_ALLOWED_USERS

telegram_yaml_update() {
  python3 - "$ROLE_YAML" "$@" <<'PYEOF'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
updates = dict(zip(sys.argv[2::2], sys.argv[3::2]))
text = path.read_text(encoding="utf-8")
match = re.search(r"(?ms)^telegram:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", text)
if not match:
    raise SystemExit(f"telegram metadata block missing from {path}")
body = match.group("body")
for key, value in updates.items():
    replacement = f"  {key}: {json.dumps(value)}"
    body, count = re.subn(
        rf"(?m)^\s+{re.escape(key)}:\s*.*$", lambda _: replacement, body, count=1
    )
    if count == 0:
        if body and not body.endswith("\n"):
            body += "\n"
        body += replacement + "\n"
path.write_text(text[: match.start("body")] + body + text[match.end("body") :], encoding="utf-8")
PYEOF
}

already_done 30-telegram && { log "[30] telegram already wired — skipping"; exit 0; }

if [[ "${SKIP_TELEGRAM:-0}" == "1" ]]; then
  telegram_yaml_update provisioning_status disabled
  log "[30] telegram — SKIPPED (SKIP_TELEGRAM=1)"
  mark_done 30-telegram
  exit 0
fi

RUNTIME="$ROLE_DIR/runtime"
ENVF="$RUNTIME/.env"
mkdir -p "$RUNTIME"
[[ ! -L "$ENVF" ]] || die "refusing to write Telegram credentials through symlink: $ENVF"

cat >&2 <<EOF

╭─ BotFather steps for @$BOT_HANDLE ─────────────────────────────────────╮
│ 1. Open Telegram, message @BotFather                                   │
│ 2. /newbot                                                             │
│ 3. Display name:   $DISPLAY_NAME                                       │
│ 4. Username:       $BOT_HANDLE  (must end in _bot)                     │
│ 5. Copy the HTTP API token from the reply.                             │
│ 6. /setjoingroups → Disable                                            │
│ 7. /setprivacy    → Disable                                            │
╰────────────────────────────────────────────────────────────────────────╯

EOF

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" && -t 0 ]]; then
  read -r -s -p "Paste the bot token for @$BOT_HANDLE (or empty to skip): " TELEGRAM_BOT_TOKEN
  echo >&2
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  telegram_yaml_update provisioning_status deferred
  warn "    no token provided; Telegram step deferred"
  warn "    re-run later with a profile-dedicated TELEGRAM_BOT_TOKEN invocation"
  exit 0
fi

# Sanity check
if [[ ! "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:.+ ]]; then
  die "token doesn't look like a Telegram bot token"
fi
log "    verifying token..."
info=$(curl -fsS "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe") \
  || die "Telegram getMe request failed"
identity=$(printf '%s' "$info" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit("Telegram getMe returned an invalid response")
result = data.get("result") or {}
if not data.get("ok") or not result.get("id") or not result.get("username"):
    raise SystemExit("Telegram rejected the bot token or omitted identity fields")
print("{}\t{}".format(
    result.get("id"), str(result.get("username")).replace(chr(9), " ").replace(chr(10), " ")
))
') || die "Telegram bot identity verification failed"
IFS=$'\t' read -r bot_id bot_username <<< "$identity"
log "    verified: @$bot_username (id=$bot_id)"

# Reject token reuse, token rotation onto an identity owned by another agent,
# and credentials parked in shared fleet/root env files. Values are compared
# in-process and never included in output or the fleet registry.
export TELEGRAM_BOT_TOKEN
python3 - "$REGISTRY_FILE" "$FLEET_ENV" "$ENVF" "$AGENT_ID" "$bot_id" <<'PYEOF'
import os
import pathlib
import re
import sys

registry_path, fleet_path, target_path, agent_id, bot_id = sys.argv[1:]
token = os.environ["TELEGRAM_BOT_TOKEN"]
target = pathlib.Path(target_path).resolve(strict=False)

def env_token(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(
        r"(?m)^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*(.*)$", text
    )
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value

owners = [("shared fleet environment", pathlib.Path(fleet_path))]
registry = pathlib.Path(registry_path)
if registry.is_file():
    try:
        import yaml  # type: ignore
        agents = (yaml.safe_load(registry.read_text(encoding="utf-8")) or {}).get("agents", {})
    except Exception as exc:
        raise SystemExit(f"cannot safely inspect Telegram ownership registry: {type(exc).__name__}")
    for other_id, entry in agents.items():
        if other_id == agent_id or not isinstance(entry, dict):
            continue
        telegram = entry.get("telegram") or {}
        if isinstance(telegram, dict) and str(telegram.get("bot_id") or "") == bot_id:
            raise SystemExit(f"Telegram bot identity is already assigned to agent {other_id}")
        role_dir = entry.get("role_dir")
        if role_dir:
            owners.append((f"agent {other_id}", pathlib.Path(str(role_dir)) / "runtime" / ".env"))

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
    if env_token(path) == token:
        raise SystemExit(f"Telegram bot token is already assigned to {owner}")
PYEOF

# Resolve operator's allowed user id
if [[ -z "${TELEGRAM_ALLOWED_USERS:-}" && -f "$HOME/.hermes/.env" ]]; then
  TELEGRAM_ALLOWED_USERS=$(grep -E '^[[:space:]]*#?[[:space:]]*TELEGRAM_ALLOWED_USERS=' "$HOME/.hermes/.env" \
    | tail -1 | sed -E 's/^[[:space:]]*#?[[:space:]]*TELEGRAM_ALLOWED_USERS=//; s/^"//; s/"$//')
fi
if [[ -z "${TELEGRAM_ALLOWED_USERS:-}" && -t 0 ]]; then
  read -r -p "Your Telegram user id (allow-list for this bot): " TELEGRAM_ALLOWED_USERS
fi
if [[ -z "${TELEGRAM_ALLOWED_USERS:-}" ]]; then
  warn "    Telegram is wired but denies all inbound users until TELEGRAM_ALLOWED_USERS is set"
fi

# Atomically replace only Telegram fields in the profile-local runtime file.
export TELEGRAM_ALLOWED_USERS
python3 - "$ENVF" <<'PYEOF'
import json
import os
import pathlib
import re
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit(f"refusing to write Telegram credentials through symlink: {path}")
path.parent.mkdir(parents=True, exist_ok=True)
text = path.read_text(encoding="utf-8") if path.exists() else ""
for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"):
    text = re.sub(rf"(?m)^\s*(?:export\s+)?#?\s*{key}\s*=.*(?:\n|$)", "", text)
text = text.rstrip("\n")
if text:
    text += "\n"
text += "".join(
    f"{key}={json.dumps(os.environ.get(key, ''))}\n"
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS")
)
fd, temporary = tempfile.mkstemp(prefix=".env.telegram-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF
unset TELEGRAM_BOT_TOKEN

# Record the verified identity, never the token.
telegram_yaml_update \
  provisioning_status verified \
  bot_username "$bot_username" \
  bot_id "$bot_id"

# Enable telegram toolset for the profile
env HERMES_HOME="$RUNTIME" "$HERMES_BIN" tools enable telegram hermes-telegram 2>/dev/null \
  || warn "    couldn't auto-enable telegram toolset; run: $ROLE_DIR/hermes tools"

log "    wired @$bot_username (id=$bot_id)"
mark_done 30-telegram
