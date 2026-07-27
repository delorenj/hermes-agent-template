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
third is `repo-local|external|template|fleet`. Normalize repository and
identifier inputs with Unicode NFKC. The closed safe-summary vocabulary is the
exact ASCII template
`signal=<safe-signal>; action=<safe-action>`. Safe signals are
`slow_feedback|manual_rework|flaky_validation|unclear_contract|missing_capability|dependency_delay|coordination_gap|environment_drift|documentation_gap|review_rework|no_material_friction|other_process_friction`.
Safe actions are
`automate_check|clarify_contract|add_test|improve_tooling|update_documentation|isolate_dependency|tighten_review|improve_coordination|stabilize_environment|retain_current_process|operator_followup`.
No arbitrary text is accepted in a summary or routing error. Never copy tokens,
credentials, raw logs, customer/PII, private paths, or other protected material
into an artifact or comment; select only the closest safe signal/action and
reference protected evidence with an opaque `evidence:<id>` or repo-relative
reference without `..` segments.

Set `source_issue` to the single issue whose work produced the improvement,
not a related issue. Use the canonical `id` from `tp list_issues`, or resolve a
provider reference with `tp resolve_issue_id <reference>` before preparing the
intent. Plane and Linear IDs are lowercase RFC UUID text with version 1-8 and
variant `[89ab]`; Trello IDs are lowercase 24-hex. Inputs are Unicode
NFKC-normalized, trimmed, control-free, provider-validated, then stored in
canonical form. Stored `source_issue` and `target_issue` must themselves be
canonical and byte-equal. If no single source exists, both are null; never
invent or substitute a target.

Persist the complete immutable prepared intent **before any board side
effect** with:

```bash
.scripts/sentinel/bin/run-retro.py prepare --repo-root "$REPO_ROOT" --intent -
```

Repository identity comes only from `.project.json.project_name`: Unicode
NFKC, trim, casefold, ASCII, then
`[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?`. Provider comes only from
`.project.json.ticket_provider.type`. Input has exactly `run_id`,
`correlation_id`, `source_issue`, `local_tracking_reference`, `decisions`,
`protected_evidence_refs`, and `sanitization`. Set `run_id` once per invocation
and reuse it only for that invocation's retry; use the event correlation ID or
`run_id` for `correlation_id`.

The exact Draft 2020-12 contract is
`.scripts/sentinel/schemas/run-retro.v6.schema.json`, schema
`hermes.run-retro` version `6`. Immutable fields include canonical
repository/provider/source/target identity, decisions,
`operator_action_required`, marker, and exact `comment_body`. Only
`routing.status`, `routing.error_category`, `routing.error_summary`, and
`routing.updated_at` are mutable. The immutable operator flag is true for a
null source or external/template/fleet scope and false only for a source-bound
repo-local improvement. No create-issue operation is available;
`local_tracking_reference` names only an existing local ticket or is null.

Fingerprint preimages are exact UTF-8 lines with one LF after every line,
including the last, and no other separators:

- `artifact_fingerprint`: SHA-256 of `hermes.run-retro.artifact`, `6`,
  canonical `repo`, `run_id`.
- `comment_fingerprint`: SHA-256 of `hermes.run-retro.comment`, `5`, canonical
  `repo`, canonical `provider`, canonical `source_issue` or `no_target_issue`,
  `what_hurt.category`, `what_hurt.summary`, `what_should_change.category`,
  `what_should_change.summary`, `fix_scope`, and lowercase `true|false` for
  immutable `operator_action_required`.
- The marker is `[run-retro-comment:<comment_fingerprint>]`.

Artifact identity is run-scoped and independent of mutable/content-derived
values. Comment identity is content-scoped and independent of run,
correlation, timestamps, and routing results. One marker therefore maps to
exactly one immutable body in every retry state. Same run plus same immutable
content reuses one path; same run plus changed content returns `stalled`
without overwrite or comment; distinct runs use distinct paths; identical
cross-run improvements share one marker and body.

`prepare` and retry finalization serialize per artifact with advisory locks
opened without truncation. Open the repository once and traverse every
`_bmad-output/implementation-artifacts/run-retros` component with
descriptor-relative `O_NOFOLLOW` operations that reject symlinks; create files,
locks, links, replaces, unlinks, and fsyncs only relative to those anchored
descriptors, and
revalidate the bound directory identity after lock acquisition. A new artifact
uses a unique `O_EXCL` temp, file fsync, validation, no-replace link, final-file
fsync, parent-directory fsync, and parse/read-back. Updates use a unique
exclusive temp, file fsync, atomic replace, final-file and directory fsync, and
read-back. Never overwrite corrupt or mismatched immutable content, and never
follow or race a path outside the repository.

Only after durable `prepared|reused`, call exactly:

```bash
tp ensure_comment <artifact-fingerprint>
```

This operation accepts no provider, issue, marker, or body argument. It reloads
the artifact and binds the exact prepared provider, source/target, marker, and
body before any external side effect; `TICKET_PROVIDER` cannot redirect it.
Null source records `no_target_issue` with no board call. Otherwise a safe
cross-run lock keyed by provider, source, and marker covers exhaustive lookup
plus at-most-once post. The provider subtree inherits that lock descriptor, so
controller death cannot release exclusivity while a provider is still posting.
Plane uses the supported
`/work-items/{id}/comments/` list/create endpoints and exhausts documented
`limit`/`offset` pages. Every successful lookup page must have exact typed
`results` and `total_results` fields, string `comment_html` values, and
consistent nonnegative pagination bounds; any malformed or ambiguous 200 is
`failed|lookup_failed` with no post. Lost response records
`failed|response_unknown`; retry
rescans and records `already_present` if the
marker landed. Provider and target in every result must exactly match the
prepared values. Terminal `posted|already_present|no_target_issue` is
monotonic: delayed failure finalization cannot overwrite it.

## Final retro checkpoint

Do not finish or go idle until
`run-retro.py validate --repo-root "$REPO_ROOT" --artifact-fingerprint
<fingerprint> --final` succeeds after parse/read-back validation of schema,
filename, canonical identities, both fingerprints, immutable agreement, closed
summary grammar, strict six-digit UTC `Z` timestamps, sanitization, target/source
equality, and final routing status. A write, fsync,
lookup, validation, corrupt-artifact, wrong-target, or immutable-input failure
makes the pass `stalled`; record only its safe category.
