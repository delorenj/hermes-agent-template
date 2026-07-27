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
`[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?`, with credential-shaped
substrings anywhere in the normalized identity (`xox*`, live/test payment
keys, Google/AWS access keys, and GitHub tokens) rejected before
fingerprinting. Provider and its complete bound
configuration come only from `.project.json.ticket_provider`. Input has exactly `run_id`,
`correlation_id`, `source_issue`, `local_tracking_reference`, `decisions`,
`protected_evidence_refs`, and `sanitization`. Set `run_id` once per invocation
and reuse it only for that invocation's retry; use the event correlation ID or
`run_id` for `correlation_id`.

The exact standard Draft 2020-12 contract is
`.scripts/sentinel/schemas/run-retro.v8.schema.json`, schema
`hermes.run-retro` version `8`. It uses no custom assertion keywords: standard
validation and runtime validation accept the same serialized JSON documents.
The JSON stores only canonical closed-shape intent plus routing; duplicated
target, fingerprints, marker, and body are not serialized because their
computed equality cannot be expressed portably in Draft 2020-12.
Draft `integer` semantics accept any finite mathematically integral JSON
number (including an integral decimal representation); generated
`routing.updated_at_epoch_us` values use integer notation and remain bounded.
Every schema/runtime string pattern uses the ECMAScript actual-end guard
`$(?![\s\S])`, so a final newline is rejected identically.
Only the closed `routing` result is mutable:
`status`, `error_category`, `updated_at_epoch_us`, and the closed
`proof.{status,transition_id}` shape. Prepared or failed routing requires
`unverified|null`; `posted|already_present|no_target_issue` requires
`verified|<canonical-rfc-uuid>`. The immutable operator flag is true
for a null source or external/template/fleet scope and false only for a
source-bound repo-local improvement. No create-issue operation is available;
`local_tracking_reference` is a canonical provider issue ID naming only an
existing local ticket, or null.

Fingerprint preimages are exact UTF-8 lines with one LF after every line,
including the last, and no other separators:

- `artifact_fingerprint`: SHA-256 of `hermes.run-retro.artifact`, `8`,
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
is closed JSON. Initial publication writes a unique exclusive temp in the held
bindings directory, fsyncs and validates it, links it to the final name without
replacement, fsyncs the final file and directory, and removes the temp. A crash
can leave only an ignorable unique temp, never a zero-byte final-name poison;
retry validates or publishes the same immutable binding. A retry that finds the
valid existing final binding fsyncs that file and the bindings directory,
validates it again, and revalidates the bound directory identities before
reuse. Final delivery atomically replaces that binding with the full
canonical final-document digest
and byte-equal `routing.proof.transition_id`. Thus a hand-edited terminal status
or proof cannot satisfy `--final`. Its exact fields are
`schema=hermes.run-retro.binding`, `schema_version=1`, `immutable_sha256`,
`final_document_sha256`, and `transition_id`. `immutable_sha256` hashes
canonical UTF-8 JSON plus one final LF for the immutable artifact view;
`final_document_sha256` hashes the same canonical encoding of the complete
finalized artifact. Same run plus same immutable content reuses one path; same run plus
changed content returns `stalled` without overwrite or comment; distinct runs
use distinct paths; identical cross-run improvements share one marker and body.

`prepare`, delivery, and finalization use one descriptor-anchored repository
lifetime: hold the parent and repository descriptors, read `.project.json`,
then hold the exact retro and bindings directory descriptors, provider script,
provider configuration, and locks through finalization without reopening
configuration or executable content by pathname. Revalidate the bound
repository entry before every external
effect; root replacement is `unsafe_artifact_path` and performs no board
call. Traverse
every `_bmad-output/implementation-artifacts/run-retros` component with
descriptor-relative `O_NOFOLLOW` operations that reject symlinks. The
unprivileged threat model trusts same-OS-UID peer processes. It rejects
symlinks, replacement path components or trees, stale identities detectable
before mutation, and untrusted repository content. Revalidate the bound
repository, retro, and bindings directory identities before every mutation and
after opening an empty `O_EXCL` temp but before writing data. Every binding
create/link/replace uses only its already-held `bindings_fd` plus a bare
filename; every artifact create/link/replace uses only its already-held
`retro_fd` plus a bare filename. A detected root/store/bindings replacement
returns `unsafe_artifact_path` before mutation. This unprivileged controller
does not claim to prevent an independent trusted same-UID peer from renaming an
already-open directory inside the final syscall window; privileged
immutable/mount helpers and trusted mutation daemons are deferred. A new
artifact uses file fsync, validation, no-replace link, final-file fsync,
parent-directory fsync, and parse/read-back. Updates use a unique exclusive
temp, file fsync, atomic
replace, final-file and directory fsync, and read-back. Never overwrite corrupt
or mismatched immutable content. Stdin, input JSON, artifact JSON, provider
stdout/stderr, and HTTP bodies have fixed byte limits; overflow returns a
sanitized declared failure.

Only after durable `prepared|reused`, call exactly:

```bash
tp ensure_comment <artifact-fingerprint>
```

The public `run-retro.py finalize` surface always returns
`untrusted_finalization`; caller-supplied provider JSON can never produce a
terminal proof. `deliver` alone creates an in-memory, artifact-fingerprint and
immutable-digest-bound transition after the bound provider execution (or
internal null-source decision), HMAC-seals its canonical result with a
process-private key, and consumes it exactly once before mutation. The
transition ID written to the artifact and final binding comes from that sealed
one-shot evidence. It cannot be replayed, split across artifacts, or
self-attested through a result file; a crash before consumption requires a
fresh idempotent delivery lookup.

This operation accepts no provider, issue, marker, or body argument. It reloads
the artifact and derives the exact prepared provider, source/target, marker, and
body before any external side effect; `TICKET_PROVIDER` cannot redirect it.
Null source records `no_target_issue` with no board call. Otherwise a bounded
cross-user host-global cross-run lock binds the exact Linux abstract Unix-socket
name `NUL + hermes.run-retro.comment-lock.v1. +
<64-hex-comment-fingerprint>`. The namespace has no writable filesystem path,
directory, key file, symlink, or permission race; it persists no repository,
credential, source, body, or protected value. Equal logical comment keys
contend and different keys have different names, so unrelated deliveries do
not serialize. Acquisition is nonblocking with a finite deadline (maximum 300
seconds); an artifact-lock timeout returns `stalled`, while a comment-lock
timeout records `failed|response_unknown`, in either case without launching the
provider. Repository copies, replacements, and distinct UIDs in the host
network namespace therefore share exactly one exhaustive-lookup/at-most-once-post
lock domain.

The provider script and controller source are opened by descriptor, receive
only the already-bound configuration, and execute from inherited descriptors
through Python/shell stdin. A detached trusted supervisor—not the delivery
controller—owns the keyed socket for the complete provider lifetime, so
controller `SIGKILL` cannot release exclusion. On Linux it accepts only a
root-owned, non-group/world-writable Bubblewrap executable, creates a private
PID namespace with `--unshare-pid --as-pid-1`, and blocks the adapter before
execution until a bounded typed `--info-fd` response has yielded the host PID
and an open pidfd for namespace PID 1. Success exits PID 1; timeout, overflow,
or failure signals that pidfd. Either transition makes the kernel destroy and
reap the complete namespace—including `setsid`, reparented double-fork, and
all-descriptor-closing descendants—before finalization and before the keyed
socket closes. Missing/invalid containment or pre-launch inventory fails closed
on a finite deadline with no provider launch. After launch, the already-held
pidfd is used for bounded terminal cleanup. If pidfd signaling itself fails
after `pidfd_open`, cleanup falls through to bounded
Bubblewrap process-group termination and confirmed reap; it never returns while
that provider subtree can still act. The controller deadline budgets lock
acquisition, provider execution, containment-info acquisition, and every
shutdown/reap window. Failure to prove containment completion fails closed
before finalization or socket release. Unsupported
platforms perform no board side effect. Provider temporary storage honors
`TMPDIR`; artifact and comment lock acquisition are both bounded.
Plane/Trello curl controllers reserve teardown time inside their hard request
or operation deadline, signal the original process group even after its leader
exits, wait/reap the leader, and verify the group no longer exists before
returning or releasing the keyed delivery lock. Overflow, timeout, or a
surviving descendant fails closed.

Plane and Trello `resolve_issue_id` HTTP bodies go through an independent
streaming byte limiter before bounded private
`TMPDIR` files and undergo strict UTF-8/JSON/type/canonical-ID validation before
any shell variable can normalize bytes; NUL, malformed, oversized, null, or
numeric identities fail closed even when curl predates reliable
`--max-filesize` enforcement for chunked or unknown-length responses. Plane uses the supported
`/work-items/{id}/comments/` list/create endpoints and
exhausts `per_page=100` plus `cursor` pages. Every HTTP-200 lookup envelope must
have typed `results`, `count`, `total_results`, `next_page_results`, and
`next_cursor`; each result has a canonical string UUID `id` and string
`comment_html`. Pin `total_results` from page one, require
count/cumulative-total agreement, unique item IDs, and a new typed cursor on
every continuing page, with a 2,000-comment safety bound. Plane issue
resolution has one operation-wide deadline across the direct lookup and all
pages, and rejects any repeated cursor cycle, including A-B-A.
`next_page_results=false` is authoritative terminal state for live work-item
and comment collection envelopes; a typed non-empty terminal `next_cursor`
such as `100:1:0` is ignored and is never followed. Cursor validation and cycle
detection apply to continuing pages only. Collection drift,
duplicate IDs/cursors, malformed or oversized envelopes, and incomplete final
pages are `failed|lookup_failed` with no post. A successful POST must return a
canonical string UUID `id`; null,
numeric, malformed, oversized, or noncanonical responses are
`failed|response_unknown`. Retry rescans and records `already_present` if the
marker landed. Provider and target in every result must exactly match the
prepared values. Terminal `posted|already_present|no_target_issue` is monotonic:
delayed failure finalization cannot overwrite it. Linear and Trello apply the
same fixed HTTP-body/time bounds and strict canonical string-ID checks; numeric,
null, malformed, noncanonical, or oversized lookup/post responses fail closed.

## Final retro checkpoint

Do not finish or go idle until
`run-retro.py validate --repo-root "$REPO_ROOT" --artifact-fingerprint
<fingerprint> --final` succeeds after bounded parse/read-back validation of the
standard schema, filename, immutable binding, canonical identities, derived
fingerprints/body, closed summary grammar, epoch-microsecond bounds,
sanitization, target/source result equality, the closed routing proof, and the
full-document digest/transition bound by the finalized binding. A write,
fsync, lookup, validation, corrupt-artifact, wrong-target, timeout, overflow, or
immutable-input failure makes the pass `stalled`; record only its safe category.
A stored `failed` delivery remains retryable evidence but never satisfies
`--final`; only `posted|already_present|no_target_issue` passes this checkpoint.
The issue close gate applies the same helper to every run-retro artifact and
requires `--final` for each one, so editing a status/proof without
the bound final transition blocks closure. When `REPO_ROOT` is omitted, an
absolute gate invocation resolves the Git repository containing its installed
`ROLE_DIR`, never the caller's current directory; an explicit root must resolve
to a repository containing `.project.json`.
