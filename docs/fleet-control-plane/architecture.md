---
stepsCompleted: [1, 2, 3, 4, 5, 6, 7, 8]
inputDocuments:
  - docs/fleet-control-plane/prd.md
  - docs/architecture.md
  - docs/operations.md
workflowType: architecture
project_name: Fleet Control Plane
date: 2026-06-27
status: draft-ready
---

# Fleet Control Plane Architecture

## Executive Summary

The Fleet should stay multi-repo. The fix is not repo fusion. The fix is a
versioned Fleet contract, a single operational control plane in `pjangler`, and
an explicit orchestration split:

- `pjangler` is the source of control-plane truth.
- n8n is the visual workflow/orchestration plane.
- systemd is the local survival layer.
- `~/.hermes` remains live runtime state, not a template source.

## System Context

```text
CommonProject
  emits base project repo and .project.json

hermes-agent-template
  emits agents/hermes/<role>, role.yaml, runtime scaffold, fallback units

pjangler
  owns Fleet contract, schema validation, template status, fleet status,
  reconciliation, migration, release gates, and n8n workflow generation

~/.hermes
  owns fleet.env, auth, profiles, agents-registry.yaml, logs, state

n8n
  runs visual Fleet workflows generated from pjangler and registry state

systemd --user
  keeps each profile gateway and local fallback heartbeat/checkpoint alive

fleet Bloodbank gateway
  routes canonical commands to registered target_agent_id values
```

## Architecture Decisions

### ADR-001: Keep Repos Independent, Bind With Fleet Contract

Decision: Keep `CommonProject`, `hermes-agent-template`, `pjangler`, and Hermes
engine as separate repos.

Rationale:

- Each repo has a valid standalone reason to exist.
- The coupling problem is contract visibility, not physical repo layout.
- Independent release cadence matters because templates, runtime state, and
  orchestration logic change at different speeds.

Consequences:

- Add `fleet-contract.yaml` and schema validation.
- `pjangler` must report effective sources and versions instead of relying on
  hidden local assumptions.

### ADR-002: Make pjangler the Fleet Control Plane

Decision: `pjangler` owns Fleet-wide inspection, validation, reconciliation,
template pinning, migration, and n8n workflow generation.

Rationale:

- `pjangler` already bootstraps projects and Hermes agents.
- It already vendors templates and owns parity/migration logic.
- It is the right place for deterministic CLI checks and MCP surfaces.

Consequences:

- `pjangler` must stop being ambiguous about whether vendored templates are
  release inputs, development overrides, or dirty experiments.
- Publish and build paths need dirty-template gates.

### ADR-003: n8n Is the Visual Orchestration Plane, Not the Source of Truth

Decision: n8n coordinates Fleet workflows but reads truth from `pjangler`,
`fleet-contract.yaml`, and `~/.hermes/agents-registry.yaml`.

Rationale:

- n8n gives the desired visual control plane for delegation and health flows.
- Letting n8n own state would create another drift surface.
- Generated workflows are easier to review and reproduce than hand-edited
  workflow state.

Consequences:

- First implementation should use generated n8n workflows with Webhook,
  Schedule Trigger, HTTP Request, Set/If/Switch/Merge, and minimal Code nodes.
- Custom n8n nodes can come later if workflow generation proves stable.
- Workflow code must be validated before creation or update.

### ADR-004: Hybrid Heartbeat Strategy

Decision: Implement heartbeat v2 as n8n-centralized orchestration with systemd
local fallback.

Rationale:

- n8n-only would make the Fleet fragile when n8n is down.
- systemd-only hides orchestration and makes delegation hard to visualize.
- Hybrid gives visual control without sacrificing local survival.

Consequences:

- systemd keeps each profile gateway and heartbeat fallback running.
- The fleet-shared Bloodbank gateway owns command-bus ingress for all profiles.
- systemd fallback performs minimal self-health and checkpoint behavior.
- n8n performs supervisor flows, agent health fanout, delegation, and
  reconciliation triggers.

### ADR-005: Treat Managed Exceptions as First-Class Fleet Entries

Decision: Profiles and services outside the standard PM-agent pattern must be
represented as explicit managed exceptions instead of undocumented drift.

Rationale:

- Voice agents, adversarial-review profiles, and historical profiles can be
  legitimate without matching the PM template.
- Drift reports are useful only when they distinguish bugs from exceptions.

Consequences:

- Registry schema gets `managed`, `kind`, `source`, `owner`, and `notes`.
- `pj fleet status` reports unmanaged/external entries separately from broken
  managed entries.

## Fleet Contract

Initial contract shape:

```yaml
schema_version: 1
fleet_contract_version: 1
service_model: hybrid-n8n-systemd
templates:
  commonproject:
    source: git@github.com:delorenj/CommonProject.git
    required_clean: true
  hermes_agent:
    source: git@github.com:delorenj/hermes-agent-template.git
    required_clean: true
schemas:
  project_json: 1
  role_yaml: 1
  agents_registry: 2
  profile_yaml: 1
n8n:
  mode: generated-workflows
  source_of_truth: pjangler
systemd:
  fallback: true
  required_units:
    - gateway
    - fallback-heartbeat
bloodbank:
  gateway_scope: fleet
  routing_key: data.target_agent_id
```

## n8n Workflow Model

### Supervisor Workflow

Trigger:

- Schedule Trigger for periodic health.
- Webhook for manual or external Fleet events.

Flow:

1. Fetch Fleet status from `pjangler`.
2. Normalize registry/profile/systemd rows.
3. Split into managed agents and managed exceptions.
4. Branch by status: healthy, drift, degraded, manual.
5. Trigger targeted reconcile or notify operator.
6. Respond with a concise Fleet summary.

Design rules:

- Normalize data before branch convergence.
- Avoid accidental item-count fanout; use execute-once where independent calls
  do not need per-agent items.
- Prefer Set/If/Switch/Merge over Code nodes when possible.
- Use HTTP Request for internal `pjangler`/Hermes APIs until dedicated nodes are
  justified.

### Per-Agent Workflow

Trigger:

- Supervisor dispatch.
- Bloodbank or internal webhook command.

Flow:

1. Load one agent entry.
2. Check profile gateway, fleet Bloodbank registration, fallback heartbeat,
   profile symlink, runtime repo, and role manifest.
3. If safe drift exists, call `pj fleet reconcile --agent <id> --apply`.
4. If unsafe drift exists, emit manual action.
5. Record result for supervisor.

## Command Surface

```text
pj templates status [--json]
pj fleet status [--json] [--agent <id>]
pj fleet reconcile [--dry-run] [--apply] [--agent <id>]
pj fleet validate [--json]
pj fleet n8n export [--workflow supervisor|agent] [--json]
pj fleet n8n validate
pj fleet n8n create --name <workflow>
```

## Data and Schema Ownership

- `.project.json`: owned by CommonProject and pjangler migrations.
- `role.yaml`: owned by hermes-agent-template, validated by pjangler.
- `agents-registry.yaml`: owned by Fleet runtime/provisioning, reconciled by
  pjangler.
- `profile.yaml`: owned by Hermes runtime, validated for inheritance metadata.
- `fleet-contract.yaml`: owned by pjangler and committed in the control-plane
  repo.

## Current Drift Repair Targets

- Dirty vendored `templates/hermes-agent` must be promoted or reverted.
- Heartbeat v2 must become a deliberate contract, not a dirty submodule state.
- `runtime_scaffold_dir` must point at an existing fallback path.
- Registry Hermes paths must be refreshed from `fleet.env`.
- Invalid generated role model fields such as `model.name: "plane"` must fail
  schema validation.

## Rollout Strategy

1. Add schemas and read-only status commands.
2. Add dirty-template gates.
3. Add safe reconcile for wrapper/profile/registry path drift.
4. Promote heartbeat v2 contract and systemd fallback behavior.
5. Generate and validate n8n supervisor workflow.
6. Enable workflow creation only after exported workflow validation passes.
