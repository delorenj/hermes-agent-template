# PM project skills: canonical provisioning and backfill

The canonical `project-skills` command enables each proven PM's own project
alongside its strict Skillex profile loadout. It writes
`skills.project_discovery: true` and an exact-root addition for
`skills.trusted_project_dirs` into the profile delta, then renders the config
under the same shared profile lock.

The runtime adapter executes the actual `HERMES_FLEET_REPO` release through its
own Python. `HERMES_FLEET_BIN` must be that release's `.venv/bin/hermes`.
An isolated fixture proves exact Git-root resolution, trust enforcement,
discovery on/off behavior and project-over-profile lexical skill precedence.
Source hashes are checked again before writing. A setting name in source is
insufficient evidence of support.

## Commands

Preview all registered PMs, including inactive and uncorrelated rows:

```bash
python3 -I scripts/hermes-profile-config.py project-skills --registered-pms --dry-run --json
```

Apply the supported rows; blocked rows remain unchanged:

```bash
python3 -I scripts/hermes-profile-config.py project-skills --registered-pms --json
```

A provisioner supplies its corroborated exact project and role directly:

```bash
python3 -I scripts/hermes-profile-config.py project-skills --profile demo-pm \
  --project-root /absolute/project --role-dir /absolute/project/agents/hermes/pm
```

Exit **0** means every requested binding can receive exact-root trust through
the supported writer. It does not mean every project is natively discoverable.
Exit **3** means at
least one row was blocked; the JSON report includes every row and the count of
updates, so a partial apply must not be reported as fleet-wide success.
`--dry-run` performs the same checks without changing either config file.
Exit 1/2 indicates an invalid command or unreadable inventory.

The standalone `skills-policy --profile demo-pm` command only enforces
`skills.external_dirs: []`; it preserves project trust. Both commands delegate
to `template/.scripts/lib/skills-policy.py`, take `ProfileConfigLock` before
reading the base/delta/generated config, and render inside that transaction
without nested locking. Symlinks, ambiguous YAML, config drift and non-finite
lock timeouts are refused. A converged rerun preserves bytes, inode, mode and
mtime. No full-config backup is created by these policy writers.

## Trust, identity and working directory

Batch identity comes from exact registry profile/project/role fields and
`agents/hermes/pm/role.yaml` identity. A present `.project.json` must agree on
`repo_path` and the exact PM agent claim. A missing manifest is reported but
does not block trust when registry and role independently verify the root.
Correlation null and inactive status do not remove a PM from scope.

Explicit unregistered bindings require an exact manifest and role claim.
Explicit bindings cannot override a contradictory registered identity. The
profile SOUL is checked without reading its contents: a symlink must point
inside the exact role; an existing plain file is recorded as such and is not
used to infer identity. Missing or foreign SOUL links are refused. An arbitrary
explicit root with no independent evidence is refused. These checks do not
write lifecycle bindings or establish Krebs readiness.

The template uses the owning manifest or the structural PM project candidate;
the writer independently verifies that candidate. The runtime resolver uses
the closest `.git` from its effective CWD. The writer executes it separately
from project CWD and role CWD. For a Git project, a different root from either
CWD blocks the row. For an evidenced non-Git project, exact own-root trust is
allowed while the missing or different native root remains a discovery gap.
HOME, `/`, a role directory or a broad ancestor is never substituted for the
canonical trust root. No Git repository or skill directory is created.

The JSON fields separate `trust_eligible` (binding evidence), `config_enabled`
(current exact trust plus discovery and strict external dirs), and
`native_discovery_status` (actual fresh project-CWD observation). A preview
leaves `config_enabled` at its current value and reports
`config_enabled_after_apply` for eligible rows. An apply also records fresh
`resolver_after` observations. `no_native_root` and `different_native_root`
explicitly mean the canonical project was not discovered, even if trust is
configured. `root_resolved_no_project_skills` covers missing directories or
currently absent trust; the raw resolver fields show which condition applies.

A gateway's configured WorkingDirectory and live process CWD are separate
observations. Configuring canonical trust does not change either, and does not
prove that an already-running session has loaded project skills. A fresh
process/session must resolve the canonical project for that tier to engage.

Trust uses `x-pjangler-merge.list_patches.skills.trusted_project_dirs.add`.
Existing profile trust lists become additions over the base; other list patches
and removals are preserved. Future base trust additions continue to flow through
re-render. The writer adds only supported runtime keys and strips the template
merge directive from generated config. Existing unknown fields, including
`trusted_project_roots`, and unrelated lists/settings are preserved. Strict
`external_dirs` remain empty.

## Strict selection and missing skill directories

Step 10 invokes the canonical project writer on initial provisioning and every
rerun. A blocked project binding is reported while strict desk provisioning
continues. Setup files are propagated through template tooling, never copied
by hand across deployed role directories.

The desk's loadout stays Skillex-owned. A real `.skillex-selection` is its
selection project; otherwise an existing receipt's project is read through
`skillex profile show`. Project trust never changes a selection receipt,
rebinds it to the repository, edits skills, or uses an `external_dirs` workaround.
An ordinary Skillex resync and a canonical re-render preserve project trust.

Missing `.agents/skills` directories are reported separately and never created.
An identity-verified, resolvable project may be configured for future discovery
even when that directory is missing; it has no current project skills to load.
The registered batch includes all 24 PMs, including TonnyBox, 33god-pm, docker,
zshyzsh and HeyMa. Nautilus's missing `.project.json` does not prevent trust
when its exact registry and role agree. Three additional evidenced unregistered
desks use explicit commands: `OptionJangler-pm`, `jacksnaps-pm`, and
`delodocs-pm`. DeLoDocs currently has no Git root, so trust eligibility and
configuration do not imply native discovery. The lowercase `optionjangler-pm`
legacy desk and unbound `raw-stdio-pm` test desk are excluded by identity
evidence; neither is renamed, deleted, rebound, or configured by inventory.

Tests in `tests/test_project_skills_policy.py` exercise executable resolver
fixtures, the real pin with disposable profiles/projects, successful top-level
writer contention, exact timeout preservation, winning-delta retries, drift,
symlinks and inherited trust. `tests/test_skillex_profile.py` covers future
provisioning and independent role selection resync/rerun behavior.
