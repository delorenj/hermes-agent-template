# hermes-agent-template

Copier template that provisions a single Hermes agent role into an existing
repository, complete with its own ignored per-agent runtime directory for
memory and session state.

## How it relates to CommonProject

| | CommonProject | hermes-agent-template |
| --- | --- | --- |
| What it scaffolds | A new top-level project | An agent role inside an existing project |
| Copier target | `./my-new-project/` | `./agents/hermes/<role>/` |
| Asks | project name + description | role + purpose + tone |
| Post-gen artifacts | Plane project, bmad init, mise tasks | Plane project, Telegram bot wiring, optional Slack wiring, pure-local runtime, systemd units |

CommonProject runs first to create the umbrella project. hermes-agent-template
runs second (for each agent role you want) to drop agents into it.

## Roles

The template provisions a single Hermes role per invocation. The `pm` role
handles project management and triage, and also runs the continuous ticket
sentinel out-of-band: a provider-agnostic board-reconciliation pass (Linear,
Plane, or Trello) with an autonomous adversarial review (act, do not wait). The
sentinel is the PM's `heartbeat.sh` reconciliation runner. **No timer is
installed for it**: Candybar owns when it runs (see
[Running reconciliation on a cadence](#running-reconciliation-on-a-cadence)).
Under the current pure-local runtime contract, a pass performs board
reconciliation only. (There is no separate `scrum-master` role; its duties
folded into the PM.)

To work on or extend the heartbeat sentinel, start with the [sentinel handoff
guide](docs/sentinel/README.md).

## Quickstart

```bash
# Inside a project (e.g. ~/code/33GOD/bloodbank)
copier copy gh:delorenj/hermes-agent-template ./agents/hermes/pm \
  --data role=pm \
  --data agent_purpose="Triage incoming bloodbank PRs and route work"
```

The template will:

1. Seed `~/.config/hermes-agent-template/config.toml` from the shipped example (see [Configuration](#configuration))
2. Ensure `~/.hermes/fleet.env` exists (single source of truth for shared Hermes binary/repo/registry)
3. Create a clean Hermes profile `<repo>-pm` without cloning the default profile's credentials
4. Create ignored role-owned state at `agents/hermes/pm/runtime/`; HERMES_HOME remains the named profile
5. Populate missing files from the runtime scaffold (SOUL.md, memories, and role state)
6. Refuse any stale project gitlink or `.gitmodules` mapping for that runtime
7. Verify a profile-dedicated BotFather token, store it in 1Password, and map only its `op://` reference
8. Defer Slack by default, or wire a dedicated app+bot pair through the same reference-only contract
9. Create a Plane project in your configured workspace
10. Mark Bloodbank ingress as fleet-scoped, with no per-profile consumer
11. Install the systemd `--user` profile gateway unit (no heartbeat timer; provisioning deletes any an older install left)
12. Append the agent and its Bloodbank `target_agent_id` to `~/.hermes/agents-registry.yaml`

## Configuration

All environment-specific defaults live in **`~/.config/hermes-agent-template/config.toml`**
(override the path with `$HERMES_TEMPLATE_CONFIG`). Nothing user-specific is
hardcoded in the scripts — so the template can be handed to someone else and
retargeted by editing one file.

On the first provisioning run, `.scripts/01-config.sh` seeds that file from
[`config.example.toml`](config.example.toml) if it doesn't exist yet, then warns
you to review it. Keys:

| Section | Key | What it sets |
| --- | --- | --- |
| `fleet` | `hermes_bin`, `hermes_repo` | Shared Hermes executable + repo checkout |
| `fleet` | `hermes_git_url`, `hermes_git_ref`, `hermes_git_sha` | The fork the one Hermes install tracks; the sha is the last HEAD fleet-sync recorded (the drift guard requires the live HEAD to descend from it) |
| `fleet` | `fleet_env`, `registry_file` | Fleet source-of-truth + registry locations |
| `fleet` | `oauth_file`, `codex_home` | Shared Hermes OAuth store + Codex CLI/app-server auth home |
| `fleet` | `runtime_scaffold_dir` | Fallback scaffold (if agent-local one is missing) |
| `plane` | `base`, `workspace` | Plane URL + workspace slug |

The example configuration retains an inert `[github].runtime_repo_owner` value
only so older manifests can still be parsed. Current provisioning never reads
it to create, attach, synchronize, restore, or retire runtime storage.

Resolution precedence for every value: **explicit env var → `~/.hermes/fleet.env`
→ `config.toml` → built-in fallback**. So you can still override any single value
per-run with an env var or `--data`, and existing setups keep working even without
the config file present.

## Platform support: Linux and macOS

Provisioning runs on Linux (systemd) and macOS (launchd). The portable paths:

| Concern | Linux | macOS |
| --- | --- | --- |
| Shell | bash >= 4.4 | `/bin/bash` is 3.2: `.scripts/lib/bash-guard.sh` re-execs the step under the first bash >= 4.4 it finds (`$HERMES_TEMPLATE_BASH`, PATH, `/opt/homebrew/bin`, `/usr/local/bin`) or stops with `brew install bash` instructions |
| Fleet / project locks | `flock(1)` | no `flock(1)`: `.scripts/lib/fd-lock.py` takes the same `flock(2)` lock on the same inherited descriptor, so it excludes `flock(1)` holders on one lock file |
| Atomic file exchange (`fleet.env`, channel CAS) | `renameat2(RENAME_EXCHANGE)` | `renamex_np(RENAME_SWAP)`; a volume without swap support fails closed before anything moves |
| Hashing, relative paths, in-place edits | `sha256sum`, `realpath --relative-to`, `sed -i` | `shasum -a 256` / python, `os.path.relpath`, a temp-file rewrite (never a `.bak`) |
| Service manager | one `hermes-<agent>-gateway.service` per agent unit file (`70-systemd.sh`) | **no per-agent units**: the one multiplexed gateway, LaunchAgent `ai.hermes.gateway`, is ensured through the fork (`hermes gateway install --if-missing`, `status`) only when the role's `service_state.gateway` is `active`; otherwise it is recorded `deferred`. A gateway that does not become supervised is never left running by the step |
| Drift guard | systemd `ExecStart=` | plus `~/Library/LaunchAgents/ai.hermes.gateway*.plist` `ProgramArguments` |

Checkout locations are configuration, not constants: `HERMES_CODE_ROOT`,
`HERMES_TEMPLATE_REPO`, and `PJ_SKILLS_REGISTRY_ROOT` (env), or `[paths]` /
`[skillex]` in `~/.config/hermes-agent-template/config.toml`. The defaults are the
Linux fleet host's `~/code/...` layout. `HERMES_TEMPLATE_PLATFORM=Darwin|Linux`
overrides `uname -s` (tests).

`install-local.sh` is **deprecated** (literal credentials, retired heartbeat) and
refuses to run on macOS. Adopt an existing host with
`docs/adopt-existing-hermes-host.md`.

## Ticket-provider adapters are vendored, not owned

`template/.scripts/providers/{plane,linear,trello}.sh` are a **mirror**. The
canonical copies live in Krebs at `krebs/adapters/tp/`, which
`33god-platform/components/krebs.yaml` declares as its source of truth.

They are mirrored here because a deployed employee needs its adapter rendered
into its own role directory and cannot reach into another repo at runtime. They
are *owned* by Krebs because creating a ticket board is a ticket concern: before
this split, pjangler created a project's board by staging a fake Hermes role tree
in a temp directory and running this template's adapter inside it, so a project
could not get a board without the agent template on disk.

Edit the Krebs copy, then `mise run tp:vendor`.
`tests/test_tp_adapter_vendoring.py` fails on drift wherever Krebs is checked
out beside this template.

## Profile skills

PM desks are **Skillex-only**: the profile's `skills/` directory holds nothing
but links Skillex owns, plus Hermes bookkeeping (`.usage.json`, curator state).
No bundled skills, no hub installs, no local skills, and no external discovery
roots. This is the canonical standard (skillex ADR-0001, Hermes PM amendment).

Provisioning requires a Skillex release whose `profile sync` supports
`--skillex-only` (0.1.3 or later; step 10 checks `--help` and refuses an older
build), Node.js 24+, a configured canonical Skillex registry, and the owning
project's `.agents/skills.json`.

- **New profile:** `hermes profile create NAME --no-alias --no-skills`, then the
  usual config, memory and SOUL steps, then `lib/skills-policy.py` writes
  `skills.external_dirs: []` into `config.delta.yaml` under the renderer lock,
  then a strict preview and a strict sync publish `.no-bundled-skills` and
  `.skillex-only` in the profile root.
- **Existing strict desk (has `.skillex-only`):** a read-only `profile show`
  (exit 0, or 6 for a pending selection or catalog change), the idempotent
  skills policy, then strict preview and sync. Config, SOUL, memory pin and
  runtime state are left byte-for-byte alone. Nothing is ever deleted.
- **Existing desk that is not strict yet:** if its `skills/` holds nothing but
  Hermes bookkeeping (an interrupted first run), provisioning resumes the full
  path. If it holds local skills or is a whole-root link, step 10 refuses and
  names the preservation-first cutover
  (`~/code/skillex/scripts/hermes-skillex-cutover.py`, preview, then `--apply`).

To apply a selection change by hand:

```bash
skillex profile sync example-pm --project /path/to/project --skillex-only --dry-run
skillex profile sync example-pm --project /path/to/project --skillex-only
```

The script supplies the selected profile and owning project path. It never
infers the project from the invocation directory. It reconciles the union of
global and project selections: a project exclusion affects the project
contribution, while an independently selected global skill remains available.
Select the required PM skills in those manifests instead of configuring the
retired `canonical_skills_dir` or `symlinked_runtime_skills` template settings.

The template repository's own `mise run skills:sync` task uses the same pinned
release with `--scope project --project '{{config_root}}'`. No skills enter hook,
manifest watcher, or copied Python skill engine remains.

Acceptance tests install the npm release under canonical `/tmp` paths. Run
`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_skillex_profile.py`.
Before publication, supply `SKILLEX_TEST_TARBALL=/absolute/path/delorenj-skillex-<version>.tgz`
to verify a prebuilt release candidate without a source dependency or rebuild.
The fixture executes the real CLI with Python and uv absent from PATH.

## What gets created where

```
your-project/
├── agents/hermes/pm/                       ← copier output, tracked in YOUR project
│   ├── role.yaml                           ← role manifest
│   ├── SOUL.md                             ← personality (canonical)
│   ├── hermes                              ← launcher
│   ├── .scripts/                           ← provisioning scripts (idempotent re-run)
│   └── runtime/                            ← ignored local HERMES_HOME
│       (HERMES_HOME for this agent)
│       └── .env                            ← profile-local channel credentials, mode 0600, gitignored
└── ...

~/.hermes/agents-registry.yaml              ← fleet roster
~/.hermes/fleet.env                          ← shared Hermes binary/repo pointer
~/.config/systemd/user/                     ← per-profile gateway (no heartbeat timer)
```

`fleet.env` is parsed as configuration data, never sourced as shell code. Each
non-comment line must be an optional `export` followed by a `KEY=literal`
assignment. Literal values may be unquoted, single-quoted, double-quoted, or
ANSI-C quoted (`$'...'`); commands, functions, `readonly`, substitutions,
duplicate keys, and malformed input are rejected before any value is imported.
For compatibility with the existing fleet layout, an unquoted value may use
only `$HERMES_FLEET_HOME` (or `${HERMES_FLEET_HOME}`) followed by a literal
path suffix. The sanitized explicit environment wins; otherwise a preceding
`HERMES_FLEET_HOME` assignment supplies the base. No other variable expansion,
parameter operator, or command syntax is accepted. Step `05` rewrites values
it owns as canonical ANSI-C literals so spaces, quotes, dollar signs,
backslashes, metacharacters, and embedded/trailing newlines round-trip as data.
Launchers, provisioners, `fleet-sync`, `migrate-unify`, and the backfill utility
all use the same rendered loader/parser pair; no maintenance path evaluates the
file independently. Existing-file updates use Linux's atomic path exchange to
bind the commit to the inode that was parsed and restore any concurrent
replacement before failing. New files use atomic no-clobber linking. A platform
without atomic exchange refuses an existing-file update rather than falling
back to a check-then-replace race.

Bloodbank command ingress is not a per-profile daemon. One fleet-shared Hermes
gateway reads the registry and routes canonical commands by
`data.target_agent_id`; local runtime directories contain no NATS consumer or
inbox bridge.
Provisioning and `fleet-sync.sh --apply` also retire the old
`hermes-<agent>-consumer.service`, even when an older `.done-70-systemd` marker
exists. Retirement fails closed: a user-manager/query error, failed disable, or
anything short of explicit `inactive` plus `disabled` leaves the unit file and
registry metadata intact and reports unhealthy drift.

Telegram and Slack ownership checks, identity claims, 1Password reference
writes, and registry upserts serialize on `${registry_file}.lock`. The lock is
held by `flock`, so a crashed process cannot leave a stale logical lock; registry
writes use an atomic replace, sync the containing directory where supported, and
keep both registry and lock at mode `0600`. Profile delta replacements use an
atomic write and remain safe to retry when a durability sync reports an error.

## The Hermes runtime: one install

Hermes is ONE code path. `~/.hermes/hermes-agent` is a checkout of the fork `delorenj/hermes-agent` `main`;
`hermes-gateway.service`, `~/.local/bin/hermes` and every `agents/hermes/*/hermes` wrapper exec its launcher
`~/.hermes/hermes-agent/.hermes/bin/hermes`. There is no second "release" checkout to drift from it.

```bash
git clone --branch main --single-branch https://github.com/delorenj/hermes-agent.git ~/.hermes/hermes-agent
git -C ~/.hermes/hermes-agent rev-parse HEAD    # the sha every registry row records
```

Do not install this fleet path from `NousResearch/hermes-agent`. Promotion is a push to the fork `main`
followed by `git pull --ff-only` in the install; `scripts/hermes-single-install-check.py` fails when any
consumer resolves elsewhere. See `docs/fleet-upgrade.md`.

## Fleet single source-of-truth

All generated launchers read `~/.hermes/fleet.env` for:

- `HERMES_FLEET_BIN`
- `HERMES_FLEET_REPO`
- `HERMES_FLEET_REGISTRY_FILE`
- `HERMES_FLEET_OAUTH_FILE`
- `HERMES_FLEET_CODEX_HOME`

If you sync/pull your shared Hermes repo and keep the same binary path, every
agent wrapper picks it up automatically. `HERMES_FLEET_OAUTH_FILE` is the
shared Hermes provider OAuth store, including `openai-codex`; `HERMES_FLEET_CODEX_HOME`
is the shared Codex CLI/app-server config/auth home.

Telegram bot tokens and Slack bot/app tokens are deliberately excluded from
this shared layer and every on-disk dotenv file. Provisioning writes them to
the configured 1Password vault and maps only `op://` references through the
named profile's `secrets.onepassword.env`; fleet config may carry the
non-secret allow-list policies as a convenience.

To retrofit existing wrappers and user systemd units:

```bash
cd /home/delorenj/code/hermes-agent-template
./scripts/backfill-fleet-sot.sh
```

## Architecture

See `docs/architecture.md`.

## Running reconciliation on a cadence

No timer is installed for the sentinel, and you must not add one (no `.timer`,
crontab entry, `systemd-run --on-*`, launchd interval, Hermes recurring job or
private sleep loop). Candybar owns when recurring work runs. To reconcile a
board on a cadence, register a Candybar `exec` runner that invokes the runner
with the named profile (use `candybar` skill; policy in flume's
`docs/scheduling-policy.md`):

```toml
# candybar.toml, next to the job source
[candybar]
name = "<project>-pm-reconcile"
schedule = "*/15 * * * *"      # sub-minute cadence is not schedulable
enabled = false                # enable only after a proven terminal invocation

[candybar.runner]
kind = "exec"
argv = ["/usr/bin/env", "HERMES_HOME=/home/<user>/.hermes/profiles/<profile>", "/abs/path/agents/hermes/pm/.scripts/heartbeat.sh"]
timeout_s = 900
```

Then `uv run candybar register <dir>` from `~/code/33GOD/candybar`, read the
schedule back with `candybar show`, and prove an invocation with
`candybar history`. The runner's own cooldown and lock still apply, so an
extra fire is cheap.

## Re-running individual steps

Every `.scripts/<NN>-*.sh` is idempotent. Drop the `.done-<NN>-*` marker file
to force re-run:

```bash
cd agents/hermes/pm
rm .scripts/.done-40-plane
SKIP_TELEGRAM=1 SKIP_SYSTEMD=1 ./.scripts/40-plane.sh
```

## Retiring an agent

(TODO: ship `retire.sh` in v1.1)

Manual retirement is deliberately non-destructive: stop the systemd units,
disable the profile units, preserve the real named-profile directory, archive the Plane project, retire messaging bots,
and remove the registry entry while preserving the ignored runtime directory.
The verified-backup and no-automated-purge retention policy is in
[Operations](docs/operations.md#retire-an-agent-preserves-runtime-by-default).
