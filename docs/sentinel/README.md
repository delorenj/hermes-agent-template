# Heartbeat sentinel — developer handoff

This guide is the entry point for continuing development of the **PM's heartbeat
sentinel**: a provider-agnostic, always-on ticket-reconciliation engine with an
autonomous adversarial review (act, do not wait). It is owned by the unified
`pm` role and runs out-of-band on the PM's heartbeat timer. Read this first,
then follow the links to the deeper guides. It's written so a Hermes agent (or a
human) can pick up the work without prior context.

## What the heartbeat sentinel is

The sentinel is the PM's continuous board-reconciliation loop, and its job is to
keep a project moving: it makes sure exactly one implementation worker is
advancing a ready ticket, or it records why none can. When a ticket reaches the
review lane, it runs an independent, rigorous **adversarial review** and acts on
the verdict autonomously — never parking work to wait on the operator. If the
work hasn't drifted from the operator's locked intent and clears the gate, the
review is `accepted`: the loop treats the ticket as done, unblocks dependents,
and emits a decision event. A real finding is `held` and the ticket goes back to
active.

There is no longer a separate `scrum-master` role; the sentinel folded into the
PM and now runs as part of the PM's **heartbeat** — a fused systemd-timer tick
that does the board-reconciliation pass and then a gated runtime checkpoint
(`.scripts/heartbeat.sh`). The engine itself lives under the PM at
`.scripts/sentinel/`.

It talks to the ticket board through a pluggable adapter, so the same engine
runs on Linear, Plane, or Trello. The engine lives once in this template (the
single source of truth) and propagates to every deployment with
`copier update`.

## Status at handoff

The following is true as of June 1, 2026.

- The engine, the provider abstraction, and the autonomous adversarial-review
  enforcement are built, syntax-clean, and validated offline with a mock
  provider.
- The **Linear** adapter is verified live against a real board (a real
  `DEL` team: `resolve`, `list_issues` returning 93 issues, and `get_issue`).
- The **Plane** adapter is verified live against a real workspace
  (`resolve`, `list_issues`, `get_issue`, `comment`, and a
  `transition` to `completed` on a disposable issue).
- The **Trello** adapter is implemented against the same contract but is
  **not** yet verified against a live board.
- The heartbeat runner uses `flock` on Linux and an atomic `mkdir` lock on macOS
  so concurrent ticks never overlap. The systemd timer (Linux) is the primary
  scheduler; a `launchd` agent covers macOS.

## Quick local install

For a local, single-machine install (no GitHub runtime repo, no Telegram, no
NATS), use the one-command bootstrap. From inside the target project:

```bash
export PLANE_API_KEY=<key>   # or LINEAR_API_KEY / TRELLO_KEY + TRELLO_TOKEN
curl -fsSL https://raw.githubusercontent.com/delorenj/hermes-agent-template/main/install-local.sh | sh
```

It installs `hermes` and `copier` if missing, writes a host-correct local
config, provisions the `pm` role, binds the PM to an existing board, and
installs the PM's heartbeat timer (`systemd` on Linux, `launchd` agent on
macOS). See [Development guide: local install](development.md#local-install-one-command).

<!-- prettier-ignore -->
> [!IMPORTANT]
> Full (non-local) provisioning is outward-facing. It can create a GitHub
> runtime repo, a Telegram bot, and a Plane project, and the Telegram step is
> interactive. Use `install-local.sh` or the `SKIP_*` flags described in
> [Development guide:
> provisioning](development.md#provisioning-the-pm-manual) for local or lean
> installs.

## Where the pieces live

All paths are relative to the repository root.

| Path | What it is |
| --- | --- |
| `copier.yml` | Questions (`role`, `ticket_provider`) and the `_tasks` provisioning chain. |
| `template/role.yaml.jinja` | The rendered role manifest, including the `ticket_provider` binding and the `reconcile` knobs (`grace_hours`, `auto_review`). |
| `template/.scripts/lib/ticket-provider.sh` | The adapter dispatcher (`tp`). The engine's only seam to a ticket system. |
| `template/.scripts/providers/{linear,plane,trello}.sh` | The provider adapters. |
| `template/.scripts/42-ticket-provider.sh` | Provisioning step that resolves or creates the board. |
| `template/.scripts/70-systemd.sh` | Provisioning step that installs the gateway, consumer, and the fused `heartbeat` timer (board-reconciliation sentinel pass + gated runtime checkpoint). |
| `install-local.sh` | One-command local install (no cloud, macOS + Linux). |
| `template/.scripts/heartbeat.sh` | The heartbeat runner: the sentinel full-pass dispatch (with its own cooldown/lock) plus the gated runtime checkpoint, fused into one tick. |
| `template/.scripts/sentinel.prompt.md.jinja` | The prompt the runner feeds to Hermes for a full reconciliation pass (rendered to `.scripts/sentinel.prompt.md`). |
| `template/.scripts/sentinel/bin/` | Enforcement tools: `issue-autonomous-review.sh`, `issue-close-gate.sh`, `emit-event.py`. |
| `template/.scripts/sentinel/docs/` | Runtime protocol docs shipped to each deployment. |

The `docs/sentinel/` directory you're reading now holds the **developer**
docs for this template. The `template/.scripts/sentinel/docs/` directory
holds the **runtime** protocol docs that ship into each provisioned project for
the agent to read at run time. Keep the two in sync when behavior changes.

## Read next

- [Architecture](architecture.md): how the engine, the adapter contract, the
  normalized states, and the autonomous adversarial review fit together.
- [Providers](providers.md): the adapter contract reference and a step-by-step
  guide to adding or verifying a provider.
- [Development guide](development.md): the edit, validate, and propagate
  workflow; provisioning and its `SKIP_*` flags; known gotchas; and the open
  roadmap.

## Next steps for the incoming agent

The highest-value open work, in order:

1. Live-verify the **Trello** adapter against a real board (Linear and Plane are
   done). See [Providers: verifying an
   adapter](providers.md#verifying-an-adapter-against-a-live-board).
2. Confirm `install-local.sh` on a real macOS machine. The Linux path and the
   Plane adapter are verified; the macOS `launchd` agent and `mkdir` lock get
   their first real run on a Mac.
3. Confirm the first full Hermes reconciliation pass on a freshly provisioned PM
   reconciles the board cleanly.
