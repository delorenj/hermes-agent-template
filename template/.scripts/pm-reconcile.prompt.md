# PM reconciliation pass

The snapshot is the work list, not approval. Work at most six writes, and use the compact brief for candidates; a quiet or UNAVAILABLE snapshot means no plan. Never infer owner approval from an untagged comment, a ticket age, or agent-authored prose. Fetch exact authority/restrictions and original hold/dependency/turn evidence before proposing a decision. A plan is a proposal, not a bypass: the applier rechecks it.

Only owned Backlog requests with documented ordinary-source authority may enter Todo. For each, state `authority:` (who/what authorized this exact work) and `restriction:` (why no spend, credentials, publication, legal, or reserved product decision is implied). Do not promote speculative or planning-only requests. Krebs alone dispatches implementation.

Clear `lifecycle:needs-input` only after citing the actual answer and the original question, or `lifecycle:blocked` only after checking the original dependency and its current resolution. Cite `recheck:` in the evidence. Failed turns require `diagnosis:` (last-turn proof and no active/queued/detached turn) and `recovery:` (the named next action); never clear `agent:failed` based on age. Do not touch a `lifecycle:human` ticket at all, even with a comment. Any ambiguous authority, source or ownership: stop and report, do not improvise.

Write ONE new JSON plan to the path in the brief, then invoke --apply once. Shape:
{"writes":[{"op":"intake|resolve|failed","ref":"BOARD-1","scope":"repo:slug or (unlabeled)","expected":"64-character digest printed by brief","evidence":"authority: ... restriction: ... or recheck: ... or diagnosis: ... recovery: ...","hold":" for intake, else exact lifecycle:needs-input|lifecycle:blocked|agent:failed"}],"roadmap":"Only still-open work, under 4000 characters","note":"short pass result"}

The driver caps writes at six, checks exact current owner routing, fresh ticket state, liveness, lease and expected digest before each write, records a receipt and stops on the first refusal. If a receipt exists, create a NEW plan filename with fresh expected digests; never replay a partially applied plan. No board comment on a quiet pass.
