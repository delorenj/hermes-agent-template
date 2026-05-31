# Continuous ticket orchestration (Scrum Master engine)

Status: Scrum Master engine protocol (provider-agnostic)

## Invariant

If a ready ticket exists, exactly one implementation worker must be actively
moving it, or the Scrum Master records why none can. Prefer one live thread over
a quiet backlog. WIP limit: one active worker ticket.

The Scrum Master owns the watch loop. Workers (codex, opencode, copilot, …) own
implementation. The Scrum Master does not write application code or approve
merges. It may close a ticket only via the autonomous delegated-review protocol
(`autonomous-delegated-review.md`).

## Ticket access

All board access goes through the adapter (`tp`, from
`.scripts/lib/ticket-provider.sh`) and reasons in normalized states:
`backlog | unstarted | started | in_review | completed`. Never call the provider
directly — the engine is identical across Linear, Plane, and Trello.

## Work-state feed

Every heartbeat/pass keeps `runtime/continuous-ticket-sentinel-state.json`
current and machine-readable: `source`, `agent_id`, `repo`, `ticket_provider`,
`status` (`idle|checking|active|blocked|stalled|error`), `active_issue`,
`summary`, `reason`, `session`, `worktree`, `updated_at`, `last_activity_at`,
`log_path`.

## Source order (each pass)

1. Active milestone (`tp active_milestone`) and issues (`tp list_issues`).
2. Local evidence under `_bmad-output/implementation-artifacts/issue-evidence/`.
3. Live worker state: zellij sessions, worktrees, branches, recent git.

When sources disagree, record a truth-check note and keep the issue open.

## Ticket selection (when no worker active)

1. A blocked/review ticket needing only agent-doable evidence repair.
2. An unblocked issue in the current milestone.
3. A small, high-priority backlog issue when the milestone has no ready ticket.

Move the chosen issue to `started` (`tp transition <id> started`) and create/
refresh its evidence file before spawning exactly one worker.

## Stop conditions

Stop without spawning only when: the board/evidence cannot be inspected; every
candidate is blocked by external evidence/credentials/product decisions (a
ticket blocked **only** on human review is NOT a stop condition — route it to
delegated review); a worker is already active and healthy; or the next action
needs destructive git ops / production credentials / a paid action.

## Review and closure

1. Run ticket verification.
2. Run the close gate: `.scripts/scrum-master/bin/issue-close-gate.sh <ISSUE>`.
3. Gate pass → recommend closure; human-review-only + grace elapsed → autonomous
   delegated review (`autonomous-delegated-review.md`).
4. Gate fail → leave open, record missing evidence.

Board status is not proof. Repository evidence and the close gate are proof.
