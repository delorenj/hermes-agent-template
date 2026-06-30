---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - docs/fleet-control-plane/prd.md
  - docs/fleet-control-plane/architecture.md
workflowType: epics-and-stories
project_name: Fleet Control Plane
date: 2026-06-27
status: draft-ready
---

# Fleet Control Plane - Epic Breakdown

## Requirements Inventory

### Functional Requirements

- FR1: Define a versioned Fleet contract.
- FR2: Report effective template source, commit, tag, and dirty state.
- FR3: Report registry/profile/role/systemd/fleet env drift.
- FR4: Reconcile safe drift through dry-run and explicit apply modes.
- FR5: Support managed exceptions.
- FR6: Validate core manifests with schemas.
- FR7: Block release on dirty vendored templates.
- FR8: Implement n8n plus systemd hybrid heartbeat.
- FR9: Keep n8n as orchestration, not source of truth.
- FR10: Generate and validate n8n workflows.
- FR11: Repair known current drift.
- FR12: Keep default commands read-only.

### Non-Functional Requirements

- NFR1: Default to dry-run/read-only.
- NFR2: Never destroy ambiguous runtime state automatically.
- NFR3: Keep local fallback alive when n8n is down.
- NFR4: Produce deterministic, grep-friendly output.
- NFR5: Support CLI and npm release gates.
- NFR6: Keep secrets out of committed artifacts.
- NFR7: Preserve fast solo iteration.
- NFR8: Validate n8n workflows before creation.

## Epic List

1. Fleet contract and schema foundation.
2. Template source status and release gates.
3. Fleet status and reconciliation.
4. Hybrid heartbeat and n8n workflow generation.
5. Drift repair and rollout hardening.

## Epic 1: Fleet Contract and Schema Foundation

Goal: Establish the explicit contract that all other implementation work checks
against.

### Story 1.1: Add Fleet Contract Schema

As the Fleet operator, I want a committed Fleet contract schema so that template
versions, service model, and manifest versions are explicit.

Acceptance Criteria:

- Given the repo contains no Fleet contract, when `pj fleet validate` runs, then
  it reports the missing contract clearly.
- Given a valid `fleet-contract.yaml`, when validation runs, then schema version,
  service model, template entries, and schema versions pass.
- Given an unsupported service model, when validation runs, then the command
  fails with the invalid value and allowed values.

### Story 1.2: Add Manifest Schemas

As an implementer, I want schemas for `.project.json`, `role.yaml`,
`agents-registry.yaml`, and `profile.yaml` so invalid generated state fails
early.

Acceptance Criteria:

- Given `role.yaml` has `model.provider: ""` and `model.name: "plane"`, when
  validation runs, then it reports an invalid model override.
- Given registry entries lack exception metadata for non-standard profiles, when
  validation runs, then they are classified as unmanaged drift.
- Given profile metadata includes `config.inherit_from: default` and
  `save_mode: delta`, when validation runs, then inheritance contract passes.

### Story 1.3: Add Managed Exception Model

As the Fleet operator, I want explicit managed exceptions so that special
profiles do not pollute drift reports.

Acceptance Criteria:

- Given a voice-agent profile exists outside the PM scaffold, when it is marked
  `managed: false` and `kind: voice-agent`, then `pj fleet status` reports it
  under exceptions.
- Given an unregistered profile exists without exception metadata, when status
  runs, then it reports manual classification required.
- Given an exception has notes, when status runs with `--json`, then notes are
  included.

## Epic 2: Template Source Status and Release Gates

Goal: Make template bytes and release safety visible.

### Story 2.1: Implement `pj templates status`

As the Fleet operator, I want to see effective template sources so I know which
bytes provisioning will use.

Acceptance Criteria:

- Given `PJANGLER_HERMES_TEMPLATE` is set, when status runs, then it reports the
  env override as effective source.
- Given vendored templates exist, when status runs, then it reports commit,
  branch, dirty state, and submodule path.
- Given local template checkout exists but vendored template takes precedence,
  when status runs, then it shows both and explains precedence.

### Story 2.2: Add Dirty Vendored Template Gate

As a package maintainer, I want publish/build to fail on dirty vendored
templates so experiments do not ship accidentally.

Acceptance Criteria:

- Given `templates/hermes-agent` has modified files, when `npm run prepublishOnly`
  runs, then it fails before packaging.
- Given a submodule pointer changed cleanly and is committed, when the gate runs,
  then it passes.
- Given generated runtime submodule changes are present, when the gate runs, then
  it reports them separately from template dirtiness.

### Story 2.3: Add Template Drift Guidance

As an implementer, I want actionable drift messages so I know whether to promote
or revert template changes.

Acceptance Criteria:

- Given vendored `templates/hermes-agent` differs from `~/code/hermes-agent-template`,
  when status runs, then it recommends promote, update pointer, or discard.
- Given CommonProject vendored commit lags local checkout, when status runs, then
  it reports pinned-vs-local delta without failing by default.
- Given `--strict` is passed, when local and vendored template commits differ,
  then the command exits non-zero.

## Epic 3: Fleet Status and Reconciliation

Goal: Turn implicit Fleet drift into deterministic status and safe repair.

### Story 3.1: Implement `pj fleet status`

As the Fleet operator, I want one command to compare registry, profiles,
role manifests, fleet env, runtime repos, and systemd units.

Acceptance Criteria:

- Given registry and profile symlink agree, when status runs, then the agent is
  healthy for that check.
- Given a registry entry points at a stale Hermes binary path, when status runs,
  then it reports stale metadata and the effective `fleet.env` path.
- Given systemd units are checkpoint-era while the contract says heartbeat-era,
  when status runs, then it reports service model drift.

### Story 3.2: Implement Safe `pj fleet reconcile`

As the Fleet operator, I want safe drift repaired explicitly so routine fixes
are repeatable.

Acceptance Criteria:

- Given a wrapper differs from the template and no runtime state is at risk, when
  `--apply` runs, then the wrapper is regenerated.
- Given a profile path is a real directory where a symlink is expected, when
  reconcile runs, then it reports manual action and does not delete anything.
- Given runtime `profile.yaml` is missing inherited config metadata, when
  `--apply` runs, then metadata is inserted without removing other fields.

### Story 3.3: Add Current Drift Repair Commands

As the Fleet operator, I want known current drift addressed by idempotent repair
paths.

Acceptance Criteria:

- Given config fallback `runtime_scaffold_dir` points at a missing path, when
  repair runs, then it updates to an existing template scaffold path.
- Given registry entries have stale Hermes paths, when repair runs, then they
  update from `fleet.env`.
- Given invalid `role.yaml` model override exists, when repair runs, then it
  removes or corrects the bad override.

## Epic 4: Hybrid Heartbeat and n8n Workflow Generation

Goal: Promote heartbeat v2 deliberately as n8n orchestration plus systemd
fallback.

### Story 4.1: Define Heartbeat v2 Contract

As the Fleet operator, I want heartbeat v2 specified so checkpoint and heartbeat
models do not coexist accidentally.

Acceptance Criteria:

- Given `service_model: hybrid-n8n-systemd`, when validation runs, then required
  gateway, consumer, and fallback heartbeat expectations are checked.
- Given a checkpoint-only agent exists during migration, when status runs, then
  it is classified as legacy rather than broken.
- Given a heartbeat-enabled agent exists, when status runs, then n8n registration
  and systemd fallback are both checked.

### Story 4.2: Export n8n Supervisor Workflow

As the Fleet operator, I want `pjangler` to generate an n8n supervisor workflow
so orchestration is reviewable and reproducible.

Acceptance Criteria:

- Given Fleet status JSON, when workflow export runs, then the generated
  workflow normalizes agent rows before branching.
- Given n8n workflow validation is available, when `pj fleet n8n validate` runs,
  then it validates generated code before any workflow is created.
- Given n8n is unavailable, when export runs, then local workflow code is still
  generated without creating remote state.

### Story 4.3: Add Per-Agent n8n Workflow Pattern

As the Fleet operator, I want per-agent workflows so health and delegation can
be visualized without hand-wiring every agent.

Acceptance Criteria:

- Given an agent id, when per-agent workflow export runs, then workflow input is
  scoped to one registry entry.
- Given safe drift is found, when the workflow is executed, then it calls the
  dry-run reconcile path first.
- Given unsafe drift is found, when the workflow is executed, then it emits a
  manual action instead of applying changes.

## Epic 5: Rollout Hardening

Goal: Make the migration safe enough to run on the live Fleet.

### Story 5.1: Add Readiness Gates

As the Fleet operator, I want a single readiness command so I know when
implementation is ready for live Fleet rollout.

Acceptance Criteria:

- Given schemas, status, reconcile dry-run, template gate, and n8n validation
  pass, when readiness runs, then it reports ready.
- Given any current drift remains unresolved, when readiness runs, then it lists
  blockers by agent or file.
- Given managed exceptions exist, when readiness runs, then it excludes them
  from blockers and includes them in a separate exception count.

### Story 5.2: Document Operator Runbook

As a future agent/operator, I want a runbook so rollout and rollback are clear.

Acceptance Criteria:

- Given the runbook, a reader can run status, validation, export, dry-run
  reconcile, apply reconcile, and rollback.
- Given n8n is down, the runbook explains systemd fallback verification.
- Given a dirty vendored template blocks release, the runbook explains promote
  versus revert choices.

### Story 5.3: Smoke Test Against Live Fleet

As the Fleet operator, I want proof against real local state before marking the
work done.

Acceptance Criteria:

- Given the live registry, when status runs, then it completes and returns JSON.
- Given a selected low-risk agent, when reconcile dry-run runs, then it reports
  expected changes without mutation.
- Given n8n workflow code is generated, when validation runs, then no workflow is
  created until validation succeeds.

