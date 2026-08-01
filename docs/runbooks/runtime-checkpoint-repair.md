# Historical runbook: retired checkpoint architecture

Status: Historical evidence only — **do not execute**

This page formerly described recovery for an older per-agent persistence
design. That design is not the current Hermes template contract, and its
commands have intentionally been removed so an operator cannot mistake them
for a supported repair procedure.

Current agents use an ignored, pure-local
`agents/hermes/<role>/runtime/` directory. Provisioning refuses stale project
gitlinks or mappings at that exact path and preserves all existing runtime
bytes. The heartbeat performs board reconciliation; it is not the runtime
backup mechanism.

For current recovery and retirement procedures, use
[Operations](../operations.md#back-up-and-restore-an-agent). In particular:

- configure and verify an encrypted filesystem backup for the exact runtime
  path;
- treat Hindsight as recovery for only the memories/events previously written
  to its bank;
- treat the secret manager as recovery for only credentials deliberately
  stored there;
- retire services and profile links without removing runtime data; and
- preserve retired runtime data because this release intentionally ships no
  automated purge; any future purge requires a separately reviewed path-safe
  tool and a verified off-host backup.

The earlier procedure remains recoverable from repository history for incident
forensics. Repository history is evidence, not an active operator interface.
