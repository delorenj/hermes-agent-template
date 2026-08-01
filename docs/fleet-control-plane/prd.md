---
stepsCompleted: [1]
inputDocuments:
  - docs/architecture.md
  - docs/operations.md
  - README.md
  - /home/delorenj/code/pjangler/src/commands/hermes/RunCopierTemplate.ts
  - /home/delorenj/code/pjangler/src/parity/index.ts
  - /home/delorenj/.hermes/agents-registry.yaml
workflowType: prd
project_name: Fleet Control Plane
date: 2026-06-27
status: draft-ready
---

# Fleet Control Plane PRD

## Problem

The Fleet is composed of valuable independent repos and runtime surfaces, but
the binding contract between them is implicit and drifting.

Current symptoms include:

- `pjangler` is already the de facto control plane, but its role is not stated
  as an architectural contract.
- `pjangler/templates/hermes-agent` can carry dirty vendored template changes
  that differ from `~/code/hermes-agent-template`.
- The live fleet uses checkpoint timers while newer dirty template work points
  toward heartbeat timers.
- The registry, profiles, fleet env, systemd units, and role manifests can
  disagree without one authoritative reconciliation path.
- n8n is a promising visual orchestration layer, but should not become another
  competing source of truth.

## Product Goal

Make `pjangler` the explicit Fleet control plane and introduce a versioned Fleet
contract that lets the independent parts stay independent while making their
runtime coupling visible, validated, and repairable.

## Users

- Primary operator: Deloren, managing a personal but production-like fleet.
- Agent implementers: Codex, Hermes PM agents, and future worker agents acting
  from explicit contracts.
- Visual orchestrator: n8n workflows that coordinate Fleet health, delegation,
  and reconciliation.

## Functional Requirements

FR1: The system shall define a versioned Fleet contract that records template
source versions, service model, schema versions, and supported runtime modes.

FR2: `pjangler` shall expose `pj templates status` to report each vendored
template source, commit, tag, dirty state, and effective provisioning source.

FR3: `pjangler` shall expose `pj fleet status` to compare the Fleet contract
against `~/.hermes/fleet.env`, `~/.hermes/agents-registry.yaml`, profiles,
role manifests, runtime repos, and systemd units.

FR4: `pjangler` shall expose `pj fleet reconcile` with dry-run by default and
explicit `--apply` mutation mode.

FR5: The Fleet registry shall support managed exceptions through fields such as
`managed`, `kind`, `owner`, `source`, and `notes`.

FR6: The implementation shall validate `.project.json`, `role.yaml`,
`agents-registry.yaml`, `profile.yaml`, and `fleet-contract.yaml` with explicit
schemas.

FR7: `pjangler` publishing shall fail when vendored template submodules are
dirty, mismatched, or missing required files.

FR8: The heartbeat v2 strategy shall use n8n as the visual orchestration plane
and systemd as the local survival fallback.

FR9: n8n workflows shall read Fleet state from `pjangler`/Fleet registry rather
than storing independent Fleet truth.

FR10: n8n workflow generation shall be reproducible from `pjangler` and must be
validatable before creation or update.

FR11: The implementation shall repair known current drift, including stale
runtime scaffold fallback paths, heartbeat/checkpoint mismatch, stale registry
Hermes paths, and invalid generated role model fields.

FR12: The operator shall be able to inspect and run all checks locally without
network side effects unless an apply or create flag is explicitly provided.

## Non-Functional Requirements

NFR1: Default commands must be read-only or dry-run.

NFR2: Reconciliation must never destroy or merge runtime state automatically
when profiles or runtime repos contain ambiguous data.

NFR3: n8n outage must not prevent baseline agent health, profile gateway
operation, fleet Bloodbank routing, or runtime checkpoint fallback.

NFR4: Validation output must be deterministic and grep-friendly.

NFR5: Release gates must be suitable for local CLI use and npm `prepublishOnly`.

NFR6: Secrets must remain outside committed artifacts and generated workflow
definitions.

NFR7: The architecture must support fast solo iteration without requiring a
large platform migration before useful checks land.

NFR8: Generated n8n workflow code must follow n8n workflow SDK design guidance:
normalize data early, avoid accidental per-item fanout, and validate workflow
code before creation.

## Scope

In scope:

- Fleet contract design.
- `pjangler` status, reconcile, validation, and release gates.
- n8n workflow generation artifacts and first supervisor workflow path.
- Systemd fallback heartbeat/checkpoint strategy.
- Current drift repair stories.

Out of scope for the first implementation:

- Rewriting Hermes internals beyond the minimal profile/config validation needed.
- Migrating every agent to custom n8n nodes.
- Removing systemd.
- Making n8n the Fleet source of truth.

## Success Criteria

- `pj templates status` clearly explains which template bytes will be used.
- `pj fleet status` reports registry/profile/systemd/schema drift without
  destructive changes.
- `pj fleet reconcile --apply` fixes safe drift and labels unsafe drift manual.
- `npm run prepublishOnly` fails if vendored templates are dirty.
- A generated n8n Fleet supervisor workflow can be validated before creation.
- Agents continue local fallback behavior when n8n is unavailable.
