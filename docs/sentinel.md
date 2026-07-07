# The PM's heartbeat sentinel

The PM runs a provider-agnostic ticket sentinel out-of-band on its heartbeat
timer: a board-reconciliation pass with an autonomous adversarial review (act,
do not wait), fused with a gated runtime checkpoint into a single timer tick.
The full documentation lives in a dedicated handoff guide.

This page is kept as a stable pointer so existing links keep working.

## Where the docs live now

- [Sentinel handoff overview](sentinel/README.md): start here.
- [Architecture](sentinel/architecture.md): the heartbeat loop, the provider
  abstraction, and the autonomous adversarial review.
- [Providers](sentinel/providers.md): the adapter contract and how to add or
  verify a provider.
- [Development guide](sentinel/development.md): editing, validating,
  provisioning, propagating, and the roadmap.
