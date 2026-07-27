# Continuous ticket orchestration (sentinel engine)

Status: sentinel engine protocol (provider-agnostic)

## Invariant

If a ready ticket exists, exactly one implementation worker must be actively
moving it, or the sentinel records why none can. Prefer one live thread over
a quiet backlog. WIP limit: one active worker ticket.

The sentinel owns the watch loop. Workers (codex, opencode, copilot, …) own
implementation. The sentinel clears review-lane work via the autonomous
adversarial review (act, do not wait) protocol
(`autonomous-delegated-review.md`), and still does not write application code or
approve merges.

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
ticket blocked **only** on human review is NOT a stop condition — run it through
the independent adversarial review and act on the verdict immediately, no
waiting; and a dependent blocked **only** on a review-accepted feature is NOT
blocked); a worker is already active and healthy; or the next action needs
destructive git ops / production credentials / a paid action.

The loop never ends a pass with work parked waiting on the operator.

## Review and closure

1. Run ticket verification.
2. Run the close gate: `.scripts/sentinel/bin/issue-close-gate.sh <ISSUE>`.
3. Run the independent adversarial review — an adversarial microscope, with the
   reviewer agent NOT the implementer (`autonomous-delegated-review.md`). On a
   clean adversarial verdict the loop autonomously treats the ticket as done
   (leaving it in the review lane as the operator's deferred-QA queue) and moves
   on with no grace wait. A real finding holds it back to active.
4. Gate fail → leave open, record missing evidence.
5. Downstream regression rollback: if a later dependent proves a
   review-accepted feature is actually broken, move it back to active as a
   prerequisite of the dependent and record the rollback.

Board status is not proof. Repository evidence and the close gate are proof.

## Post-loop improvement (end-of-batch retro)

Immediately after the final board-status report, make exactly these three
decisions:

1. What hurt this batch?
2. What should change?
3. Is the fix repo-local or external/template/fleet?

Normalize each sanitized text answer by trimming it and collapsing internal
whitespace to one space. Set `source_issue` to the single issue whose work
produced the improvement; never select a merely related issue. If there is no
single source issue, set it to null.

Persist exactly one durable JSON artifact at
`_bmad-output/implementation-artifacts/run-retros/<fingerprint>.json`.
The schema is `hermes.run-retro`, version `1`, and requires:
`schema`, `schema_version`, `fingerprint`, `run_id`, `correlation_id`, `repo`,
`source_issue`, `local_tracking_reference`, `decisions` (`what_hurt`,
`what_should_change`, `fix_scope` = `repo-local|external|template|fleet`),
`protected_evidence_refs`, `sanitization`, `operator_action_required`,
`comment` (`target_issue`, `fingerprint_marker`, `status` =
`posted|already_present|failed|no_target_issue`, `failure_category`), and
`recorded_at`. Set `run_id` once per sentinel invocation to the triggering event
ID when present; otherwise generate one UUID and reuse it for every retry in
that invocation. Use the triggering event correlation ID when present;
otherwise use `run_id` as `correlation_id`. Set `repo` to the installing
repository and `recorded_at` to the artifact write time in RFC 3339 UTC.

Before fingerprinting, artifact writing, or commenting, sanitize the answers.
Never copy tokens, credentials, raw logs, customer data/PII, or private or
absolute paths into the artifact or a comment. Record only a safe category and
summary. `protected_evidence_refs` may contain opaque evidence IDs or
repo-relative protected references, never protected content or private paths.
Set `sanitization.status` to `sanitized` and list the omitted data categories.

Compute `fingerprint` as lowercase SHA-256 hex of this exact UTF-8,
newline-separated input after normalization: `hermes.run-retro`, `1`, `repo`,
`source_issue` (or the literal `no_target_issue`), `decisions.what_hurt`,
`decisions.what_should_change`, and `decisions.fix_scope`, in that order. Set
`fingerprint_marker` to `[run-retro:<fingerprint>]`.

Route idempotently. If `source_issue` is null, do not invent a target or post a
comment; record `target_issue: null`, `status: no_target_issue`, and
`operator_action_required: true`. Otherwise, the source issue is the only
comment target: inspect it with `tp get_issue <source_issue>` and search its
comments for the exact fingerprint marker. If present, do not post again and
record `already_present`; if absent, post one sanitized summary plus marker with
`tp comment` and record `posted` or `failed`. Set
`operator_action_required: true` for external/template/fleet scope, failed
delivery, or no target; include that explicit boolean in the artifact and
sanitized comment. The adapter has no create-issue operation. Never claim a
follow-up exists; `local_tracking_reference` may name only an already-existing
ticket on the installing repo's board, otherwise it is null.

Write the complete JSON to a temporary file in the artifact directory, flush
and close it, parse it and validate every required field, enum, and matching
fingerprint, then atomically rename it to `<fingerprint>.json`. Reopen and parse
the final path; verify schema/version, fingerprint, routing status, and
sanitization again. Retain this durable evidence across loop cleanup. Reuse a
valid existing artifact for the same fingerprint; update routing outcome only
by the same atomic replacement and never create a duplicate artifact.

## Final retro checkpoint

Do not finish the pass or go idle until the final artifact exists at the
deterministic path, its parse/read-back validation passes, and its comment status
is one of `posted`, `already_present`, `failed`, or `no_target_issue`. A failed
write or validation makes the pass `stalled`; record that failure without
exposing protected data.
