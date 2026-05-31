# hermes-agent-template

Copier template that provisions a single Hermes agent role into an existing
repository, complete with its own per-agent runtime repo for git-tracked
memory/state checkpointing.

## How it relates to CommonProject

| | CommonProject | hermes-agent-template |
| --- | --- | --- |
| What it scaffolds | A new top-level project | An agent role inside an existing project |
| Copier target | `./my-new-project/` | `./agents/hermes/<role>/` |
| Asks | project name + description | role + purpose + tone |
| Post-gen artifacts | Plane project, bmad init, mise tasks | Plane project, Telegram bot wiring, agent-hm-* runtime repo, systemd units |

CommonProject runs first to create the umbrella project. hermes-agent-template
runs second (for each agent role you want) to drop agents into it.

## Roles

The template provisions several roles. The `pm` role handles project management
and triage. The `scrum-master` role runs a provider-agnostic ticket sentinel
(Linear, Plane, or Trello) with an autonomous delegated-review escape hatch.

To work on or extend the Scrum Master, start with the [Scrum Master handoff
guide](docs/scrum-master/README.md).

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
4. Create a new private GitHub repo `<owner>/agent-hm-<repo>-pm` for the runtime
5. Populate it with the runtime scaffold (config.yaml, SOUL.md, memories, consumer.py)
6. Add it as a git submodule at `agents/hermes/pm/runtime/` (== HERMES_HOME)
7. Prompt for a BotFather token, store it in `runtime/.env`
8. Create a Plane project in your configured workspace
9. Install systemd `--user` units: gateway, consumer, hourly checkpoint timer
10. Append the agent to `~/.hermes/agents-registry.yaml`

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
| `fleet` | `fleet_env`, `registry_file` | Fleet source-of-truth + registry locations |
| `fleet` | `runtime_scaffold_dir` | Fallback scaffold (if agent-local one is missing) |
| `fleet` | `canonical_skills_dir`, `symlinked_runtime_skills` | Skills mirrored into each profile |
| `github` | `runtime_repo_owner` | Owner of the `agent-hm-*` runtime repos |
| `plane` | `base`, `workspace` | Plane URL + workspace slug |
| `bloodbank` | `nats_host`, `nats_port`, `compose_dir` | NATS endpoint + compose dir hint |

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
│   └── runtime/                            ← git submodule → agent-hm-<repo>-pm
│       (HERMES_HOME for this agent)
└── ...

github.com/delorenj/agent-hm-<repo>-pm/     ← new private repo, this agent's state
├── config.yaml                             ← cloned from global ~/.hermes
├── SOUL.md                                 ← evolves over time
├── memories/{MEMORY,USER}.md
├── sessions/sessions.db                    ← LFS-tracked
├── decisions/                              ← markdown files, one per call
├── bloodbank-consumer.py
└── .gitattributes / .gitignore             ← LFS rules + secret guards

~/.hermes/agents-registry.yaml              ← fleet roster
~/.hermes/fleet.env                          ← shared Hermes binary/repo pointer
~/.config/systemd/user/                     ← gateway, consumer, checkpoint timer
```

## Fleet single source-of-truth

All generated launchers read `~/.hermes/fleet.env` for:

- `HERMES_FLEET_BIN`
- `HERMES_FLEET_REPO`
- `HERMES_FLEET_REGISTRY_FILE`

If you sync/pull your shared Hermes repo and keep the same binary path, every
agent wrapper picks it up automatically. To retrofit existing wrappers:

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

Manual: stop systemd units, `hermes profile delete`, archive the Plane
project, `/deletebot` in BotFather, archive the
`agent-hm-*` runtime repo, remove the registry entry.
