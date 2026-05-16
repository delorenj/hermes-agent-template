#!/usr/bin/env bash
# Create the per-agent Hermes profile (clones from default ~/.hermes).
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 10-hermes-profile && { log "[10] profile already created — skipping"; exit 0; }

log "[10] creating hermes profile: $PROFILE_NAME"
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"

if [[ -d "$PROFILE_HOME" ]]; then
  log "    profile dir already exists; reusing"
else
  "$HERMES_BIN" profile create "$PROFILE_NAME" --clone-all --no-alias
fi

# Strip any inherited gateway state so this profile boots clean.
rm -f "$PROFILE_HOME/gateway.pid" "$PROFILE_HOME/gateway_state.json" \
      "$PROFILE_HOME/processes.json" 2>/dev/null || true

# Apply role-specific config overrides.
REPO_PATH="$(project_repo_path)" || die "couldn't locate project repo root"
log "    setting terminal.cwd = $REPO_PATH"
env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" config set terminal.cwd "$REPO_PATH"

# Install the project's SOUL.md into the profile so the agent loads it.
if [[ -f "$ROLE_DIR/SOUL.md" ]]; then
  cp "$ROLE_DIR/SOUL.md" "$PROFILE_HOME/SOUL.md"
  log "    installed SOUL.md into profile"
fi

mark_done 10-hermes-profile
