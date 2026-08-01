# Operations

Running the hermes fleet day-to-day: provisioning, starting, stopping, retiring,
debugging.

## Provision a new agent

```bash
cd /path/to/the/project-repo            # MUST be inside a git repo
copier copy gh:delorenj/hermes-agent-template ./agents/hermes/<role> \
  --data target_repo=<repo-name>        # REQUIRED — must match the parent repo name
  --data role=<role>                    # pm | dev | review | ops | qa | ci | ...
  --data agent_purpose="<one-liner>"
  --data soul_tone=direct               # direct | playful | formal | terse
  --trust
```

`target_repo` MUST be supplied explicitly. The template no longer derives it
from `dst_path` (path-based regex was unreliable across relative vs absolute
invocations).

### What runs

| Step | What happens | Skippable via |
| --- | --- | --- |
| 00 banner | Print identity | n/a |
| 01 config | Seed `~/.config/hermes-agent-template/config.toml` from the shipped example if absent (see [Configuration](#configuration)) | n/a |
| 05 fleet env | Ensure `~/.hermes/fleet.env` exists (shared Hermes binary/repo/registry source-of-truth), populated from `config.toml` | n/a |
| 10 hermes profile | `hermes profile create <repo>-<role> --clone --no-alias` + mirror skills/plugins/hooks from default + symlink canonical runtime skills (`delonet-conventions`, `delonet-dotenv`, `hermes-pm-template-maintenance`, `hindsight`, `subagent-driven-development`) from `/home/delorenj/.agents/skills`; PM roles also seed `VOX_URL` in profile `.env` | n/a |
| 20 runtime repo | Create gh:delorenj/agent-hm-<repo>-<role> (private), push scaffold from role-local `.runtime-scaffold/`, submodule-add into ./runtime/, symlink ~/.hermes/profiles/<id> → runtime; PM roles also link the Voxxy plugin and set `tts.provider: voxxy` | `SKIP_RUNTIME_REPO=1` |
| 30 telegram | Verify an invocation-supplied, profile-dedicated BotFather token; reject fleet reuse; write only to runtime/.env | `SKIP_TELEGRAM=1` |
| 31 slack | Disabled/deferred by default; verify a dedicated app+bot pair with `auth.test` and write it only to runtime/.env when explicitly enabled | `SKIP_SLACK=1` |
| 40 plane | Create Plane project in 33god workspace (1:1 with agent), patch identifier into role.yaml | `SKIP_PLANE=1` |
| 60 bloodbank | Compatibility checkpoint for fleet-shared routing; installs no files, dependencies, or services | `SKIP_BLOODBANK=1` remains a no-op |
| 70 systemd | Install user units: profile gateway and heartbeat timer (board-reconciliation sentinel pass + gated runtime checkpoint, one tick) | `SKIP_SYSTEMD=1` |
| 80 registry | Append entry to ~/.hermes/agents-registry.yaml | n/a |
| 99 summary | Print summary | n/a |

Every step is idempotent — re-running the entire provisioning is safe. Each
step writes a `.done-NN-*` marker; delete that marker to force a re-run.

## Configuration

Environment-specific defaults are NOT hardcoded — they live in
`~/.config/hermes-agent-template/config.toml` (override the path with
`$HERMES_TEMPLATE_CONFIG`). Step `01 config` seeds it from the shipped
`config.example.toml` on first run. Every provisioning script and the generated
`hermes` launcher read it.

Resolution precedence for each value: **explicit env var → `~/.hermes/fleet.env`
→ `config.toml` → built-in fallback**. To retarget the whole template for a
different machine/user, edit this one file:

```toml
[fleet]
hermes_bin = "/path/to/hermes-agent/.venv/bin/hermes"
hermes_repo = "/path/to/hermes-agent"
oauth_file = "~/.hermes/auth.json"
codex_home = "~/.codex"
canonical_skills_dir = "/path/to/.agents/skills"
voxxy_plugin_dir = "~/code/voxxy/plugins/tts/voxxy"
vox_url = "https://vox.delo.sh"

[github]
runtime_repo_owner = "your-gh-owner"

[plane]
base = "https://plane.example.com"
workspace = "your-workspace"
```

`role.yaml` stores an empty `runtime.github_owner` and `plane.workspace` for
freshly provisioned agents; the shell layer fills them from `config.toml` at
runtime (older manifests that baked `owner/name` into `runtime.github_repo`
still work unchanged).

## Fleet source-of-truth

`~/.hermes/fleet.env` is the single shared pointer every generated launcher reads:

- `HERMES_FLEET_BIN` (the exact Hermes executable all agents use)
- `HERMES_FLEET_REPO` (the upstream/fork checkout you keep on the edge)
- `HERMES_FLEET_REGISTRY_FILE` (defaults to `~/.hermes/agents-registry.yaml`)
- `HERMES_FLEET_OAUTH_FILE` (shared Hermes provider OAuth store)
- `HERMES_FLEET_CODEX_HOME` (shared Codex CLI/app-server auth/config home)

If you `git pull`/sync the repo at `HERMES_FLEET_REPO` and rebuild/update that
same binary path, every wrapper benefits immediately with no per-agent edits.
For Codex auth, run `hermes auth add openai-codex` through any generated agent
launcher once; all agents using the same fleet env read the same Hermes OAuth
store afterward.

To retrofit older provisioned agents onto this model, run:

```bash
cd /home/delorenj/code/hermes-agent-template
./scripts/backfill-fleet-sot.sh
```

## Start the daemons for an agent

```bash
AGENT=bloodbank-pm
systemctl --user start hermes-${AGENT}-heartbeat.timer

# Gateway will fail to start until at least one messaging platform is wired.
# After running .scripts/30-telegram.sh or .scripts/31-slack.sh:
systemctl --user start hermes-${AGENT}-gateway.service
```

## Talk to an agent

| Channel | How |
| --- | --- |
| Telegram | DM `@<repo>_<role>_bot` (once Telegram is wired) |
| Slack | DM or mention the verified per-agent Slack bot (once Slack is wired) |
| Local CLI | `./agents/hermes/<role>/hermes chat "..."` |
| Bloodbank | Publish to `bloodbank.cmd.v1.agent.invocation.start` with `data.target_agent_id = <agent_id>` |

## Inspect fleet state

```bash
python3 - <<'EOF'
import yaml, pathlib
agents = yaml.safe_load(pathlib.Path.home().joinpath('.hermes/agents-registry.yaml').read_text())['agents']
for k, v in agents.items():
    print(f"{k:25s}  @{v['telegram']['bot_username']:30s}  plane={v['plane']['identifier']}  runtime=gh:{v['runtime_repo']}")
EOF

# Service status
systemctl --user list-units --state=active 'hermes-*'
systemctl --user list-timers 'hermes-*'

# Bloodbank routing identity consumed by the fleet-shared gateway
python3 -c "import yaml,pathlib; print(yaml.safe_load(pathlib.Path.home().joinpath('.hermes/agents-registry.yaml').read_text())['agents']['<agent-id>']['bloodbank'])"
```

## Deferred manual steps (one-time per agent)

### Telegram bot

1. Open Telegram, message `@BotFather`
2. `/newbot`, display name `<Repo> <ROLE>`, username `<repo>_<role>_bot`
3. Copy the HTTP API token
4. `/setjoingroups Disable`, `/setprivacy Disable`
5. Re-run the wire-up step:
   ```bash
   cd <project>/agents/hermes/<role>
   rm .scripts/.done-30-telegram
   TELEGRAM_BOT_TOKEN='<bot-id>:<secret>' \
     TELEGRAM_ALLOWED_USERS='<your-user-id>' \
     SKIP_TELEGRAM=0 ./.scripts/30-telegram.sh
   systemctl --user restart hermes-<agent-id>-gateway.service
   ```

The token is captured before shared fleet configuration is loaded, verified
through Telegram `getMe`, checked against local token and bot-identity owners,
and atomically written only to the profile's gitignored `runtime/.env` with
mode `0600`. `~/.hermes/fleet.env` and `~/.hermes/.env` must not contain
`TELEGRAM_BOT_TOKEN`; the manifest and registry store only verified bot
identity metadata. `TELEGRAM_ALLOWED_USERS` is non-secret and may be shared.

### Slack app and bot (opt-in)

Slack remains deferred unless `ENABLE_SLACK=1` (also accepts
`WIRE_SLACK=1`) is set or both credentials are supplied. Each enabled agent
must have its own Slack app-level Socket Mode token and bot token; neither may
be reused by another profile.

```bash
cd <project>/agents/hermes/<role>
ENABLE_SLACK=1 \
  SLACK_BOT_TOKEN='xoxb-...' \
  SLACK_APP_TOKEN='xapp-...' \
  SLACK_ALLOWED_USERS='U01ABC2DEF3' \
  ./.scripts/31-slack.sh
systemctl --user restart hermes-<agent-id>-gateway.service
```

The step calls Slack's read-only `auth.test` endpoint for the bot token, checks
the local fleet for token or bot-identity reuse, and records only the verified
workspace/bot identity in `role.yaml` and the fleet registry. Tokens are
atomically written to the agent's gitignored `runtime/.env` with mode `0600`.
They must never be placed in `~/.hermes/.env` or `~/.hermes/fleet.env`.

`SLACK_ALLOWED_USERS` is non-secret and may instead be set in `fleet.env` as a
shared policy. An empty allow-list is safe but denies all inbound Slack users.

## Restore an agent on a new machine

```bash
cd /path/to/the/project-repo
git submodule update --init --recursive
git -C agents/hermes/<role>/runtime lfs pull

# Restore secrets that were excluded from git:
op read 'op://DeLoSecrets/agent-hm-<repo>-<role>/.env' > agents/hermes/<role>/runtime/.env
# (or copy from a backup machine)

# Symlink the profile dir
ln -sfn $PWD/agents/hermes/<role>/runtime ~/.hermes/profiles/<repo>-<role>

# Re-enable systemd units
systemctl --user enable hermes-<repo>-<role>-gateway.service
systemctl --user enable hermes-<repo>-<role>-heartbeat.timer
systemctl --user start  hermes-<repo>-<role>-heartbeat.timer
```

## Retire an agent (manual, until v1.1 ships retire.sh)

```bash
AGENT=bloodbank-dev
# 1. Stop daemons
systemctl --user disable --now hermes-${AGENT}-gateway.service
systemctl --user disable --now hermes-${AGENT}-heartbeat.timer

# 2. Delete hermes profile (cascades to symlinked runtime — make sure
#    that's what you want!)
hermes profile delete ${AGENT}

# 3. Archive Plane project (Plane UI or API)
PROJECT_ID=$(python3 -c "import yaml,pathlib; print(yaml.safe_load(pathlib.Path.home().joinpath('.hermes/agents-registry.yaml').read_text())['agents']['${AGENT}']['plane']['project_id'])")
curl -X POST "https://plane.delo.sh/api/v1/workspaces/33god/projects/${PROJECT_ID}/archive/" \
  -H "X-API-Key: ${PLANE_33GOD_API_KEY}"

# 4. BotFather: /deletebot @<repo>_<role>_bot
# 5. Archive runtime repo (GitHub UI; we don't have delete_repo scope by default)
# 6. Remove registry entry
python3 -c "
import yaml, pathlib
p = pathlib.Path.home() / '.hermes' / 'agents-registry.yaml'
d = yaml.safe_load(p.read_text()); d['agents'].pop('${AGENT}', None)
p.write_text(yaml.safe_dump(d))"

# 7. In the project repo, remove the submodule
cd /path/to/project
git submodule deinit -f agents/hermes/<role>/runtime
git rm -f agents/hermes/<role>/runtime
rm -rf .git/modules/agents/hermes/<role>/runtime
rm -rf agents/hermes/<role>
```

## Troubleshooting

### Gateway service fails immediately
- Check `~/.hermes/profiles/<agent>/.env` has `TELEGRAM_BOT_TOKEN` set
- `journalctl --user -u hermes-<agent>-gateway.service`
- If "all configured messaging platforms failed to connect" — Telegram step wasn't run yet. Run `.scripts/30-telegram.sh` first.

### Bloodbank command not reaching an agent
- Confirm the fleet registry entry has `bloodbank.gateway_scope: fleet`
- Confirm its `bloodbank.target_agent_id` exactly matches the command's
  `data.target_agent_id`
- Inspect the fleet-shared Bloodbank gateway; there is intentionally no
  `hermes-<agent>-consumer.service` or runtime inbox to repair

### Heartbeat not checkpointing (runtime not pushing)
The checkpoint runs inside the heartbeat tick (after the board-reconciliation
sentinel pass), gated to at most once an hour.
- Look at the most recent heartbeat log: `tail <role>/runtime/logs/heartbeat.log`
- Verify the submodule's git remote is reachable: `cd ...runtime && git push origin HEAD`
- If LFS items fail to push: `git lfs push origin HEAD --all`
- Force a checkpoint out-of-band: `bash agents/hermes/<role>/.scripts/checkpoint.sh`

### Profile dir contains nested `profiles/profiles/...`
- That was a `--clone-all` bug; we switched to `--clone`. If you see it, just `rm -rf` the nested tree. The template's 10-hermes-profile.sh also has a belt-and-suspenders rm.

### `hermes` launcher complains about HERMES_BIN
- Check the launcher script: `./agents/hermes/<role>/hermes` falls back to `$HOME/.hermes/hermes-agent/.venv/bin/hermes` (after `$HERMES_BIN`, `fleet.env`, and config.toml). Override with `HERMES_BIN=/path/to/hermes ./agents/hermes/pm/hermes status`.
