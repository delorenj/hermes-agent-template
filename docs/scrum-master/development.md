# Scrum Master development guide

This guide covers the day-to-day work of changing the Scrum Master engine:
how to edit it, how to validate changes locally, how to provision and propagate
it, and what to watch out for. It closes with the record of the Drumjangler
cutover and the open roadmap. Read the [handoff overview](README.md) and
[Architecture](architecture.md) first.

## Two doc trees, kept in sync

The engine has two documentation trees, and they serve different readers. When
you change behavior, update both.

- `docs/scrum-master/` (this directory) holds the **developer** docs for working
  on the template.
- `template/.scripts/scrum-master/docs/` holds the **runtime** protocol docs
  that render into every provisioned project. The running agent reads these at
  run time. They are `autonomous-delegated-review.md`,
  `continuous-ticket-orchestration.md`, and `bloodbank-events.md`.

## Making a change

Most engine logic lives under `template/.scripts/scrum-master/` and
`template/.scripts/providers/`. The flow is the same for any change.

1. Edit the engine file, the adapter, or the prompt.
2. Validate locally with the steps in [Validating changes
   locally](#validating-changes-locally).
3. If you changed behavior, update the runtime protocol docs and these developer
   docs.
4. Commit. Deployments pick up the change with `copier update` (see
   [Propagating changes](#propagating-changes)).

The prompt is a Jinja template,
`template/.scripts/scrum-master/continuous-ticket-sentinel.prompt.md.jinja`. It
uses `{{ display_name }}`, `{{ target_repo }}`, `{{ role }}`, and
`{{ ticket_provider }}`. Copier renders it during provisioning.

## Validating changes locally

You can validate everything except live-board behavior without credentials.

First, check shell syntax across the engine:

```bash
for f in template/.scripts/lib/ticket-provider.sh \
         template/.scripts/providers/*.sh \
         template/.scripts/scrum-master/continuous-ticket-sentinel.sh \
         template/.scripts/scrum-master/bin/*.sh; do
  sh -n "$f" || bash -n "$f" || echo "FAIL $f"
done
```

Next, check the inline Python blocks. The shell scripts embed Python in `<<'PY'`
heredocs and `python3 -c '...'` calls, and a quoting mistake there only surfaces
at run time. This extraction script parses each block:

```bash
python3 - <<'PY'
import re, pathlib, ast
import glob
bad = 0
for f in glob.glob("template/.scripts/**/*.sh", recursive=True):
    text = pathlib.Path(f).read_text()
    for m in re.finditer(r"<<'PY'\n(.*?)\nPY\b", text, re.S):
        try: ast.parse(m.group(1))
        except SyntaxError as e: bad += 1; print(f"heredoc {f}: {e}")
    for m in re.finditer(r"python3 -c '(.*?)'", text, re.S):
        try: ast.parse(m.group(1))
        except SyntaxError as e: bad += 1; print(f"inline {f}: {e}")
print("OK" if not bad else f"{bad} FAILED")
PY
```

Finally, run the enforcement quartet end to end with a mock provider. This
proves the accept and hold branches, the decision-event emission, and the
optional adapter-based transition without touching a real board.

<details>
<summary>Mock-provider end-to-end test</summary>

```bash
T=$(mktemp -d); RD="$T/agents/hermes/scrum-master"
mkdir -p "$RD/.scripts/lib" "$RD/.scripts/providers" "$RD/.scripts/scrum-master/bin" \
         "$T/_bmad-output/implementation-artifacts/issue-evidence"
( cd "$T" && git init -q && git config user.email t@t && git config user.name t )
cp template/.scripts/lib/ticket-provider.sh "$RD/.scripts/lib/"
cp template/.scripts/scrum-master/bin/* "$RD/.scripts/scrum-master/bin/"
printf 'repo: demo\nrole: scrum-master\nagent_id: demo-sm\nticket_provider:\n  name: fake\nscrum_master:\n  grace_hours: 0\n  auto_review: true\n' > "$RD/role.yaml"
printf '#!/usr/bin/env sh\nop="$1"; shift 2>/dev/null||true\ncase "$op" in\n transition) echo "FAKE $1 -> $2" >&2; echo ok;;\n comment) echo okc;; *) echo f;; esac\n' > "$RD/.scripts/providers/fake.sh"
chmod +x "$RD/.scripts/providers/fake.sh" "$RD/.scripts/scrum-master/bin/"*.sh
EV="$T/_bmad-output/implementation-artifacts/issue-evidence"
printf '## Issue\n- Worker: codex\n## Acceptance Criteria\n- AC1: done\n## Repo Changes\n- x\n## Verification\n- Command: t\n- Result: pass\n## Ledger Update\n- Ledger updated: yes\n## Known Gaps\n- None\n## Close Recommendation\nClose recommendation: ready\n' > "$EV/TIC-1.md"
printf '## Reviewer\n- Reviewer agent: rev\n- Independent of implementer: yes\n## Locked Intent Baseline\n- Acceptance criteria source: board\n## Drift Assessment\n- Drift assessment: none\n## Adversarial Findings\n- Critical/high findings: none\n## Decision\n- Decision: accept\n' > "$EV/TIC-1.review.md"
export BLOODBANK_EVENTS_LOG="$T/e.jsonl"
bash "$RD/.scripts/scrum-master/bin/issue-autonomous-review.sh" TIC-1 "$EV/TIC-1.review.md" --close
rm -rf "$T"
```

A passing run prints `ACCEPTED — treat as done (no human wait)`,
`FAKE TIC-1 -> completed` (from the optional `--close` sweep this test passes),
and exits 0.

</details>

## Local install (one command)

For a local, single-machine deploy — the lowest-friction path, and the right
one for handing the system to someone else — use `install-local.sh` at the
repository root instead of driving Copier by hand. From inside the target
project:

```bash
export PLANE_API_KEY=<key>   # or LINEAR_API_KEY / TRELLO_KEY + TRELLO_TOKEN
curl -fsSL https://raw.githubusercontent.com/delorenj/hermes-agent-template/main/install-local.sh | sh
```

The script:

1. installs `hermes` (its own installer) and `copier` if either is missing,
2. writes a host-correct local `config.toml` (detects the hermes binary, leaves
   cloud fields blank) if one doesn't exist,
3. lists the boards on the chosen provider and prompts for the one to manage,
4. provisions the `pm` and `scrum-master` roles through Copier with all cloud,
   Telegram, and `systemd`/gateway steps skipped,
5. binds the Scrum Master to the existing board (it never creates one — it
   scrubs the provider key from Copier's environment so `42-ticket-provider.sh`
   skips board creation),
6. installs the sentinel as a `launchd` agent on macOS or a `systemd` timer on
   Linux,
7. smoke-tests the board connection through the adapter.

Useful environment overrides (skip the prompts): `HAT_REPO`, `HAT_PROVIDER`,
`HAT_ROLES`, `HAT_PLANE_WORKSPACE`, `HAT_PLANE_PROJECT`, `HAT_LINEAR_TEAM`,
`HAT_TRELLO_BOARD`, and `HAT_DRY_RUN=1` to preview without changing anything.

This local path does not wire Telegram or email. Those are convenience layers in
the `pjangler` provisioner, not requirements for a working agent — talk to the
agents with `agents/hermes/pm/hermes chat "..."` and let the sentinel run on its
timer.

## Provisioning a Scrum Master (manual)

To drive Copier directly instead of using `install-local.sh`, provision a
standalone Scrum Master with:

```bash
cd /path/to/your-project
copier copy gh:delorenj/hermes-agent-template ./agents/hermes/scrum-master \
  --data role=scrum-master \
  --data target_repo=<repo> \
  --data ticket_provider=<linear|plane|trello>
```

The `_tasks` chain in `copier.yml` runs the numbered provisioning scripts in
order. Several of them reach outside the repository.

<!-- prettier-ignore -->
> [!CAUTION]
> Full provisioning is outward-facing and partly interactive.
> `20-runtime-repo.sh` creates a private GitHub repo with `gh repo create`,
> `30-telegram.sh` prompts for a BotFather token and blocks waiting for input,
> and `42-ticket-provider.sh` can create a Plane project. Don't run full
> provisioning unattended.

Use the `SKIP_*` environment flags for a local or lean install. Each numbered
script checks its flag and skips cleanly.

| Flag | Skips |
| --- | --- |
| `SKIP_TELEGRAM` | The interactive BotFather token step. |
| `SKIP_RUNTIME_REPO` | Creating the GitHub runtime repo. |
| `SKIP_PLANE` | Creating a Plane project. |
| `SKIP_BLOODBANK` | Installing the BloodBank consumer. |
| `SKIP_SYSTEMD` | Installing `systemd` units. |

For example, a local install that creates no cloud resources:

```bash
SKIP_TELEGRAM=1 SKIP_RUNTIME_REPO=1 SKIP_PLANE=1 SKIP_BLOODBANK=1 \
  copier copy gh:delorenj/hermes-agent-template ./agents/hermes/scrum-master \
  --data role=scrum-master --data target_repo=<repo> --data ticket_provider=linear
```

After provisioning, set the board binding in
`agents/hermes/scrum-master/role.yaml`. For Linear, set `ticket_provider.team`
to the team key. Make the provider key available to the sentinel's environment:
on Linux through a `systemd` `EnvironmentFile` (for example
`~/.hermes/<agent_id>.env`); on macOS the `launchd` agent sources that same
per-agent env file, so write the key there.

`SKIP_SYSTEMD` gates only the gateway, consumer, and checkpoint units in
`70-systemd.sh`. The sentinel scheduler itself is installed by
`75-scrum-master.sh`, which picks `launchd` on macOS and `systemd` on Linux
regardless of that flag.

## Propagating changes

The template is the single source of truth. Deployments pick up engine changes
with `copier update`, which performs a three-way merge against the recorded
answers in `.copier-answers.yml`. Local provisioning state, such as board ids
and tokens, is preserved while the engine logic refreshes.

```bash
cd /path/to/your-project
copier update ./agents/hermes/scrum-master
```

<!-- prettier-ignore -->
> [!NOTE]
> `copier update` re-runs the `_tasks` chain. Pass the same `SKIP_*` flags you
> used at provision time so the update doesn't try to recreate cloud resources.

## The Drumjangler cutover

Drumjangler was the prototype for this engine and the first project cut over to
it. The cutover used a lean, fully reversible path rather than full Copier
provisioning, because full provisioning would have created a GitHub repo and
prompted for a Telegram token.

What was done:

- The engine was deployed from the template into
  `agents/hermes/scrum-master/`, identical to the source of truth.
- `role.yaml` was bound to Linear, team `DEL`. The runtime reuses the existing
  `pm` runtime through a symlink,
  `agents/hermes/scrum-master/runtime -> ../pm/runtime`, so the Scrum Master
  shares the Hermes profile and the `LINEAR_API_KEY` wiring from
  `~/.hermes/stemjangler-pm.env`.
- The Linear adapter was verified live against the real `DEL` board from the
  deployed role.
- The `systemd` timer
  `hermes-drumjangler-scrum-master-continuous-ticket-sentinel.timer` was
  installed and enabled. The old
  `hermes-stemjangler-pm-continuous-ticket-sentinel.timer` was disabled and
  stopped.
- The four bespoke `pm`-role sentinel scripts were removed. They're recoverable
  from Git history.

To roll back, restore the bespoke scripts from Git, then flip the timers:

```bash
git checkout <previous-commit> -- \
  agents/hermes/pm/.scripts/continuous-ticket-sentinel.sh \
  agents/hermes/pm/.scripts/75-continuous-ticket-sentinel.sh \
  agents/hermes/pm/.scripts/run-adversarial-review.sh \
  agents/hermes/pm/continuous-ticket-sentinel.prompt.md
systemctl --user enable --now hermes-stemjangler-pm-continuous-ticket-sentinel.timer
systemctl --user disable --now hermes-drumjangler-scrum-master-continuous-ticket-sentinel.timer
```

The full cutover record lives in the Drumjangler repo at
`docs/operations/scrum-master-migration.md`.

## Known gotchas

These cost real debugging time. Watch for them.

- **The interactive Telegram step.** `30-telegram.sh` uses `read` to capture a
  bot token, so it blocks unattended runs. Always pass `SKIP_TELEGRAM=1` for
  automated provisioning.
- **The dispatcher needs Bash.** `ticket-provider.sh` reads
  `${BASH_SOURCE[0]}`. Source it under `bash`, not `sh`. The adapters
  themselves are POSIX shell.
- **`emit-event.py` blocks on standard input.** When it runs in a
  non-interactive context, it reads standard input for an optional JSON payload.
  Redirect standard input from `/dev/null` when you call it from another script.
  The enforcement scripts already do this.
- **Brace defaults in `dash`.** Avoid `${2:-{}}` in adapter scripts. The `dash`
  shell mis-parses the `{}` default and appends a stray brace. Hoist the default
  into a variable instead.
- **Quoting in `python3 -c`.** See the warning in
  [Providers: adding a provider](providers.md#adding-a-provider).
- **`flock` is Linux-only.** macOS doesn't ship `flock`, so the runner falls
  back to an atomic `mkdir` lock. Keep both paths working in the runner.
- **Plane state is a bare UUID.** The Plane v1 API returns an issue's `state` as
  a UUID with no embedded object, so the adapter must join issues against the
  project's states map. Descriptions are `description_html`, not
  `description_stripped`.
- **Engine files render for every role.** Copier renders
  `template/.scripts/scrum-master/` and the provider files for all roles, not
  only `scrum-master`. They're inert for other roles because
  `75-scrum-master.sh` self-guards on `role == scrum-master`. Restricting the
  render to the `scrum-master` role with a conditional file path is a cosmetic
  improvement noted in the roadmap.

## Open roadmap

The following work is open for the incoming agent, roughly in priority order.

1. **Live-verify the Trello adapter.** Linear and Plane are verified live
   (Plane includes `transition` and `comment`). Trello is implemented against
   the contract but unverified. Follow [Providers: verifying an
   adapter](providers.md#verifying-an-adapter-against-a-live-board) with Trello
   credentials, and fix any endpoint or field mismatches.
2. **Confirm `install-local.sh` on a real macOS machine.** The Linux path is
   verified end to end and the Plane adapter is verified live, but the macOS
   `launchd` agent, the `mkdir` lock, and the assumption that the older Copier
   steps (`10-hermes-profile.sh`, `80-registry.sh`) are Darwin-clean get their
   first real run on a Mac.
3. **Give the Drumjangler Scrum Master its own runtime repo.** It currently
   reuses the `pm` runtime through a symlink. Re-provision with
   `SKIP_TELEGRAM=1` and a dedicated runtime repo, then replace the symlink.
4. **Confirm the first full Hermes pass after a cutover.** Heartbeat,
   adapter, and enforcement layers are verified, but the first live `run:full`
   pass with the new prompt is the last thing to watch.
5. **Restrict engine-file rendering to the `scrum-master` role.** Use a Copier
   conditional file path so other roles don't receive inert engine files.

## Read next

- [Architecture](architecture.md): the engine internals.
- [Providers](providers.md): the adapter contract and verification process.
