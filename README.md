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
| Post-gen artifacts | Plane project, bmad init, mise tasks | Plane project, Cloudflare email rule, Telegram bot wiring, agent-hm-* runtime repo, systemd units |

CommonProject runs first to create the umbrella project. hermes-agent-template
runs second (for each agent role you want) to drop agents into it.

## Quickstart

```bash
# Inside a project (e.g. ~/code/33GOD/bloodbank)
copier copy gh:delorenj/hermes-agent-template ./agents/hermes/pm \
  --data role=pm \
  --data agent_purpose="Triage incoming bloodbank PRs and route work"
```

The template will:

1. Create the hermes profile `<repo>-pm` via `hermes profile create --clone-all`
2. Create a new private GitHub repo `agent-hm-<repo>-pm` for the runtime
3. Populate it with the runtime scaffold (config.yaml, SOUL.md, memories, consumer.py)
4. Add it as a git submodule at `agents/hermes/pm/runtime/` (== HERMES_HOME)
5. Prompt for a BotFather token, store it in `runtime/.env`
6. Create a Plane project in the `33god` workspace
7. Create a Cloudflare Email Routing rule for `<repo>-pm@delo.sh`
8. Install systemd `--user` units: gateway, consumer, hourly checkpoint timer
9. Append the agent to `~/.hermes/agents-registry.yaml`

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
~/.config/systemd/user/                     ← gateway, consumer, checkpoint timer
```

## Architecture

See `docs/architecture.md`.

## Re-running individual steps

Every `.scripts/<NN>-*.sh` is idempotent. Drop the `.done-<NN>-*` marker file
to force re-run:

```bash
cd agents/hermes/pm
rm .scripts/.done-40-plane
SKIP_TELEGRAM=1 SKIP_EMAIL=1 SKIP_SYSTEMD=1 ./.scripts/40-plane.sh
```

## Retiring an agent

(TODO: ship `retire.sh` in v1.1)

Manual: stop systemd units, `hermes profile delete`, archive the Plane
project, delete the CF email rule, `/deletebot` in BotFather, archive the
`agent-hm-*` runtime repo, remove the registry entry.
