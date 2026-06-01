# Scrum Master architecture

This guide explains how the Scrum Master engine works: the watch loop, the
provider abstraction, the normalized state model, and the autonomous
delegated-review protocol. Read the [handoff overview](README.md) first for the
big picture and the file map.

## The role

The Scrum Master is a first-class Hermes role, a peer of the `pm` role. It's
materialized into a project at `agents/hermes/scrum-master/` and carries its own
`role.yaml`, runtime, prompt, enforcement tools, and a scheduler unit
(`systemd` on Linux, `launchd` on macOS). The role id is `scrum-master`, and the
display name is `<Repo> Scrum Master`.

You can provision it two ways:

- **Standalone**, by running Copier with `--data role=scrum-master`.
- **As a PM add-on**, by answering `yes` to the `with_scrum_master` question
  during a `pm` provision. That chains a Scrum Master provision for the same
  repo and provider through `90-chain-scrum-master.sh`.

For the exact commands, see [Development guide:
provisioning](development.md#provisioning-a-scrum-master-manual).

## The watch loop

A scheduler (`systemd` timer on Linux, `launchd` agent on macOS) fires the
runner, `template/.scripts/scrum-master/continuous-ticket-sentinel.sh`, about
once a minute. The runner is a cheap heartbeat that decides whether a full,
LLM-backed pass is worth running. This keeps cost low while staying responsive.

The heartbeat reads the work-state file,
`runtime/continuous-ticket-sentinel-state.json`, and chooses one of these
decisions:

- `skip:active-worker`: a healthy worker process is already moving the active
  ticket, so there's nothing to do.
- `skip:cooldown`: a full pass ran too recently (within
  `SENTINEL_FULL_RUN_COOLDOWN_SECONDS`, default 300).
- `skip:blocker-cooldown`: the project is blocked and a full pass ran within
  `SENTINEL_BLOCKED_FULL_RUN_COOLDOWN_SECONDS`, default 900. This is the
  back-off that prevents a blocked project from burning passes.
- `run:full`: none of the above hold, so the runner executes a full pass.

A full pass runs `hermes chat` with the rendered prompt,
`continuous-ticket-sentinel.prompt.md`. The prompt tells the agent to reconcile
the board, the local evidence, and live worker state, then either delegate one
worker, monitor the active worker, run a delegated review, or record a blocker.

The runner writes the outcome back to the state file so dashboards and the next
heartbeat can read it. The required fields are `source`, `agent_id`, `repo`,
`ticket_provider`, `status`, `summary`, `reason`, `updated_at`,
`last_activity_at`, and `log_path`. The `status` is one of `idle`, `checking`,
`active`, `blocked`, `stalled`, or `error`.

<!-- prettier-ignore -->
> [!NOTE]
> The heartbeat must never use the state file's own modification time as a
> liveness signal. The heartbeat rewrites that file every minute, which would
> make a stale lane look perpetually fresh. The runner uses recorded worker
> process markers and `last_activity_at` instead.

## The provider abstraction

The engine never calls a ticket system directly. It calls a normalized adapter
contract, `template/.scripts/lib/ticket-provider.sh`, which exposes a single
function, `tp`, that dispatches to one provider implementation under
`template/.scripts/providers/`. The active provider is chosen at provision time
through the `ticket_provider` question and recorded in `role.yaml` under
`ticket_provider.name`.

This is what lets one engine serve three back ends: Trello, Plane, and Linear.
For the full contract and the per-provider details, see
[Providers](providers.md).

### Normalized states

The engine reasons in provider-neutral states. Each adapter maps these to the
concrete states, groups, or lists of its back end.

| Normalized state | Meaning |
| --- | --- |
| `backlog` | Not yet ready to start. |
| `unstarted` | Ready, not started. |
| `started` | A worker is actively implementing. |
| `in_review` | Implementation complete, awaiting review. |
| `completed` | Done. |

The mapping is configurable per project in `role.yaml` under
`ticket_provider`, so non-standard boards still work. For example, you can set
`in_review: "In Review"` and `completed: "Done"` to match your board's column
names.

## Autonomous delegated review

This is the feature that keeps a project from stalling for days when work is
done but no human is available to review it. The full protocol ships into each
deployment at
`template/.scripts/scrum-master/docs/autonomous-delegated-review.md`. Here's the
shape.

When the sentinel finds a ticket whose only remaining blocker is human review,
and a grace window (`scrum_master.grace_hours`, default 24) has passed with no
human activity, it delegates the review to an **independent** reviewer. The
reviewer must not be the agent that implemented the ticket. The reviewer checks
the work against the operator's **locked intent**: the acceptance criteria, the
active milestone, the project's horizon model, and the product north star.

The reviewer then runs the decision gate:

```bash
.scripts/scrum-master/bin/issue-autonomous-review.sh <ISSUE> <REPORT> --close
```

This script couples four checks so a closure can't slip through on a weak basis:

1. The close gate passes (`issue-close-gate.sh` confirms the evidence file is
   complete).
2. The reviewer attests independence from the implementer.
3. Drift is `none` or `minor`, never `significant`.
4. There are no unresolved critical or high findings.

If all four hold, the script closes the ticket through the adapter
(`tp transition <id> completed`) and emits a decision event with
`decision=closed`. If any check fails, the ticket stays open and the script
emits `decision=held` with the reason. When in doubt, it holds.

The escape hatch is deliberately narrow. It never applies to tickets blocked on
credentials, external access, paid actions, or product decisions. Those still
wait for a human.

### Decision events

Every delegated-review decision, whether `closed` or `held`, emits a local
BloodBank-style event:

```text
bloodbank.v1.repo.<repo>.issue.autonomous_review.decided
```

The event carries `issue`, `decision`, `drift`, `close_gate`, `reviewer_agent`,
`evidence_file`, and `report_file`. It's the operator's accountability record of
a decision made on their behalf. The emitter is
`template/.scripts/scrum-master/bin/emit-event.py`, and the event types are
documented in `template/.scripts/scrum-master/docs/bloodbank-events.md`. Events
append to `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`, a
local spool that doesn't require NATS, so the loop stays reliable offline.

## How the parts connect

A full pass moves through these components in order:

1. The scheduler (`systemd` timer or `launchd` agent) triggers the runner.
2. The runner's heartbeat decides `run:full` and calls `hermes chat` with the
   prompt.
3. The agent reads the runtime protocol docs and reconciles state by calling
   `tp` (the adapter) for board data.
4. The agent delegates a worker, monitors one, records a blocker, or runs a
   delegated review.
5. A delegated review runs the enforcement tools in `bin/`, which close through
   `tp transition` and emit a decision event.
6. The runner writes the outcome to the state file for the next heartbeat.

## Read next

- [Providers](providers.md): the adapter contract and how to add or verify a
  provider.
- [Development guide](development.md): editing, validating, propagating, and the
  open roadmap.
