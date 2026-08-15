#!/usr/bin/env bash
# Install systemd --user units: profile gateway and fused heartbeat timer
# (board-reconciliation sentinel pass + gated runtime checkpoint, one tick).
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

if [[ "$ROLE" == "reporter" ]]; then
  log "[70] reporter systemd — gateway/consumer intentionally not installed"
  log "    use the reporter runtime's explicit install command after credential and policy preflight"
  mark_done 70-systemd
  exit 0
fi

RUNTIME="$ROLE_DIR/runtime"
# Singleton-runtime contract: units set HERMES_HOME to the agent's NAMED PROFILE
# dir, never the raw runtime path — Hermes derives profile identity and shared
# fleet auth from the unresolved HERMES_HOME. $RUNTIME stays correct for
# EnvironmentFile and logs: those are the repo-owned side the profile links to.
FLEET_HOME="${HERMES_FLEET_HOME:-$HOME/.hermes}"
PROFILE_HOME="$FLEET_HOME/profiles/${PROFILE_NAME:-$AGENT_ID}"
REPO_ROOT="$(project_repo_path)" || REPO_ROOT="$ROLE_DIR"
SYS_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYS_DIR" "$RUNTIME/logs"

# Upgrade remediation must run before honoring a legacy done marker. Older
# templates installed one per-profile Bloodbank consumer; leaving even one
# enabled races the fleet-shared durable gateway and can duplicate execution.
LEGACY_CONSUMER_UNIT="hermes-${AGENT_ID}-consumer.service"
LEGACY_CONSUMER_PATH="$SYS_DIR/$LEGACY_CONSUMER_UNIT"
legacy_consumer_present=0
[[ -e "$LEGACY_CONSUMER_PATH" || -L "$LEGACY_CONSUMER_PATH" ]] \
  && legacy_consumer_present=1
if command -v systemctl >/dev/null 2>&1; then
  legacy_active_result="$(systemctl_user_unit_state is-active "$LEGACY_CONSUMER_UNIT")"
  legacy_enabled_result="$(systemctl_user_unit_state is-enabled "$LEGACY_CONSUMER_UNIT")"
  [[ "$legacy_active_result" != error\|* ]] \
    || die "cannot safely query legacy consumer activity; preserving unit: ${legacy_active_result#*|}"
  [[ "$legacy_enabled_result" != error\|* ]] \
    || die "cannot safely query legacy consumer enablement; preserving unit: ${legacy_enabled_result#*|}"
  legacy_active_state="${legacy_active_result#*|}"
  legacy_enabled_state="${legacy_enabled_result#*|}"
  [[ "$legacy_active_state" == "not-found" && "$legacy_enabled_state" == "not-found" ]] \
    || legacy_consumer_present=1
elif [[ $legacy_consumer_present -eq 1 ]]; then
  die "systemctl is unavailable; cannot safely retire legacy consumer: $LEGACY_CONSUMER_UNIT"
fi
if [[ $legacy_consumer_present -eq 1 ]]; then
  systemctl --user disable --now "$LEGACY_CONSUMER_UNIT" >/dev/null 2>&1 \
    || die "legacy consumer disable failed; preserving unit: $LEGACY_CONSUMER_UNIT"
  legacy_active_result="$(systemctl_user_unit_state is-active "$LEGACY_CONSUMER_UNIT")"
  legacy_enabled_result="$(systemctl_user_unit_state is-enabled "$LEGACY_CONSUMER_UNIT")"
  [[ "$legacy_active_result" == "ok|inactive" ]] \
    || die "legacy consumer is not proven inactive; preserving unit: ${legacy_active_result#*|}"
  [[ "$legacy_enabled_result" == "ok|disabled" ]] \
    || die "legacy consumer is not proven disabled; preserving unit: ${legacy_enabled_result#*|}"
  rm -f -- "$LEGACY_CONSUMER_PATH"
  if systemd_user_available; then
    systemctl --user daemon-reload >/dev/null 2>&1 \
      || warn "    systemd daemon-reload failed after legacy consumer retirement"
  fi
  log "    retired legacy per-profile Bloodbank consumer: $LEGACY_CONSUMER_UNIT"
fi

already_done 70-systemd && { log "[70] systemd already installed — legacy cleanup checked"; exit 0; }
[[ "${SKIP_SYSTEMD:-0}" == "1" ]] && { log "[70] systemd — SKIPPED"; mark_done 70-systemd; exit 0; }

# The heartbeat runner (board-reconciliation sentinel pass + gated checkpoint)
# and the checkpoint helper both render into the role dir; just ensure they are
# executable. heartbeat.sh calls checkpoint.sh internally.
HEARTBEAT_BIN="$ROLE_DIR/.scripts/heartbeat.sh"
CREDENTIAL_LAUNCHER="$ROLE_DIR/.scripts/credential-launch.sh"
chmod +x "$HEARTBEAT_BIN" "$CREDENTIAL_LAUNCHER" "$ROLE_DIR/.scripts/checkpoint.sh" 2>/dev/null || true

[[ -d "$PROFILE_HOME" && ! -L "$PROFILE_HOME" ]] \
  || die "named profile is not a real directory; run: pj migrate hermes.runtime-singleton '$REPO_ROOT'"

# Encrypted credentials are optional and take precedence over the existing
# ignored runtime/.env fallback. Their plaintext appears only below /run while
# systemd executes the unit. Files are provisioned separately with
# `systemd-creds encrypt --user`; this step never reads or writes secret values.
CREDENTIAL_DIR="${HERMES_SYSTEMD_CREDENTIAL_DIR:-$HOME/.config/hermes-agent/credentials}"
TELEGRAM_CREDENTIAL="$CREDENTIAL_DIR/${AGENT_ID}-telegram-bot-token.cred"
MODEL_CREDENTIAL="$CREDENTIAL_DIR/${AGENT_ID}-model-api-key.cred"
GW_CREDENTIAL_LINES=""
HB_CREDENTIAL_LINES=""
if [[ -f "$TELEGRAM_CREDENTIAL" ]]; then
  command -v systemd-creds >/dev/null 2>&1 \
    || die "encrypted Telegram credential exists but systemd-creds is unavailable"
  GW_CREDENTIAL_LINES="LoadCredentialEncrypted=telegram_bot_token:$TELEGRAM_CREDENTIAL"
fi
if [[ -f "$MODEL_CREDENTIAL" ]]; then
  [[ -n "$(yaml_get model.key_env)" ]] \
    || die "encrypted model credential exists but model.key_env is blank in role.yaml"
  command -v systemd-creds >/dev/null 2>&1 \
    || die "encrypted model credential exists but systemd-creds is unavailable"
  GW_CREDENTIAL_LINES="${GW_CREDENTIAL_LINES}${GW_CREDENTIAL_LINES:+$'\n'}LoadCredentialEncrypted=model_api_key:$MODEL_CREDENTIAL"
  HB_CREDENTIAL_LINES="LoadCredentialEncrypted=model_api_key:$MODEL_CREDENTIAL"
fi

# Gateway unit
GW_UNIT="hermes-${AGENT_ID}-gateway.service"
cat > "$SYS_DIR/$GW_UNIT" <<UNIT
[Unit]
Description=Hermes Gateway — $DISPLAY_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HERMES_HOME=$PROFILE_HOME
Environment=HERMES_BIN=$HERMES_BIN
Environment=CODEX_HOME=$CODEX_HOME
EnvironmentFile=-$RUNTIME/.env
$GW_CREDENTIAL_LINES
ExecStart=$CREDENTIAL_LAUNCHER gateway
Restart=on-failure
RestartSec=10
StandardOutput=append:$RUNTIME/logs/gateway.systemd.log
StandardError=append:$RUNTIME/logs/gateway.systemd.log

[Install]
WantedBy=default.target
UNIT

# Fused heartbeat: board-reconciliation sentinel pass + gated runtime checkpoint.
# Frequent ticks (1 min); heartbeat.sh's own cooldown/lock logic rate-limits the
# full Hermes pass, and the checkpoint is gated to ~hourly inside the runner.
# The per-agent EnvironmentFiles load ticket-provider creds for the sentinel pass.
HB_SVC="hermes-${AGENT_ID}-heartbeat.service"
HB_TIMER="hermes-${AGENT_ID}-heartbeat.timer"
cat > "$SYS_DIR/$HB_SVC" <<UNIT
[Unit]
Description=Hermes Heartbeat (reconcile + checkpoint) — $DISPLAY_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
Environment=HERMES_HOME=$PROFILE_HOME
Environment=HERMES_BIN=$HERMES_BIN
Environment=CODEX_HOME=$CODEX_HOME
EnvironmentFile=-%h/.config/hermes-agent/env
EnvironmentFile=-%h/.hermes/env
EnvironmentFile=-%h/.hermes/hermes-agent.env
EnvironmentFile=-%h/.hermes/${AGENT_ID}.env
EnvironmentFile=-$RUNTIME/.env
$HB_CREDENTIAL_LINES
ExecStart=$CREDENTIAL_LAUNCHER heartbeat
TimeoutStartSec=45min
StandardOutput=append:$RUNTIME/logs/heartbeat.log
StandardError=append:$RUNTIME/logs/heartbeat.log
UNIT
cat > "$SYS_DIR/$HB_TIMER" <<UNIT
[Unit]
Description=Heartbeat (reconcile + checkpoint) for $AGENT_ID

[Timer]
OnBootSec=1min
OnUnitInactiveSec=1min
Unit=$HB_SVC
Persistent=true

[Install]
WantedBy=timers.target
UNIT

if systemd_user_available; then
  systemctl --user daemon-reload
  # `enable --now` both enables (persist across login) AND starts the unit now, so a
  # freshly provisioned agent comes up live instead of dormant. Units with missing
  # creds (e.g. a gateway with no Telegram token yet) fail softly via Restart=on-failure.
  for u in "$GW_UNIT" "$HB_TIMER"; do
    systemctl --user enable --now "$u" >/dev/null 2>&1 && log "    enabled + started: $u" || warn "    failed to enable/start: $u"
  done
else
  warn "    systemd --user not available; units installed at $SYS_DIR but not enabled"
fi

mark_done 70-systemd
