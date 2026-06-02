# Scrum Master sentinel — developer handoff

This guide is the entry point for continuing development of the **Scrum Master**
role: a provider-agnostic, always-on ticket sentinel with an autonomous
adversarial review (act, do not wait). Read this first, then follow the links to
the deeper guides. It's written so a Hermes agent (or a human) can pick up the
work without prior context.

## What the Scrum Master is

The Scrum Master is a Hermes agent role whose core loop keeps a project moving:
it makes sure exactly one implementation worker is advancing a ready ticket, or
it records why none can. When a ticket reaches the review lane, it runs an
independent, rigorous **adversarial review** and acts on the verdict
autonomously — never parking work to wait on the operator. If the work hasn't
drifted from the operator's locked intent and clears the gate, the review is
`accepted`: the loop treats the ticket as done, unblocks dependents, and emits a
decision event. A real finding is `held` and the ticket goes back to active.

It talks to the ticket board through a pluggable adapter, so the same engine
runs on Linear, Plane, or Trello. The engine lives once in this template (the
single source of truth) and propagates to every deployment with
`copier update`.

## Status at handoff

The following is true as of June 1, 2026.

- The engine, the provider abstraction, and the autonomous adversarial-review
  enforcement are built, syntax-clean, and validated offline with a mock
  provider.
- The **Linear** adapter is verified live against a real board (Drumjangler's
  `DEL` team: `resolve`, `list_issues` returning 93 issues, and `get_issue`).
- The **Plane** adapter is verified live against a real workspace
  (`resolve`, `list_issues`, `get_issue`, `comment`, and a
  `transition` to `completed` on a disposable issue).
- The **Trello** adapter is implemented against the same contract but is
  **not** yet verified against a live board.
- The engine runs on **Linux** (`systemd`) and **macOS** (`launchd`). The runner
  uses `flock` on Linux and an atomic `mkdir` lock on macOS.
- **Drumjangler** has been cut over to this engine (provider `linear`). Its
  bespoke sentinel is retired. See
  [Development guide: the Drumjangler cutover](development.md#the-drumjangler-cutover).

## Quick local install

For a local, single-machine install (no GitHub runtime repo, no Telegram, no
NATS), use the one-command bootstrap. From inside the target project:

```bash
export PLANE_API_KEY=<key>   # or LINEAR_API_KEY / TRELLO_KEY + TRELLO_TOKEN
curl -fsSL https://raw.githubusercontent.com/delorenj/hermes-agent-template/main/install-local.sh | sh
```

It installs `hermes` and `copier` if missing, writes a host-correct local
config, provisions the PM and Scrum Master roles, binds the Scrum Master to an
existing board, and installs the sentinel as a `launchd` agent (macOS) or
`systemd` timer (Linux). See [Development guide: local
install](development.md#local-install-one-command).

<!-- prettier-ignore -->
> [!IMPORTANT]
> Full (non-local) provisioning is outward-facing. It can create a GitHub
> runtime repo, a Telegram bot, and a Plane project, and the Telegram step is
> interactive. Use `install-local.sh` or the `SKIP_*` flags described in
> [Development guide:
> provisioning](development.md#provisioning-a-scrum-master-manual) for local or lean
> installs.

## Where the pieces live

All paths are relative to the repository root.

| Path | What it is |
| --- | --- |
| `copier.yml` | Questions (`role`, `ticket_provider`, `with_scrum_master`) and the `_tasks` provisioning chain. |
| `template/role.yaml.jinja` | The rendered role manifest, including the `ticket_provider` binding and `scrum_master` knobs. |
| `template/.scripts/lib/ticket-provider.sh` | The adapter dispatcher (`tp`). The engine's only seam to a ticket system. |
| `template/.scripts/providers/{linear,plane,trello}.sh` | The provider adapters. |
| `template/.scripts/42-ticket-provider.sh` | Provisioning step that resolves or creates the board. |
| `template/.scripts/75-scrum-master.sh` | Provisioning step that installs the sentinel scheduler (`systemd` timer on Linux, `launchd` agent on macOS). |
| `template/.scripts/90-chain-scrum-master.sh` | Chains a Scrum Master provision when a PM opts in. |
| `install-local.sh` | One-command local install (no cloud, macOS + Linux). |
| `template/.scripts/scrum-master/continuous-ticket-sentinel.sh` | The runner (heartbeat plus full-pass dispatch). |
| `template/.scripts/scrum-master/continuous-ticket-sentinel.prompt.md.jinja` | The prompt the runner feeds to Hermes for a full pass. |
| `template/.scripts/scrum-master/bin/` | Enforcement tools: `issue-autonomous-review.sh`, `issue-close-gate.sh`, `emit-event.py`. |
| `template/.scripts/scrum-master/docs/` | Runtime protocol docs shipped to each deployment. |

The `docs/scrum-master/` directory you're reading now holds the **developer**
docs for this template. The `template/.scripts/scrum-master/docs/` directory
holds the **runtime** protocol docs that ship into each provisioned project for
the agent to read at run time. Keep the two in sync when behavior changes.

## Read next

- [Architecture](architecture.md): how the engine, the adapter contract, the
  normalized states, and the autonomous adversarial review fit together.
- [Providers](providers.md): the adapter contract reference and a step-by-step
  guide to adding or verifying a provider.
- [Development guide](development.md): the edit, validate, and propagate
  workflow; provisioning and its `SKIP_*` flags; the Drumjangler cutover record;
  known gotchas; and the open roadmap.

## Next steps for the incoming agent

The highest-value open work, in order:

1. Live-verify the **Trello** adapter against a real board (Linear and Plane are
   done). See [Providers: verifying an
   adapter](providers.md#verifying-an-adapter-against-a-live-board).
2. Confirm `install-local.sh` on a real macOS machine. The Linux path and the
   Plane adapter are verified; the macOS `launchd` agent and `mkdir` lock get
   their first real run on a Mac.
3. Give the Drumjangler Scrum Master its own runtime repo instead of the shared
   symlink. See [Development guide: open
   roadmap](development.md#open-roadmap).
4. Confirm the first full Hermes pass after a live cutover reconciles the board
   cleanly.
