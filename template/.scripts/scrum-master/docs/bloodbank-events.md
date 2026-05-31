# Scrum Master workflow events

Status: Scrum Master engine protocol (provider-agnostic)

## Purpose

Local workflow events are the machine-readable timeline of the sentinel loop.
The board remains the human command center; issue evidence files remain the
close gate; this JSONL spool lets Hermes, dashboards, and future agents observe
what happened.

## Emitter

```bash
.scripts/scrum-master/bin/emit-event.py <event_type> --field key=value [...]
```

Appends to `_bmad-output/implementation-artifacts/bloodbank-events.jsonl`
(git-ignored dev spool) using the Hermes CloudEvents envelope shape. Event types
use the project repo lane `bloodbank.v1.repo.<repo>.<entity>.<action>`, where
`<repo>` comes from `role.yaml`.

## Event types

| Event type | When | Required data |
| --- | --- | --- |
| `…repo.<repo>.issue.evidence.created` | Evidence file created | `issue`, `evidence_file` |
| `…repo.<repo>.issue.gate.passed` | Close gate passes | `issue`, `evidence_file` |
| `…repo.<repo>.issue.gate.failed` | Close gate fails | `issue`, `evidence_file` |
| `…repo.<repo>.issue.autonomous_review.decided` | Independent reviewer decides a human-review-only ticket on the operator's behalf | `issue`, `decision` (`closed`/`held`), `drift`, `close_gate`, `reviewer_agent`, `evidence_file`, `report_file` |
| `…repo.<repo>.issue.truthcheck.flagged` | Status/evidence mismatch found | `issue`, `reason` |

## Rules

- Emit events for consequential transitions; do not invent types casually.
- Event emission never replaces the board update or issue evidence.
- If emission fails, continue and report the trail is incomplete.
- Autonomous closure is legitimate only when
  `issue.autonomous_review.decided` is emitted with `decision=closed` and
  `close_gate=pass` by `bin/issue-autonomous-review.sh`. That script will not
  emit a `closed` decision while the close gate fails or drift is `significant`.

## Canonical BloodBank

These project-local repo-lane events are BloodBank-*style*. Promote a type to a
canonical NATS subject only after adding its JSON Schema to the BloodBank schema
tree and passing validation. The local emitter does not require NATS so the loop
stays reliable offline.
