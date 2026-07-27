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

Persist exactly one durable JSON artifact for this invocation at
`_bmad-output/implementation-artifacts/run-retros/<artifact_fingerprint>.json`.
The schema is `hermes.run-retro`, version `2`, and requires:
`schema`, `schema_version`, `artifact_fingerprint`, `comment_fingerprint`,
`run_id`, `correlation_id`, `repo`, `source_issue`,
`local_tracking_reference`, `decisions` (`what_hurt`, `what_should_change`,
`fix_scope` = `repo-local|external|template|fleet`),
`protected_evidence_refs`, `sanitization`, `operator_action_required`,
`comment` (`target_issue`, `comment_fingerprint_marker`, `status` =
`posted|already_present|failed|no_target_issue`, `failure_category`),
`recorded_at`, and `updated_at`. Set `run_id` once per sentinel invocation to
the triggering event ID when present; otherwise generate one UUID. Reuse that
same `run_id` for every retry of that invocation, but generate or accept a
different `run_id` for every distinct invocation. Use the triggering event
correlation ID when present; otherwise use `run_id` as `correlation_id`, and
keep it unchanged on retries. Set `repo` to the installing repository.
`recorded_at` is the first successful artifact-write time and is immutable;
`updated_at` is the latest successful atomic-write time. Both use RFC 3339 UTC.

Before fingerprinting, artifact writing, or commenting, sanitize the answers.
Never copy tokens, credentials, raw logs, customer data/PII, or private or
absolute paths into the artifact or a comment. Record only a safe category and
summary. `protected_evidence_refs` may contain opaque evidence IDs or
repo-relative protected references, never protected content or private paths.
Set `sanitization.status` to `sanitized` and list the omitted data categories.

Compute two separate identities after normalization and sanitization:

- `comment_fingerprint` is lowercase SHA-256 hex of exactly these seven UTF-8
  lines, in order: `hermes.run-retro.comment`, `2`, `repo`, `source_issue` (or
  the literal `no_target_issue`), `decisions.what_hurt`,
  `decisions.what_should_change`, and `decisions.fix_scope`.
- `artifact_fingerprint` is lowercase SHA-256 hex of exactly these four UTF-8
  lines, in order: `hermes.run-retro.artifact`, `2`, `run_id`, and
  `comment_fingerprint`.

For both preimages, terminate every line, including the last, with one LF byte
(`\n`); use no other separators or extra bytes. Set
`comment_fingerprint_marker` to
`[run-retro-comment:<comment_fingerprint>]`. The artifact fingerprint is
run-scoped and therefore creates a different path for distinct invocations even
when their sanitized content is identical. The comment fingerprint deliberately
excludes `run_id`, `correlation_id`, timestamps, and routing outcome so identical
sanitized improvements deduplicate across runs.

Route idempotently. If `source_issue` is null, do not invent a target or post a
comment; record `target_issue: null`, `status: no_target_issue`, and
`operator_action_required: true`. Otherwise, the source issue is the only
comment target: inspect it with `tp get_issue <source_issue>` and search its
comments for the exact comment fingerprint marker. If present, do not post
again and record `already_present`; if absent, post one sanitized summary plus
that marker with `tp comment` and record `posted` or `failed`. Set
`operator_action_required: true` for external/template/fleet scope, failed
delivery, or no target; include that explicit boolean in the artifact and
sanitized comment. The adapter has no create-issue operation. Never claim a
follow-up exists; `local_tracking_reference` may name only an already-existing
ticket on the installing repo's board, otherwise it is null.

Write the complete JSON to a temporary file in the artifact directory, flush,
sync, and close it, then parse it and validate every required field and enum.
Recompute both fingerprints from their exact preimages; require the artifact
filename stem to equal `artifact_fingerprint`, require
`comment_fingerprint_marker` to equal its derived marker, and require nonempty,
unchanged `run_id` and `correlation_id`. Atomically rename the validated file to
`<artifact_fingerprint>.json`, then reopen and parse the final path and repeat
schema/version, identity, routing-status, and sanitization validation.

A retry of the same invocation reuses only its valid matching
`<artifact_fingerprint>.json`: preserve `recorded_at` and all identity and
sanitized-content fields, inspect comments again, and atomically update only
`comment`, `operator_action_required`, and `updated_at`. If a previous post
succeeded but its response was lost, finding the marker records
`already_present`; if the marker is absent, retry `tp comment` and record
`posted` or `failed`. A distinct invocation must never reuse or overwrite a
prior run's artifact, even when `comment_fingerprint` matches; it writes its own
run-scoped path and uses the shared comment fingerprint only for comment
deduplication. An existing path that fails identity or parse validation makes
the pass `stalled` and must not be overwritten. Retain every valid run-scoped
artifact across loop cleanup.

## Final retro checkpoint

Do not finish the pass or go idle until the final artifact exists at
`_bmad-output/implementation-artifacts/run-retros/<artifact_fingerprint>.json`,
its parse/read-back validation proves the schema/version, run/correlation
identity, both recomputed fingerprints, filename, marker, sanitization, and
routing status, and its comment status is one of `posted`, `already_present`,
`failed`, or `no_target_issue`. A failed write or validation makes the pass
`stalled`; record that failure without exposing protected data.
