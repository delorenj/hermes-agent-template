# Heartbeat sentinel architecture

This guide explains how the heartbeat sentinel engine works: the heartbeat loop,
the provider abstraction, the normalized state model, and the autonomous
adversarial-review protocol. Read the [handoff overview](README.md) first for the
big picture and the file map.

## Who owns it

The sentinel is owned by the unified `pm` role. It's materialized into a project
at `agents/hermes/pm/`, where the PM carries its `role.yaml`, runtime, the
sentinel prompt, the enforcement tools under `.scripts/sentinel/`, and the
heartbeat runner. There is no separate `scrum-master` role; its
continuous-ticket-sentinel duties fold into the PM and run out-of-band on the
PM's heartbeat timer.

You provision it by provisioning the PM: run Copier with `--data role=pm` (the
`70-systemd.sh` step installs the fused `heartbeat` timer that drives the
sentinel). For the exact commands, see [Development guide:
provisioning](development.md#provisioning-the-pm-manual).

## The heartbeat loop

A scheduler (`systemd` timer on Linux, `launchd` agent on macOS) fires the
runner, `template/.scripts/heartbeat.sh`, about once a minute. For a pure-local
runtime, each tick performs a board-reconciliation **sentinel pass**. The cheap
heartbeat decides whether a full, LLM-backed reconciliation pass is worth
running. This keeps cost low while staying responsive; runtime backup remains
an operator-managed filesystem concern outside the sentinel.

The sentinel reads the work-state file,
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
`sentinel.prompt.md`. The prompt tells the agent to reconcile
the board, the local evidence, and live worker state, then either delegate one
worker, monitor the active worker, run an autonomous adversarial review, or
record a blocker. A pass never ends in a "looks good, waiting on the operator"
state: the adversarial review acts on its own verdict.

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
| `cancelled` | Intentionally rejected or abandoned; not done. |

The mapping is configurable per project in `role.yaml` under
`ticket_provider`, so non-standard boards still work. For example, you can set
`in_review: "In Review"`, `completed: "Done"`, and `cancelled: "Cancelled"`
to match your board's column names.

## Autonomous adversarial review (act, do not wait)

This is the **normal per-pass path** for a ticket that has reached review: an
independent, rigorous **adversarial review** that renders a verdict and the loop
**acts on it autonomously**. It is not a narrow escape hatch and not an exception
to a no-close rule — it is how reviewed work moves. The full protocol ships into
each deployment at
`template/.scripts/sentinel/docs/autonomous-delegated-review.md`. Here's the
shape.

When the sentinel finds a ticket in the review lane, it delegates the review to
an **independent** reviewer. There is no mandatory grace window:
`reconcile.grace_hours` defaults to `0`, so the reviewer acts immediately. It
remains an optional operator knob — set it `>0` to reintroduce a deliberate wait
— but the default never parks completed work waiting on the operator. The
reviewer must not be the agent that implemented the ticket. Acting as an
adversarial microscope, the reviewer tries to break the work, checking it against
the operator's **locked intent**: the acceptance criteria, the active milestone,
the project's horizon model, and the product north star.

The reviewer then runs the decision gate:

```bash
.scripts/sentinel/bin/issue-autonomous-review.sh <ISSUE> <REPORT> --close
```

This script couples four checks so an acceptance can't slip through on a weak
basis:

1. The close gate passes (`issue-close-gate.sh` confirms the evidence file is
   complete).
2. The reviewer attests independence from the implementer.
3. Drift is `none` or `minor`, never `significant`.
4. There are no unresolved critical or high findings.

If all four hold, the script renders an `accepted` verdict and emits a decision
event with `decision=accepted`. **Treat review as done:** an accepted `in_review`
ticket counts as `completed` for dependents and flow, so downstream work is
unblocked immediately. By default the ticket **stays in the review lane**, which
serves as the operator's deferred-QA queue — it is not auto-transitioned to
`completed`. The `--close` flag is optional (an operator QA sweep can use it to
drive `tp transition <id> completed`); the normal loop omits it. If any check
fails, the ticket goes back to / stays active and the script emits
`decision=held` with the reason. When in doubt, it holds.

This is an adversarial review, not a lightweight or sanity check — the reviewer
is hard to satisfy. It never clears tickets blocked on credentials, external
access, paid actions, or undecided product decisions. Those genuine out-of-scope
blockers are recorded and waited on exactly as before.

### Downstream regression rollback

Deferring operator QA buys speed, and the safety valve is a downstream regression
rollback. If a later dependent proves a review-accepted feature is **actually
broken**, the loop moves that feature back to active (`started` if a worker takes
it now, else `unstarted`) as a prerequisite of the dependent, comments naming the
dependent and the symptom, and emits

```text
bloodbank.v1.repo.<repo>.issue.review_rollback.recorded
```

carrying `{issue, surfaced_by, reason}`. The dependent stays blocked on the
prerequisite until the fix lands. This is expected and healthy — it is the trade
for deferring operator QA, not a failure of the review.

### Decision events

Every adversarial-review decision, whether `accepted` or `held`, emits a local
BloodBank-style event:

```text
bloodbank.v1.repo.<repo>.issue.autonomous_review.decided
```

The event carries `issue`, `decision`, `drift`, `close_gate`, `reviewer_agent`,
`evidence_file`, and `report_file`. Together with the
`issue.review_rollback.recorded` event it forms the operator's queryable
accountability trail for every autonomous decision. The emitter is
`template/.scripts/sentinel/bin/emit-event.py`, and the event types are
documented in `template/.scripts/sentinel/docs/bloodbank-events.md`. Events
append to `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`, a
local spool that doesn't require NATS, so the loop stays reliable offline.

## How the parts connect

A full pass moves through these components in order:

1. The scheduler (`systemd` timer or `launchd` agent) triggers the heartbeat
   runner (`heartbeat.sh`).
2. The runner's sentinel logic decides `run:full` and calls `hermes chat` with
   the prompt.
3. The agent reads the runtime protocol docs and reconciles state by calling
   `tp` (the adapter) for board data.
4. The agent delegates a worker, monitors one, records a blocker, or runs an
   autonomous adversarial review.
5. An adversarial review runs the enforcement tools in `.scripts/sentinel/bin/`,
   which render an `accepted` or `held` verdict and emit a decision event; the
   loop acts on the verdict (treat as done and unblock dependents, or send the
   ticket back to active) without waiting on the operator.
6. The runner writes the outcome to the state file for the next tick and exits.

## Read next

- [Providers](providers.md): the adapter contract and how to add or verify a
  provider.
- [Development guide](development.md): editing, validating, propagating, and the
  open roadmap.
