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
third is `repo-local|external|template|fleet`. The closed safe-summary vocabulary
is the exact ASCII template
`signal=<safe-signal>; action=<safe-action>`. Safe signals are
`slow_feedback|manual_rework|flaky_validation|unclear_contract|missing_capability|dependency_delay|coordination_gap|environment_drift|documentation_gap|review_rework|no_material_friction|other_process_friction`.
Safe actions are
`automate_check|clarify_contract|add_test|improve_tooling|update_documentation|isolate_dependency|tighten_review|improve_coordination|stabilize_environment|retain_current_process|operator_followup`.
No arbitrary text is accepted in any persisted field, summary, routing error,
or comment. Never copy tokens, credentials, raw logs, customer/PII, private
paths, or other protected material into an artifact or comment; select only the
closest safe signal/action. Reference protected evidence only by an opaque
`evidence:<canonical-rfc-uuid>` token whose value contains no evidence text,
path, log content, or customer data.

Set `source_issue` to the single issue whose work produced the improvement,
not a related issue. Use the canonical `id` from `tp list_issues`, or resolve a
provider reference with `tp resolve_issue_id <reference>` before preparing the
intent. Plane and Linear IDs are lowercase RFC UUID text with version 1-8 and
variant `[89ab]`; Trello IDs are lowercase 24-hex. Inputs use Unicode NFKC
normalization, trimming, control rejection, and provider validation before storage in
canonical form. Persist `source_issue` once. It is the only allowed target:
every provider result must return that byte-equal value. If no single source
exists, it is null; never invent or substitute a target. `run_id` and
`correlation_id` are canonical lowercase RFC UUIDs.

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

The exact standard Draft 2020-12 contract is
`.scripts/sentinel/schemas/run-retro.v7.schema.json`, schema
`hermes.run-retro` version `7`. It uses no custom assertion keywords: standard
validation and runtime validation accept the same serialized JSON documents.
The JSON stores only canonical closed-shape intent plus routing; duplicated
target, fingerprints, marker, and body are not serialized because their
computed equality cannot be expressed portably in Draft 2020-12.
`routing.updated_at_epoch_us` is a bounded integer epoch-microsecond value. Only
`routing.status`, `routing.error_category`, and
`routing.updated_at_epoch_us` are mutable. The immutable operator flag is true
for a null source or external/template/fleet scope and false only for a
source-bound repo-local improvement. No create-issue operation is available;
`local_tracking_reference` is a canonical provider issue ID naming only an
existing local ticket, or null.

Fingerprint preimages are exact UTF-8 lines with one LF after every line,
including the last, and no other separators:

- `artifact_fingerprint`: SHA-256 of `hermes.run-retro.artifact`, `7`,
  canonical `repo`, `run_id`.
- `comment_fingerprint`: SHA-256 of `hermes.run-retro.comment`, `6`, canonical
  `repo`, canonical `provider`, canonical `source_issue` or `no_target_issue`,
  `what_hurt.category`, `what_hurt.summary`, `what_should_change.category`,
  `what_should_change.summary`, `fix_scope`, and lowercase `true|false` for
  immutable `operator_action_required`.
- The marker is `[run-retro-comment:<comment_fingerprint>]`.

Artifact identity is run-scoped and independent of mutable/content-derived
values. Comment identity and the exact comment body are derived only from
immutable closed-shape intent, so one marker maps to one body in every retry
state. An adjacent no-replace `.bindings/<artifact_fingerprint>.sha256` record
binds the immutable serialized intent without adding a nonportable computed JSON
field. Same run plus same immutable content reuses one path; same run plus
changed content returns `stalled` without overwrite or comment; distinct runs
use distinct paths; identical cross-run improvements share one marker and body.

`prepare`, delivery, and finalization use one descriptor-anchored repository
lifetime: read `.project.json`, bind the store and provider script, hold the
locks, and finalize without reopening the repository by pathname. Traverse
every `_bmad-output/implementation-artifacts/run-retros` component with
descriptor-relative `O_NOFOLLOW` operations that reject symlinks. Revalidate the
bound directory before every create and after opening an empty `O_EXCL` temp but
before writing data; if any component was relocated, create no new artifact or
lock entry and return only `unsafe_artifact_path`. A new artifact uses file
fsync, validation, no-replace link, final-file fsync, parent-directory fsync,
and parse/read-back. Updates use a unique exclusive temp, file fsync, atomic
replace, final-file and directory fsync, and read-back. Never overwrite corrupt
or mismatched immutable content. Stdin, input JSON, artifact JSON, provider
stdout/stderr, and HTTP bodies have fixed byte limits; overflow returns a
sanitized declared failure.

Only after durable `prepared|reused`, call exactly:

```bash
tp ensure_comment <artifact-fingerprint>
```

This operation accepts no provider, issue, marker, or body argument. It reloads
the artifact and derives the exact prepared provider, source/target, marker, and
body before any external side effect; `TICKET_PROVIDER` cannot redirect it.
Null source records `no_target_issue` with no board call. Otherwise a safe
cross-run lock keyed by provider, source, and marker covers exhaustive lookup
plus at-most-once post. The provider script is opened by descriptor before
execution. Its process starts a new session/process group, inherits the lock
descriptor for controller-SIGKILL safety, and on timeout or output overflow the
controller terminates and reaps the full group before releasing the lock.

Plane uses the supported `/work-items/{id}/comments/` list/create endpoints and
exhausts `per_page=100` plus `cursor` pages. Every HTTP-200 lookup envelope must
have typed `results`, `count`, `total_results`, `next_page_results`, and
`next_cursor`; each result has a canonical string UUID `id` and string
`comment_html`. Pin `total_results` from page one, require
count/cumulative-total agreement, unique item IDs, and a new typed cursor on
every continuing page, with a 2,000-comment safety bound. Collection drift,
duplicate IDs/cursors, malformed or oversized envelopes, and incomplete final
pages are `failed|lookup_failed` with no post. A successful POST must return a
canonical string UUID `id`; null,
numeric, malformed, oversized, or noncanonical responses are
`failed|response_unknown`. Retry rescans and records `already_present` if the
marker landed. Provider and target in every result must exactly match the
prepared values. Terminal `posted|already_present|no_target_issue` is monotonic:
delayed failure finalization cannot overwrite it.

## Final retro checkpoint

Do not finish or go idle until
`run-retro.py validate --repo-root "$REPO_ROOT" --artifact-fingerprint
<fingerprint> --final` succeeds after bounded parse/read-back validation of the
standard schema, filename, immutable binding, canonical identities, derived
fingerprints/body, closed summary grammar, epoch-microsecond bounds,
sanitization, target/source result equality, and final routing status. A write,
fsync, lookup, validation, corrupt-artifact, wrong-target, timeout, overflow, or
immutable-input failure makes the pass `stalled`; record only its safe category.
