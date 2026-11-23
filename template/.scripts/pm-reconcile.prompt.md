# PM reconciliation: read-only checkpoint

Automatic board writes are DISABLED unconditionally. `--apply` always refuses,
before reading policy, a plan, a lease, or providers. Do not run Pilot writes
manually to bypass this gate. No configuration or environment flag enables it.
Pilot/Plane does not yet offer the provider-enforced expected-version and
human-absence mutation predicates needed to preserve `lifecycle:human` against
an outside writer. A local lease or another read cannot close that race.
This is NOT delivery, installation readiness, or production acceptance.

The snapshot is a proposal work list, not approval. Inspect independent Backlog
in priority order even when other work is held; preserve real holds, unanswered
questions, busy turns, review and human tickets. Krebs alone dispatches and owns
`agent:working`. Never infer permission from age, agent prose, or a marker prefix.

For an intake proposal, cite `authority:`, `restriction:`, `criteria:`,
`dependencies:`, `files:`, and `launch-cut:` evidence. A resolved-hold proposal
needs the original question/dependency and `recheck:` proof. Failed-turn proposals
need `diagnosis:` and `recovery:`. Reserved spend/publication/legal/credential
and product decisions remain reserved. Ambiguous sources stay unknown.

Optional read-only evaluation: save a local runtime JSON plan then use
`--evaluate <absolute-plan-path>`. A successful result says `proposal_only`, never
applied. Shape:
{"writes":[{"op":"intake|resolve|failed","ref":"BOARD-1","scope":"repo:slug or (unlabeled)","expected":"digest printed by brief","evidence":"authority: ... restriction: ... criteria: ... dependencies: ... files: ... launch-cut: ...","hold":""}],"note":"exact excluded-candidate reasons"}

A no-write plan is `{"writes":[],"note":"specific reasons"}`. Evaluation prints
its note locally; it does not create a receipt, consume a plan, update a roadmap,
or touch a lease. Estimated provider cost is THREE mutations per intake (move
PATCH + evidence POST + triage PATCH) and TWO per hold recovery, maximum six.
Those are proposal estimates, not a working write adapter or runtime cost bound.
Reject/disposition, paging fairness, global read/deadline bounds, atomic confined
receipts and exact lease handoff remain deferred. No comment echo is ignored
without actor/receipt proof; this checkpoint conservatively retains all comments.
