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
Post-loop routing additionally uses `tp resolve_issue_id` for canonical provider
identity and `tp ensure_comment` for exhaustive, serialized comment delivery.

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

Each of the first two answers is one sanitized `category` plus `summary`; the
third is `repo-local|external|template|fleet`. Normalize with Unicode NFKC,
trim, and collapse internal whitespace to one space; line breaks and tabs are
rejected and must be summarized first. Never put tokens,
credentials, raw logs, customer data/PII, or private/absolute paths in an
artifact or comment. Use only sanitized categories/summaries and opaque or
repo-relative protected evidence references; refer to protected evidence
without reproducing it.

Set `source_issue` to the single issue whose work produced the improvement,
not a related issue. Use the canonical `id` from `tp list_issues`, or resolve a
provider reference with `tp resolve_issue_id <reference>` before preparing the
intent. Plane and Linear canonical IDs are lowercase hyphenated UUIDs; Trello
canonical IDs are lowercase 24-hex card IDs. Reference inputs are Unicode
NFKC-normalized, trimmed, control-free, and provider-character validated. If no
single source exists, use null; never invent a target.

Persist the immutable prepared intent **before any comment/post side effect**
with:

```bash
.scripts/sentinel/bin/run-retro.py prepare --repo-root "$REPO_ROOT" --intent -
```

The command reads the canonical repository identity only from
`.project.json.project_name`: Unicode NFKC, trim, casefold, then require the
ASCII pattern `[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?`. It likewise reads the
provider only from `.project.json.ticket_provider.type`. Its input JSON has
exactly `run_id`, `correlation_id`, `source_issue`,
`local_tracking_reference`, `decisions`, `protected_evidence_refs`, and
`sanitization`. Set `run_id` once from the triggering event ID or a new UUID;
reuse it only for retries of that invocation. Set `correlation_id` once from
the event correlation ID or `run_id`. A different invocation gets a different
`run_id`.

The exact Draft 2020-12 schema is
`.scripts/sentinel/schemas/run-retro.v4.schema.json`. Schema
`hermes.run-retro` version `4` has immutable identity/content fields and only
these mutable routing-result fields: `routing.status`,
`routing.error_category`, `routing.error_summary`,
`routing.operator_action_required`, and `routing.updated_at`. `target_issue`
is immutable and must be null with a null `source_issue`, or byte-for-byte equal
to the canonical `source_issue`. Final status is exactly
`posted|already_present|failed|no_target_issue`; `prepared` is valid only
before routing. External/template/fleet scope, failed delivery, or no target
requires `operator_action_required: true`. There is no create-issue adapter
operation. `local_tracking_reference` may identify only an already-existing
local ticket or be null.

Identity preimages are exact UTF-8 lines with one LF after every line, including
the last, and no other separators:

- `artifact_fingerprint` is SHA-256 of four lines:
  `hermes.run-retro.artifact`, `4`, canonical `repo`, `run_id`.
- `comment_fingerprint` is SHA-256 of ten lines:
  `hermes.run-retro.comment`, `3`, canonical `repo`, canonical `provider`,
  canonical `source_issue` or `no_target_issue`, `what_hurt.category`,
  `what_hurt.summary`, `what_should_change.category`,
  `what_should_change.summary`, `fix_scope`.
- The marker is `[run-retro-comment:<comment_fingerprint>]`.

Artifact identity is run-scoped and independent of decisions, source issue,
correlation, and routing. Comment identity is content-scoped and independent
of run/correlation/timestamps. Thus same run plus same immutable content reuses
one path; same run plus changed sanitized content or comment fingerprint
returns `stalled` without overwrite or comment; different runs always have
different paths, while identical cross-run improvements share a comment marker.

`run-retro.py prepare` serializes each run with an exclusive lock. A new
artifact uses a unique `O_EXCL` temporary file, file fsync, validation, a
no-replace link into
`_bmad-output/implementation-artifacts/run-retros/<artifact_fingerprint>.json`,
final-file fsync, parent-directory fsync, and parse/read-back validation. Retry
updates use the same lock plus a unique exclusive temp, file fsync, atomic
replace, final-file and parent-directory fsync, and read-back. Never overwrite
a corrupt artifact or one with different immutable content.

Only after `prepare` confirms a durable `prepared` or matching `reused`
artifact may routing occur. For null source, finalize `no_target_issue` without
a board call. Otherwise obtain the sanitized body with
`run-retro.py comment-body`, then call only:

```bash
tp ensure_comment <canonical-source-id> <marker> "<sanitized-body>"
```

`ensure_comment` takes a cross-run lock keyed by provider, canonical issue, and
marker; exhaustively paginates comments (including every Plane cursor page);
posts at most once while still locked; and returns
`posted|already_present|failed`. A lookup/pagination failure returns
`failed`/`lookup_failed` and performs no post. A lost post response returns
`failed`/`response_unknown`; retrying re-scans all comments and records
`already_present` if the post landed. A lock/serialization failure returns
`failed`/`serialization_failed` and performs no post. Finalize the returned JSON with
`run-retro.py finalize`; it rejects a target other than the immutable source
and updates only the five authorized routing fields.

## Final retro checkpoint

Do not finish or go idle until
`run-retro.py validate --repo-root "$REPO_ROOT" --artifact-fingerprint
<fingerprint> --final` succeeds after parse/read-back validation of schema,
filename, canonical identities, both fingerprints, immutable agreement,
sanitization, target/source equality, and final routing status. A write, fsync,
lookup, validation, corrupt-artifact, wrong-target, or immutable-input failure
makes the pass `stalled`; record only its safe category.
