# hermes-agent-template

Copier template that provisions a single Hermes agent role into an existing
repository, complete with its own ignored per-agent runtime directory for
memory and session state.

## How it relates to CommonProject

| | CommonProject | hermes-agent-template |
| --- | --- | --- |
| What it scaffolds | A new top-level project | An agent role inside an existing project |
| Copier target | `./my-new-project/` | `./agents/hermes/<role>/` |
| Asks | project name + description | role + purpose + tone |
| Post-gen artifacts | Plane project, bmad init, mise tasks | Plane project, Telegram bot wiring, optional Slack wiring, pure-local runtime, systemd units |

CommonProject runs first to create the umbrella project. hermes-agent-template
runs second (for each agent role you want) to drop agents into it.

## Roles

The template provisions a single Hermes role per invocation. The `pm` role
handles project management and triage, and also runs the continuous ticket
sentinel out-of-band: a provider-agnostic board-reconciliation pass (Linear,
Plane, or Trello) with an autonomous adversarial review (act, do not wait). The
sentinel runs as the PM's **heartbeat** systemd timer. Under the current
pure-local runtime contract, that tick performs board reconciliation only.
(There is no separate `scrum-master` role; its duties folded into the PM
heartbeat.)

To work on or extend the heartbeat sentinel, start with the [sentinel handoff
guide](docs/sentinel/README.md).

## Quickstart

```bash
# Inside a project (e.g. ~/code/33GOD/bloodbank)
copier copy gh:delorenj/hermes-agent-template ./agents/hermes/pm \
  --data role=pm \
  --data agent_purpose="Triage incoming bloodbank PRs and route work"
```

The template will:

1. Seed `~/.config/hermes-agent-template/config.toml` from the shipped example (see [Configuration](#configuration))
2. Ensure `~/.hermes/fleet.env` exists (single source of truth for shared Hermes binary/repo/registry)
3. Create the hermes profile `<repo>-pm` via `hermes profile create --clone`
4. Create ignored local state at `agents/hermes/pm/runtime/` (== HERMES_HOME)
5. Populate missing files from the runtime scaffold (config.yaml, SOUL.md, memories)
6. Refuse any stale project gitlink or `.gitmodules` mapping for that runtime
7. Verify a profile-dedicated BotFather token and store it only in `runtime/.env`
8. Defer Slack by default, or verify and store an explicitly supplied dedicated Slack app+bot pair in `runtime/.env`
9. Create a Plane project in your configured workspace
10. Mark Bloodbank ingress as fleet-scoped, with no per-profile consumer
11. Install systemd `--user` units: profile gateway and board-reconciliation heartbeat timer
12. Append the agent and its Bloodbank `target_agent_id` to `~/.hermes/agents-registry.yaml`

## Configuration

All environment-specific defaults live in **`~/.config/hermes-agent-template/config.toml`**
(override the path with `$HERMES_TEMPLATE_CONFIG`). Nothing user-specific is
hardcoded in the scripts — so the template can be handed to someone else and
retargeted by editing one file.

On the first provisioning run, `.scripts/01-config.sh` seeds that file from
[`config.example.toml`](config.example.toml) if it doesn't exist yet, then warns
you to review it. Keys:

| Section | Key | What it sets |
| --- | --- | --- |
| `fleet` | `hermes_bin`, `hermes_repo` | Shared Hermes executable + repo checkout |
| `fleet` | `hermes_git_url`, `hermes_git_ref`, `hermes_git_sha` | Reviewed Hermes fork publication used by clean installs and fleet audit metadata |
| `fleet` | `fleet_env`, `registry_file` | Fleet source-of-truth + registry locations |
| `fleet` | `oauth_file`, `codex_home` | Shared Hermes OAuth store + Codex CLI/app-server auth home |
| `fleet` | `runtime_scaffold_dir` | Fallback scaffold (if agent-local one is missing) |
| `fleet` | `canonical_skills_dir`, `symlinked_runtime_skills` | Skills mirrored into each profile |
| `plane` | `base`, `workspace` | Plane URL + workspace slug |

The example configuration retains an inert `[github].runtime_repo_owner` value
only so older manifests can still be parsed. Current provisioning never reads
it to create, attach, synchronize, restore, or retire runtime storage.

Resolution precedence for every value: **explicit env var → `~/.hermes/fleet.env`
→ `config.toml` → built-in fallback**. So you can still override any single value
per-run with an env var or `--data`, and existing setups keep working even without
the config file present.

## What gets created where

```
your-project/
├── agents/hermes/pm/                       ← copier output, tracked in YOUR project
│   ├── role.yaml                           ← role manifest
│   ├── SOUL.md                             ← personality (canonical)
│   ├── hermes                              ← launcher
│   ├── .scripts/                           ← provisioning scripts (idempotent re-run)
│   └── runtime/                            ← ignored local HERMES_HOME
│       (HERMES_HOME for this agent)
│       └── .env                            ← profile-local channel credentials, mode 0600, gitignored
└── ...

~/.hermes/agents-registry.yaml              ← fleet roster
~/.hermes/fleet.env                          ← shared Hermes binary/repo pointer
~/.config/systemd/user/                     ← per-profile gateway + heartbeat timer
```

Bloodbank command ingress is not a per-profile daemon. One fleet-shared Hermes
gateway reads the registry and routes canonical commands by
`data.target_agent_id`; local runtime directories contain no NATS consumer or
inbox bridge.
Provisioning and `fleet-sync.sh --apply` also retire the old
`hermes-<agent>-consumer.service`, even when an older `.done-70-systemd` marker
exists. Retirement fails closed: a user-manager/query error, failed disable, or
anything short of explicit `inactive` plus `disabled` leaves the unit file and
registry metadata intact and reports unhealthy drift.

Telegram and Slack ownership checks, identity claims, runtime credential
writes, and registry upserts serialize on `${registry_file}.lock`. The lock is
held by `flock`, so a crashed process cannot leave a stale logical lock; registry
writes use an atomic replace, sync the containing directory where supported, and
keep both registry and lock at mode `0600`. Profile credential replacements use
the same file-plus-parent durability boundary and remain safe to retry when a
durability sync reports an error.

## Reviewed Hermes runtime publication

Clean installs use only the reviewed fleet fork publication below. The local
installer verifies the commit belongs to the named ref and refuses an existing
checkout whose `origin` points elsewhere:

```bash
git clone --branch main --single-branch \
  https://github.com/delorenj/hermes-agent.git \
  ~/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1
git -C ~/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1 rev-parse HEAD
# 0408fec7a153e6c32c064acd2b8053917f1525f1
```

Do not install this fleet path from `NousResearch/hermes-agent` or mutate a
shared working checkout. Promotion happens on the reviewed fork `main` and is
installed into an immutable, full-SHA release directory pinned by
`config.example.toml` and each registry entry.

## Fleet single source-of-truth

All generated launchers read `~/.hermes/fleet.env` for:

- `HERMES_FLEET_BIN`
- `HERMES_FLEET_REPO`
- `HERMES_FLEET_REGISTRY_FILE`
- `HERMES_FLEET_OAUTH_FILE`
- `HERMES_FLEET_CODEX_HOME`

If you sync/pull your shared Hermes repo and keep the same binary path, every
agent wrapper picks it up automatically. `HERMES_FLEET_OAUTH_FILE` is the
shared Hermes provider OAuth store, including `openai-codex`; `HERMES_FLEET_CODEX_HOME`
is the shared Codex CLI/app-server config/auth home.

Telegram bot tokens and Slack bot/app tokens are deliberately excluded from
this shared layer. They belong only in the enabled agent's `runtime/.env` and
are checked for local profile ownership during provisioning; fleet config may
carry the non-secret `TELEGRAM_ALLOWED_USERS` and `SLACK_ALLOWED_USERS`
policies as a convenience.

To retrofit existing wrappers and user systemd units:

```bash
cd /home/delorenj/code/hermes-agent-template
./scripts/backfill-fleet-sot.sh
```

## Architecture

See `docs/architecture.md`.

## Re-running individual steps

Every `.scripts/<NN>-*.sh` is idempotent. Drop the `.done-<NN>-*` marker file
to force re-run:

```bash
cd agents/hermes/pm
rm .scripts/.done-40-plane
SKIP_TELEGRAM=1 SKIP_SYSTEMD=1 ./.scripts/40-plane.sh
```

## Retiring an agent

(TODO: ship `retire.sh` in v1.1)

Manual retirement is deliberately non-destructive: stop the systemd units,
disable the profile units, preserve the real named-profile directory, archive the Plane project, retire messaging bots,
and remove the registry entry while preserving the ignored runtime directory.
The verified-backup and no-automated-purge retention policy is in
[Operations](docs/operations.md#retire-an-agent-preserves-runtime-by-default).
