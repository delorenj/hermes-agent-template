# Scrum Master role & the ticket sentinel

Status: Active architecture
Date: 2026-05-31

## What this is

The **Scrum Master** is a Hermes agent role whose core loop is the **continuous
ticket sentinel**: an always-on watcher that keeps a project moving by ensuring
exactly one implementation worker is advancing a ready ticket, or recording why
none can. When work is complete but only human review is missing, it runs the
**autonomous delegated-review** protocol so a project never stalls for days
waiting on a human.

The sentinel was first built inside the `drumjangler` project against Linear.
This document promotes it to a **first-class, bootstrappable role** in
`hermes-agent-template` — the single source of truth — so any project can
install it, and a single update to the template propagates to every deployment
via `copier update`.

## Role topology

`scrum-master` is a peer role to `pm`, materialized the same way:
`agents/hermes/scrum-master/`. It carries its own runtime, profile, systemd
units, and ticket-provider binding.

Two ways to provision it:

1. **Standalone:**
   ```bash
   copier copy gh:delorenj/hermes-agent-template ./agents/hermes/scrum-master \
     --data role=scrum-master --data target_repo=<repo> \
     --data ticket_provider=<linear|plane|trello>
   ```
2. **As a PM add-on:** during `role=pm` provisioning, answer `yes` to
   `with_scrum_master`. The PM provision chains a Scrum Master provision for the
   same repo and provider. This is the "option in the PM setup TUI."

Role identity: id `scrum-master`, short alias `sm`, display name
`<Repo> Scrum Master`.

## Single source of truth & propagation

The template is canonical. The Scrum Master engine — runner, prompt, protocol
docs, enforcement scripts, and the provider-adapter contract — lives **once** in
`template/.scripts/scrum-master/` and `template/scrum-master/`. Deployments
receive a rendered copy.

Updates propagate by re-applying the template:

```bash
cd <project>
copier update ./agents/hermes/scrum-master   # pulls newest engine, keeps answers
```

`copier update` performs a 3-way merge against the recorded answers in
`.copier-answers.yml`, so local provisioning state (board ids, tokens) is
preserved while engine logic is refreshed. A fleet-wide `hermes sm update`
helper (future) can loop this over every registered Scrum Master.

Project-local policy that a project *wants* to override (e.g. a custom drift
rubric) stays in the project's `docs/operations/`. Engine logic never forks per
project; only configuration does.

## Ticket-provider abstraction

The sentinel never talks to a ticket system directly. It calls a **normalized
adapter contract** (`template/.scripts/lib/ticket-provider.sh`) that dispatches
to one provider implementation selected at provision time
(`ticket_provider` = `linear` | `plane` | `trello`).

Rationale: Trello at Intelliforia, Plane for personal projects, Linear for the
Drumjangler experiment — one engine, three back ends.

### The contract

Each provider implements these operations (stdin/args in, JSON or status out):

| Operation | Purpose |
| --- | --- |
| `resolve` | Validate credentials/config; print `provider`, `board_id`, `board_url`. |
| `active_milestone` | JSON `{id, name, state}` for the current milestone/cycle/board. |
| `list_issues` | JSON array of issues in the active milestone: `{id, key, title, state, state_type, updated_at, assignee, url}`. |
| `get_issue <id>` | JSON issue detail incl. `description`, `acceptance`, `comments[]`. |
| `comment <id> <body>` | Post a comment; print comment id. |
| `transition <id> <target>` | Move an issue to a **normalized** state. |
| `create_board <name> <ident> <desc>` | Provision the board/project (the `40-plane` analog). |

### Normalized states

The engine reasons in provider-neutral states; each adapter maps them:

| Normalized | Linear (`state.type`) | Plane (`group`) | Trello (list) |
| --- | --- | --- | --- |
| `backlog` | `backlog` | `backlog` | "Backlog" list |
| `unstarted` | `unstarted` | `unstarted` | "To Do" list |
| `started` | `started` | `started` | "In Progress" list |
| `in_review` | `started` (named "In Review") | `started` ("In Review") | "Review" list |
| `completed` | `completed` | `completed` | "Done" list |

Adapters resolve the concrete state/list id from these names per board. List/
state naming is configurable per project in `role.yaml` under
`ticket_provider.state_map` so non-standard boards still work.

### Adapter status

- **Linear** — reference implementation. Reuses the proven
  `scripts/linear/graphql.py` envelope and `close-issue.py` transition logic.
- **Plane** — REST (`$PLANE_BASE/api/v1/workspaces/<ws>/projects/<proj>/...`,
  `X-API-Key`). Board creation already existed in `40-plane.sh`; the sentinel
  operations are added here. Requires live verification against a Plane board.
- **Trello** — REST (`https://api.trello.com/1/`, `key`+`token`). Boards/lists/
  cards model; "milestone" maps to a board, "issue" to a card, "state" to a
  list. Requires live verification against a Trello board.

## Autonomous delegated review

The Scrum Master inherits the autonomous delegated-review escape hatch
(`docs/operations/autonomous-delegated-review.md`, promoted alongside this
engine): when a ticket is blocked **only** on human review and a grace window
elapses, an independent reviewer checks it against the operator's locked intent
and may close it — emitting a decision event — if there is no significant drift.
This is provider-agnostic: closure goes through `transition <id> completed` on
the adapter, and the decision event uses the project's BloodBank lane.

## Provisioning steps (Scrum Master)

Added to the template `_tasks`, guarded by role/answers:

| Step | Role guard | Action |
| --- | --- | --- |
| `42-ticket-provider.sh` | all | Resolve/create the board for `ticket_provider` (generalizes `40-plane.sh`). |
| `75-scrum-master.sh` | `scrum-master` | Install the sentinel systemd timer + runner, wire the provider, install protocol docs. |

For `role=pm` with `with_scrum_master=yes`, `99-summary.sh` chains a
`scrum-master` provision for the same repo/provider.

## Migration of the Drumjangler prototype

Drumjangler's hand-built sentinel (`agents/hermes/pm/.scripts/continuous-ticket-sentinel.sh`,
`75-*`, `run-adversarial-review.sh`, the prompt, and `docs/operations/*`) is the
prototype this role generalizes. Once the template role is proven, Drumjangler
re-provisions a `scrum-master` role (provider=linear) and drops the bespoke PM
sentinel scripts, becoming a normal consumer of the single source of truth.
