#!/usr/bin/env bash
# Append this agent's entry to the global fleet registry.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

mkdir -p "$(dirname "$REGISTRY_FILE")"
if [[ ! -f "$REGISTRY_FILE" ]]; then
  cat > "$REGISTRY_FILE" <<'YAML'
# Hermes agent fleet registry.
# One entry per provisioned agent. Managed by hermes-agent-template/.scripts/80-registry.sh.
schema_version: 1
agents: {}
YAML
fi

PROJECT_PATH="$(project_repo_path)" || PROJECT_PATH=""
PLANE_PROJECT_ID="$(cat "$ROLE_DIR/.scripts/.plane-project-id" 2>/dev/null || true)"

log "[80] appending to fleet registry: $REGISTRY_FILE"

python3 - "$REGISTRY_FILE" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" \
  "$PROJECT_PATH" "$ROLE_DIR" "$PROFILE_NAME" \
  "$(yaml_get telegram.provisioning_status)" "$BOT_HANDLE" "$(yaml_get telegram.bot_id)" \
  "$(yaml_get slack.provisioning_status)" "$(yaml_get slack.team_id)" \
  "$(yaml_get slack.team_name)" "$(yaml_get slack.bot_user_id)" \
  "$(yaml_get slack.bot_id)" "$(yaml_get slack.bot_username)" \
  "$(yaml_get bloodbank.gateway_scope)" "$(yaml_get bloodbank.target_agent_id)" \
  "$PLANE_WORKSPACE" "$PLANE_PROJECT_ID" "$(yaml_get plane.identifier)" \
  "$RUNTIME_REPO" "$HERMES_BIN" "$HERMES_AGENT_REPO" "$FLEET_ENV" \
  "hermes-${AGENT_ID}-gateway.service" "hermes-${AGENT_ID}-heartbeat.timer" <<'PYEOF'
import sys, pathlib, datetime
try:
    import yaml  # type: ignore
except ImportError:
    sys.exit("PyYAML required; pip install pyyaml")
(path, agent_id, repo, role, display, project, role_dir, profile,
 telegram_status, bot, telegram_bot_id,
 slack_status, slack_team_id, slack_team_name, slack_user_id, slack_bot_id,
 slack_username, bloodbank_scope, bloodbank_target, plane_ws, plane_id,
 plane_ident, runtime_repo, hermes_bin, hermes_repo, fleet_env, gw,
 heartbeat) = sys.argv[1:29]
p = pathlib.Path(path)
data = yaml.safe_load(p.read_text()) or {"schema_version": 1, "agents": {}}
data.setdefault("agents", {})[agent_id] = {
  "repo": repo, "role": role, "display_name": display,
  "project_path": project, "role_dir": role_dir,
  "profile_name": profile,
  "telegram": {
    "provisioning_status": telegram_status,
    "bot_username": bot,
    "bot_id": telegram_bot_id,
  },
  "slack": {
    "provisioning_status": slack_status,
    "team_id": slack_team_id,
    "team_name": slack_team_name,
    "bot_user_id": slack_user_id,
    "bot_id": slack_bot_id,
    "bot_username": slack_username,
  },
  "bloodbank": {
    "gateway_scope": bloodbank_scope,
    "target_agent_id": bloodbank_target,
  },
  "plane": {"workspace": plane_ws, "project_id": plane_id, "identifier": plane_ident},
  "runtime_repo": runtime_repo,
  "hermes": {"bin": hermes_bin, "repo": hermes_repo, "fleet_env": fleet_env},
  "systemd": {"gateway_unit": gw, "heartbeat_timer": heartbeat},
  "provisioned_at": datetime.datetime.utcnow().isoformat() + "Z",
}
p.write_text(yaml.safe_dump(data, sort_keys=False))
PYEOF

mark_done 80-registry
