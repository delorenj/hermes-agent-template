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
| 10 hermes profile | `hermes profile create <repo>-<role> --clone --no-alias` + mirror skills/plugins/hooks from default + symlink canonical runtime skills (`delonet-conventions`, `delonet-dotenv`, `hermes-pm-template-maintenance`, `hindsight`, `subagent-driven-development`) from `/home/delorenj/.agents/skills` | n/a |
| 20 runtime repo | Create gh:delorenj/agent-hm-<repo>-<role> (private), push scaffold from role-local `.runtime-scaffold/`, submodule-add into ./runtime/, symlink ~/.hermes/profiles/<id> → runtime | `SKIP_RUNTIME_REPO=1` |
| 30 telegram | Capture BotFather token, write to runtime/.env, enable hermes-telegram toolset | `SKIP_TELEGRAM=1` |
| 40 plane | Create Plane project in 33god workspace (1:1 with agent), patch identifier into role.yaml | `SKIP_PLANE=1` |
| 60 bloodbank | Install consumer (renders from scaffold w/ agent values), health-check NATS, install nats-py via uv if missing | `SKIP_BLOODBANK=1` |
| 70 systemd | Install user units: gateway, consumer, hourly checkpoint timer | `SKIP_SYSTEMD=1` |
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

[github]
runtime_repo_owner = "your-gh-owner"

[plane]
base = "https://plane.example.com"
workspace = "your-workspace"

[bloodbank]
nats_host = "127.0.0.1"
nats_port = 4222
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
systemctl --user start hermes-${AGENT}-consumer.service
systemctl --user start hermes-${AGENT}-checkpoint.timer

# Gateway will fail to start until Telegram is wired up (no other platforms
# configured). After running .scripts/30-telegram.sh:
systemctl --user start hermes-${AGENT}-gateway.service
```

## Talk to an agent

| Channel | How |
| --- | --- |
| Telegram | DM `@<repo>_<role>_bot` (once Telegram is wired) |
| Local CLI | `./agents/hermes/<role>/hermes chat "..."` |
| Bloodbank | Publish to subject `bloodbank.cmd.v1.agent.<agent_id>.<verb>.requested` |

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

# Tail the consumer for live bloodbank events
journalctl --user -fu hermes-<agent-id>-consumer.service
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
   SKIP_TELEGRAM=0 ./.scripts/30-telegram.sh
   systemctl --user restart hermes-<agent-id>-gateway.service
   ```

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
systemctl --user enable hermes-<repo>-<role>-{gateway,consumer}.service
systemctl --user enable hermes-<repo>-<role>-checkpoint.timer
systemctl --user start  hermes-<repo>-<role>-{consumer}.service
systemctl --user start  hermes-<repo>-<role>-checkpoint.timer
```

## Retire an agent (manual, until v1.1 ships retire.sh)

```bash
AGENT=bloodbank-dev
# 1. Stop daemons
systemctl --user disable --now hermes-${AGENT}-{gateway,consumer}.service
systemctl --user disable --now hermes-${AGENT}-checkpoint.timer

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

### Consumer not seeing events
- Verify NATS is up: `docker compose -f ~/code/33GOD/bloodbank/compose/docker-compose.yml ps`
- Tail consumer: `journalctl --user -fu hermes-<agent>-consumer.service`
- Make sure something is actually publishing to `bloodbank.evt.v1.repo.<repo>.*`

### Checkpoint timer not pushing
- Look at the most recent checkpoint log: `tail ~/.hermes/profiles/<agent>/logs/checkpoint.log`
- Verify the submodule's git remote is reachable: `cd ...runtime && git push origin HEAD`
- If LFS items fail to push: `git lfs push origin HEAD --all`

### Profile dir contains nested `profiles/profiles/...`
- That was a `--clone-all` bug; we switched to `--clone`. If you see it, just `rm -rf` the nested tree. The template's 10-hermes-profile.sh also has a belt-and-suspenders rm.

### `hermes` launcher complains about HERMES_BIN
- Check the launcher script: `./agents/hermes/<role>/hermes` references `/home/delorenj/code/hermes-agent/.venv/bin/hermes`. Override with `HERMES_BIN=/path/to/hermes ./agents/hermes/pm/hermes status`.
