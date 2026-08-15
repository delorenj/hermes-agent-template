---
workflowType: fleet-control-plane-artifacts
date: 2026-06-27
status: draft-ready
source: adapted-bmad
---

# Fleet Control Plane Implementation Artifacts

This folder captures the implementation plan for tightening the Fleet without
collapsing its repos into one codebase.

The Fleet remains a set of independently useful parts:

- `CommonProject` owns the base project scaffold.
- `hermes-agent-template` owns agent-role provisioning.
- `~/.hermes` owns live runtime state.
- `~/.local/share/hermes-agent/releases/<commit>` owns each immutable Hermes engine checkout and binary.
- `pjangler` becomes the explicit Fleet control plane.
- n8n becomes the visual orchestration layer, backed by local systemd fallback.

## Artifacts

- [prd.md](./prd.md) - Functional and non-functional requirements.
- [architecture.md](./architecture.md) - Technical architecture and ADRs.
- [epics-and-stories.md](./epics-and-stories.md) - Implementation backlog with acceptance criteria.
- [implementation-readiness-report-2026-06-27.md](./implementation-readiness-report-2026-06-27.md) - Readiness assessment and launch gates.

## Adapted BMAD Note

The formal BMAD workflow expects a project-local `_bmad` runtime, PRD, and
step-by-step user confirmations. This repo does not currently contain `_bmad`.
These artifacts preserve the BMAD intent and structure while using the live
architecture review and advanced elicitation decisions as input.
