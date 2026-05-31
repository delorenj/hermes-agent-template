# Autonomous delegated review and closure

Status: Scrum Master engine protocol (provider-agnostic)

## Purpose

The Scrum Master sentinel must not block for days when the only thing between a
completed ticket and closure is an unavailable human reviewer. After a grace
window, an **independent review agent** checks the work against the operator's
locked intent. If there is no significant drift and the close gate passes, it
closes the ticket on the operator's behalf — through the ticket-provider adapter
(`tp transition <id> completed`) — and emits a BloodBank decision event carrying
the full report. This works identically on Linear, Plane, or Trello.

This is the only sanctioned path for the agent to close a ticket without a
human. It is deliberately narrow and loosens no other gate.

## Not for

Fires **only** when the sole remaining blocker is human review of completed
work. Out of scope (record the blocker and wait):

- Blocked on external credentials, third-party access, or paid actions.
- Blocked on an undecided product decision.
- Acceptance criteria not fully satisfied by repository evidence.
- Depends on another open, unblocked issue.

## Trigger conditions (ALL)

1. **Human-review-only blocker** — issue is `in_review`, complete, nothing
   missing but a human's sign-off.
2. **Grace window elapsed** — no human activity for `scrum_master.grace_hours`
   (role.yaml; default 24h; override env `DRUMJANGLER_AUTO_REVIEW_GRACE_HOURS`).
3. **Evidence exists** — complete evidence file under
   `_bmad-output/implementation-artifacts/issue-evidence/<ISSUE>.md`.
4. **Independent reviewer available** — agent id differs from the implementer
   recorded in the evidence (`Worker:` / `Implemented by:`) and from the PM.

## Locked intent baseline

Drift is measured against fixed intent, assembled from: the issue's acceptance
criteria; the active milestone (`tp active_milestone`) and the project's horizon
model; the product north star; and any locked planning/decision artifacts. The
reviewer does not re-litigate intent.

## Drift rubric

- **significant (HOLD)** — an AC unmet; user-facing capability added/removed
  beyond the ACs or milestone; contradicts a locked decision/north star; pulls
  Later work into Now or touches another milestone; contradicts locked
  architecture; introduces a new external dependency/credential/paid action.
- **minor (close allowed)** — internal refactors, extra tests, naming, cosmetic
  deviations within locked intent, docs.
- **none** — matches locked intent and ACs.

Only `none`/`minor` with no unresolved critical/high findings may close.

## Decision

Run from the role's bin (couples gate + drift + event + closure):

```bash
.scripts/scrum-master/bin/issue-autonomous-review.sh <ISSUE> <REPORT> --close
```

- **closed** — all conditions met; ticket transitioned to `completed` via the
  adapter; event emitted with `decision=closed`.
- **held** — any condition fails; ticket stays open; event `decision=held` with
  the reason. When in doubt, hold.

The script will not emit a `closed` decision while the close gate fails or drift
is `significant`.

## Decision event

```text
bloodbank.v1.repo.<repo>.issue.autonomous_review.decided
```

Required data: `issue`, `decision`, `drift`, `close_gate`, `reviewer_agent`,
`evidence_file`, `report_file`. See `bloodbank-events.md`.

## Review report shape

Write `<ISSUE>.review.md`; the script validates it:

```markdown
# Autonomous Review Report: <ISSUE>
## Issue
- Linear/Plane/Trello issue: <ISSUE>
- Review lane reason:
## Reviewer
- Reviewer agent: <independent-agent-id>
- Independent of implementer: yes
## Locked Intent Baseline
- Acceptance criteria source:
- Milestone / horizon:
## Drift Assessment
- Drift assessment: none        # none | minor | significant
## Adversarial Findings
- Critical/high findings: none
## Decision
- Decision: close               # close | hold
```

## Operator override

`scrum_master.auto_review: false` (role.yaml) or `SCRUM_MASTER_AUTO_REVIEW=off`
disables the escape hatch. Autonomous closures are fully traceable via the
decision events in `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`.
