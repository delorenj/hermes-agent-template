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
| 10 hermes profile | Create a clean named profile without cloning credentials; hard-validate and symlink canonical runtime skills (`delonet-conventions`, `delonet-dotenv`, `hermes-pm-template-maintenance`, `hindsight`, `subagent-driven-development`) from `~/.agents/skills`; reject legacy raw channel credentials for approval-gated migration | n/a |
| 20 local runtime | Populate missing files from role-local `.runtime-scaffold/` into ignored `./runtime/`, then audit/apply `pj migrate hermes.runtime-singleton`; the named profile remains a real directory | `SKIP_RUNTIME_REPO=1` |
| 30 telegram | Verify an invocation-supplied, profile-dedicated BotFather token; reject fleet reuse; store it in 1Password and map only an `op://` reference | `SKIP_TELEGRAM=1` |
| 31 slack | Deferred by default; verify a dedicated app+bot pair with `auth.test`, store both in 1Password, and map references only | `SKIP_SLACK=1` |
| 40 plane | Create Plane project in 33god workspace (1:1 with agent), patch identifier into role.yaml | `SKIP_PLANE=1` |
| 60 bloodbank | Compatibility checkpoint for fleet-shared routing; installs no files, dependencies, or services | `SKIP_BLOODBANK=1` remains a no-op |
| 70 systemd | Install user units: profile gateway and board-reconciliation heartbeat timer | `SKIP_SYSTEMD=1` |
| 80 registry | Append entry to ~/.hermes/agents-registry.yaml | n/a |
| 99 summary | Print summary | n/a |

Every step is idempotent — re-running the entire provisioning is safe. Each
step writes a `.done-NN-*` marker; delete that marker to force a re-run.
PM reconciliation defaults on. A deliberate checkpoint-only deployment sets
both `reconcile.enabled: false` and `reconcile.explicit_opt_out: true`; this
sentinel preserves the choice while legacy default-off manifests migrate to
the operational default.

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
hermes_git_url = "https://github.com/delorenj/hermes-agent.git"
hermes_git_ref = "main"
hermes_git_sha = "0408fec7a153e6c32c064acd2b8053917f1525f1"
oauth_file = "~/.hermes/auth.json"
codex_home = "~/.codex"
canonical_skills_dir = "/path/to/.agents/skills"
vox_plugin_name = "vox"
vox_plugin_dir = "~/code/voxxy/plugins/tts/vox"
vox_voice = "carlin"
vox_url = "https://vox.delo.sh"
onepassword_vault = "DeLoSecrets"
onepassword_item_prefix = "hermes-agent"

[plane]
base = "https://plane.example.com"
workspace = "your-workspace"
```

`role.yaml` stores compatibility metadata for older generated roles, but the
current local-runtime provisioner does not use it for storage. Plane workspace
defaults are still filled from `config.toml`.

## Fleet source-of-truth

`~/.hermes/fleet.env` is the single shared pointer every generated launcher reads:

- `HERMES_FLEET_BIN` (the exact Hermes executable all agents use)
- `HERMES_FLEET_REPO` (the upstream/fork checkout you keep on the edge)
- `HERMES_FLEET_REGISTRY_FILE` (defaults to `~/.hermes/agents-registry.yaml`)
- `HERMES_FLEET_OAUTH_FILE` (shared Hermes provider OAuth store)
- `HERMES_FLEET_CODEX_HOME` (shared Codex CLI/app-server auth/config home)

If you `git pull`/sync the repo at `HERMES_FLEET_REPO` and rebuild/update that
same binary path, every wrapper benefits immediately with no per-agent edits.
Every generated wrapper and systemd entrypoint also resolves the containing
project and exports `TERMINAL_CWD` for that process. Do not set `terminal.cwd`
through `hermes config`: named-profile config is shared across the fleet.
For Codex auth, run `hermes auth add openai-codex` through any generated agent
launcher once; all agents using the same fleet env read the same Hermes OAuth
store afterward.

The reviewed runtime source is the `delorenj/hermes-agent` fork, publication
ref `main`, pinned at
`0408fec7a153e6c32c064acd2b8053917f1525f1`. `install-local.sh` performs a
single-branch clone, verifies the pin is on that ref, passes both `--branch` and
`--commit` to the checked-out installer, and refuses an existing checkout with
a different origin. It never fetches or writes upstream `main`.

To retrofit older provisioned agents onto this model, run:

```bash
cd /home/delorenj/code/33GOD/hermes-agent-template
./scripts/backfill-fleet-sot.sh
```

Then audit legacy consumers before relying on the fleet-shared Bloodbank
gateway:

```bash
./scripts/fleet-sync.sh
./scripts/fleet-sync.sh --apply
```

Any active, enabled, installed, or registry-declared
`hermes-<agent>-consumer.service` is unhealthy drift. Apply mode disables and
stops it, proves the unit is explicitly `inactive` and `disabled`, then removes
the unit, reloads user systemd, and removes the legacy registry metadata. A
user-manager/query error, failed disable, or ambiguous post-disable state fails
closed and preserves both the unit file and registry metadata. The same cleanup
runs in step 70 before its done marker is honored.

Fleet registry and chat identity changes share `${registry_file}.lock`. `flock`
owns the lock in the kernel (an on-disk lock file surviving a crash is safe),
while atomic replace prevents partial YAML. Registry and lock files are mode
`0600`; symlink targets are refused. Registry and profile-local credential
replacements also sync the containing directory where the platform supports it.
Unexpected directory-sync errors are reported as failures without marking the
step complete, so rerunning the idempotent provisioning step is safe.

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
| Bloodbank | Publish to `bloodbank.cmd.agent.invocation.start` with `data.target_agent_id = <agent_id>` |

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

A resolvable target remains quarantined while `bloodbank.enabled` is `false`.
After the profile and ingress policy have passed their activation checks, edit
that strict boolean to `true` in the role's `role.yaml`, then rerun
`.scripts/80-registry.sh`. No provisioning or parity command auto-enables it.

## Deferred manual steps (one-time per agent)

### Telegram bot

1. Open Telegram, message `@BotFather`
2. `/newbot`, display name `<Repo> <ROLE>`, username `<repo>_<role>_bot`
3. Copy the HTTP API token
4. `/setjoingroups Disable`, `/setprivacy Disable`
5. Re-run the wire-up step:
   ```bash
   cd <project>/agents/hermes/<role>
   TELEGRAM_BOT_TOKEN='<bot-id>:<secret>' \
     TELEGRAM_ALLOWED_USERS='<your-user-id>' \
     SKIP_TELEGRAM=0 ./.scripts/30-telegram.sh
   ./.scripts/70-systemd.sh
   ```

The token is captured before shared fleet configuration is loaded, verified
through Telegram `getMe`, checked against local token and bot-identity owners,
and stored directly in the configured 1Password vault. Only its `op://`
reference is mapped into `secrets.onepassword.env` in the named profile delta;
no dotenv file may contain `TELEGRAM_BOT_TOKEN`. The manifest and registry
store only verified bot identity metadata. `TELEGRAM_ALLOWED_USERS` is
non-secret and may be shared.

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
./.scripts/70-systemd.sh
```

The step calls Slack's read-only `auth.test` endpoint for the bot token, checks
the local fleet for token or bot-identity reuse, and records only the verified
workspace/bot identity in `role.yaml` and the fleet registry. Tokens are stored
in 1Password and only their `op://` references are mapped into the named
profile. They must never be placed in `runtime/.env`, `~/.hermes/.env`, or
`~/.hermes/fleet.env`.

`SLACK_ALLOWED_USERS` is non-secret and may instead be set in `fleet.env` as a
shared policy. An empty allow-list is safe but denies all inbound Slack users.

## Back up and restore an agent

The template does **not** ship automatic backup for the ignored
`agents/hermes/<role>/runtime/` directory. A project clone recreates the
tracked role and its empty scaffold, not accumulated local state. Configure an
encrypted filesystem backup or snapshot that includes the exact runtime path
before treating the agent as recoverable.

For a manual transfer, create a private archive, copy it to operator-managed
encrypted storage, and verify both the checksum and readable member list:

```bash
PROJECT=/absolute/path/to/project
ROLE=pm
BACKUP_DIR="$HOME/.local/state/hermes-runtime-backups"
mkdir -p -m 0700 "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/$(basename "$PROJECT")-${ROLE}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar -C "$PROJECT" -czf "$BACKUP" "agents/hermes/${ROLE}/runtime"
chmod 0600 "$BACKUP"
sha256sum "$BACKUP" > "${BACKUP}.sha256"
sha256sum -c "${BACKUP}.sha256"
tar -tzf "$BACKUP" >/dev/null
```

That archive may contain credentials and private conversations. It is not an
off-host backup until it has been copied to encrypted storage controlled by the
operator and independently verified there.

Recovery sources are intentionally distinct:

- The verified filesystem backup is the only complete source for local config,
  sessions, databases, skills, and other runtime files.
- Hindsight can restore only memories/events that were previously written to
  its remote bank. It is not a backup of the runtime directory.
- The secret manager can restore only credentials deliberately stored there;
  it does not contain memories, sessions, or local configuration by default.
- Re-running provisioning restores the scaffold and service definitions, not
  learned state.

Restore the project and provision the role first. With its services stopped,
extract the verified archive at the project root, run
`pj migrate hermes.runtime-singleton /absolute/path/to/project`, and confirm
the named profile is a real directory before enabling the services.

### Inject an encrypted model credential from 1Password

The commands below stream secrets directly from 1Password into
`systemd-creds`; they do not create a plaintext file. Set `model.key_env` in
`role.yaml` before creating the model credential.

```bash
AGENT=example-director
CRED_DIR="$HOME/.config/hermes-agent/credentials"
install -d -m 0700 "$CRED_DIR"
env -u OP_API_TOKEN op read 'op://DeLoSecrets/<item>/<field>' \
  | systemd-creds encrypt --user --name=model_api_key - \
      "$CRED_DIR/${AGENT}-model-api-key.cred"
chmod 0600 "$CRED_DIR/${AGENT}-model-api-key.cred"
```

Re-run `.scripts/70-systemd.sh`; it reconciles the unit definitions even after
a completed installation, so no marker deletion is required. Never print,
decrypt to disk, or commit the credential. Channel credentials use the native
Hermes `secrets.onepassword.env` reference mapping instead of systemd files.

## Retire an agent (preserves runtime by default)

Retirement stops external behavior and detaches the profile while preserving
the role directory and every runtime byte. Do not use a profile command whose
deletion behavior is unknown.

```bash
PROJECT=/absolute/path/to/project
ROLE=pm
AGENT=bloodbank-pm
RUNTIME="$PROJECT/agents/hermes/$ROLE/runtime"
PROFILE="$HOME/.hermes/profiles/$AGENT"

systemctl --user disable --now "hermes-${AGENT}-gateway.service"
systemctl --user disable --now "hermes-${AGENT}-heartbeat.timer"

# The profile is a real directory. Do not unlink or recursively delete it;
# archive/retire through a dedicated PJangler migration when available.
test -d "$PROFILE"
test ! -L "$PROFILE"

# Archive the Plane project and retire Telegram/Slack identities through their
# administrative UIs, then remove the fleet registry entry under its lock.
# The runtime directory remains in place.
test -d "$RUNTIME"
```

### Runtime retention after retirement

Profile and service retirement always preserves the local runtime. This
release intentionally provides no automated runtime purge, and this operations
guide supplies no deletion recipe.

Any purge is a separate future operator-retention process. It requires a
separately reviewed, path-safe tool that canonicalizes both the repository root
and role path, refuses ambiguous or linked targets, and verifies that the
backup archive contains the expected runtime members before it can remove any
data. Until such a tool is reviewed and shipped, preserve the runtime.

Removing the tracked role scaffold is a different project change and is not
part of profile or service retirement.

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

### Runtime changes are not appearing in Hindsight or backups

Pure-local runtime changes are not synchronized by the heartbeat.
- Look at the most recent heartbeat log: `tail <role>/runtime/logs/heartbeat.log`
- Verify the configured filesystem backup includes the exact runtime path and
  successfully restore-test its latest snapshot.
- Query Hindsight separately for the agent bank; only events already written
  there are recoverable from Hindsight.
- Check the secret manager separately for the profile credentials you chose to
  store there.

### Profile dir contains nested `profiles/profiles/...`
- That was a `--clone-all` bug; current provisioning creates a clean profile without cloning. Preserve
  the profile and runtime, inspect the unexpected nesting, and move only the
  confirmed redundant entries to a quarantine directory for review.

### `hermes` launcher complains about HERMES_BIN
- Check the launcher script: `./agents/hermes/<role>/hermes` falls back to the pinned full-SHA release under `$HOME/.local/share/hermes-agent/releases/` (after `$HERMES_BIN`, `fleet.env`, and config.toml). Override with `HERMES_BIN=/path/to/hermes ./agents/hermes/pm/hermes status`.
