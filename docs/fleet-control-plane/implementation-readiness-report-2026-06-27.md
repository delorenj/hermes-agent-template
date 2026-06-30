---
stepsCompleted: [1, 2, 3, 4, 5, 6]
inputDocuments:
  - docs/fleet-control-plane/prd.md
  - docs/fleet-control-plane/architecture.md
  - docs/fleet-control-plane/epics-and-stories.md
workflowType: implementation-readiness
project_name: Fleet Control Plane
date: 2026-06-27
status: ready-with-known-blockers
---

# Implementation Readiness Assessment Report

**Date:** 2026-06-27
**Project:** Fleet Control Plane

## Summary

The implementation plan is coherent and ready to begin, with known blockers that
should be handled in the first implementation epic rather than deferred.

The strongest architecture choice is the hybrid model:

- n8n coordinates Fleet workflows visually.
- `pjangler` remains the control-plane and validation source.
- systemd keeps local survival behavior in place.

## Document Inventory

- PRD: `docs/fleet-control-plane/prd.md`
- Architecture: `docs/fleet-control-plane/architecture.md`
- Epics and stories: `docs/fleet-control-plane/epics-and-stories.md`
- Existing operational docs: `docs/architecture.md`, `docs/operations.md`

## Alignment Review

### PRD to Architecture

Pass. Each major requirement maps to an architecture decision:

- Fleet contract maps to ADR-001.
- `pjangler` control plane maps to ADR-002.
- n8n orchestration maps to ADR-003.
- hybrid heartbeat maps to ADR-004.
- managed exceptions map to ADR-005.

### Architecture to Epics

Pass. The epics cover the required implementation sequence:

1. Define contract and schemas.
2. Make template sources visible and gated.
3. Add status and reconcile.
4. Promote heartbeat v2 and generate n8n workflows.
5. Harden rollout.

### Risk Coverage

Mostly pass. The plan explicitly covers:

- n8n outage fallback.
- dirty vendored templates.
- stale registry paths.
- ambiguous profile/runtime state.
- workflow validation before creation.
- managed exceptions.

Remaining risk: exact n8n workflow node parameters must be validated during
implementation using the n8n workflow SDK before creating live workflows.

## Known Blockers to Resolve First

1. Dirty vendored `pjangler/templates/hermes-agent` currently contains heartbeat
   v2 changes that are not cleanly promoted into `hermes-agent-template`.
2. Current `hermes-agent-template` clean tree still describes checkpoint timers,
   while pjangler parity expects heartbeat timers.
3. `~/.config/hermes-agent-template/config.toml` has a missing fallback scaffold
   path.
4. `~/.hermes/agents-registry.yaml` contains stale Hermes path metadata.
5. Some profiles exist outside registry coverage and need managed exception
   classification.
6. At least one generated role manifest has invalid model fields.

## Readiness Decision

Status: ready to implement Epic 1.

Do not start live n8n workflow creation until these gates are available:

- Schema validation exists.
- Template status reports clean/dirty state.
- Fleet status can run read-only against the live registry.
- n8n workflow export can be validated without creating remote workflows.

## First Implementation Slice

The first slice should be deliberately boring:

1. Add `fleet-contract.yaml` draft and schema.
2. Add manifest schemas.
3. Add `pj templates status --json`.
4. Add dirty vendored template detection.
5. Add `pj fleet status --json` with read-only checks only.

This gives immediate visibility and reduces risk before any mutating reconcile
or n8n creation work begins.

## Implementation Exit Gates

- All schema tests pass.
- `pj templates status --json` reports no unclassified dirty template state.
- `pj fleet status --json` reports all managed drift with stable identifiers.
- Managed exceptions are explicitly recorded.
- `pj fleet reconcile` defaults to dry-run.
- n8n workflow code validates before creation.
- Existing docs are updated to reflect heartbeat v2 or checkpoint legacy mode.

