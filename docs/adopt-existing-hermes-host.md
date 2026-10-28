# Adopt an existing Hermes host as a 33GOD host

A repeatable runbook for converting a machine that already runs Hermes (a desktop
app, a stray upstream checkout, hand-made profiles) into a 33GOD Hermes host: one
install of the delorenj fork, a fleet base, generated desks, its own org chart,
Skillex-only skills, pinned identity memory and one multiplexed gateway.

It was written from the first real adoption: Damian's Mac Studio (macOS 26.3,
arm64), 2026-10-28, which ran the Nous "Hermes Agent.app" v0.22.0 with two
hand-made profiles (`aai-team`, `private-desk`). Every gotcha below was hit there.
Linux hosts follow the same order; the macOS-only parts are marked.

> **Scope.** This adopts a host as *its own* 33GOD host (own registry, own memory
> service, own secrets). It never adds rows to the operator's registry, never
> copies the operator's Hindsight banks, SOULs or personalities, and never puts
> DeLoSecrets references or a 1Password service token on someone else's machine
> unless the operator says so.

## 0. Decisions to get from the operator first

Write them down before touching the host; each one changes a step below.

| Decision | Options | Where it lands |
|---|---|---|
| Authority | who may act on the machine (a charter may reserve it for one agent) | the record you write at the end |
| Inference | AutomaticAI consumer token (which channel, which route scope) or the host's own providers | fleet base `model`/`providers`/`delegation`/`auxiliary`, `.env` |
| Secrets | the host's own vault with `op://` refs, or target-local mode-0600 `.env` files | `.env` files, `secrets.onepassword` |
| Memory | which Hindsight (local service, operator service), fresh banks per desk | `hindsight/config.json` per desk |
| Cutover | in place or side by side; what happens to the old runtime | steps 2-4 |
| Existing rules in the old profiles (privacy, lockdowns) | keep / rewrite / drop | desk deltas, `SOUL.md` |

## 1. Preflight (read-only)

Run every remote script as a file through a login shell, never as a quoted one-liner:

```sh
ssh -o BatchMode=yes user@host 'zsh -l -s' < step.sh     # bash -l -s on Linux
```

macOS: a line starting with `=word` is a zsh filename lookup; the login PATH may
lack `~/.local/bin` and the mise shims (use absolute paths: `~/.local/bin/mise`,
`/opt/homebrew/bin/...`); there is no `timeout(1)`; `/usr/bin/python3` is the
Xcode stub (3.9) - never run tooling on it; APFS is case-insensitive
(`~/Code` == `~/code`) but **case-preserving**: write every path with its
on-disk spelling (`ls ~ | grep -i '^code$'`). git prints the on-disk case, and
flume before eb63508 compared paths as strings, so a `~/Code/flume` wrapper on
a disk that says `code` raised two false critical review errors
(`profile.renderer` "package root is not a git checkout root",
`scaffold.source` "not an initialized submodule").

Check and record:

- **Code lines.** Every `hermes`-ish entry point (`~/.local/bin/*`, app bundles,
  old checkouts) and its version. The fork must not be older than what wrote the
  state: compare `_config_version` in every `config.yaml` and
  `select max(version) from schema_version` in every `state.db` with the fork's
  (`hermes_cli/config_defaults.py`, `hermes_state_common.py`). Equal is fine; newer
  on the host means "fresh desks, carry SOUL + settings only".
- **Activity.** Last `messages.timestamp` per `state.db`. Writes that only touch
  `cron/ticker_*` and the WAL are a cron ticker, not a user.
- **Sticky profile.** `~/.hermes/active_profile`. If it names a desk, a bare
  `hermes` (and `HERMES_HOME=~/.hermes hermes ...`) runs THAT desk. Use
  `hermes -p default ...` for anything aimed at the base.
- **Pre-existing 33GOD crumbs** (`~/.hermes/fleet.env`,
  `~/.config/hermes-agent-template/config.toml`, `~/.agents/*`). Someone may have
  started before you; preserve their content, back it up, retarget it.
- **Satellite jobs** (LaunchAgents/systemd units) that hard-code the old runtime's
  python or paths.
- **Other agents' work in flight** on the same checkouts (dirty submodules,
  `*.failed-*` directories). Do not "fix" it.

## 2. Back up (outside every git repo)

```sh
B=~/hermes-backups/pre-33god-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$B/sqlite" "$B/local-bin"
for h in "" profiles/<desk> ...; do sqlite3 ~/.hermes/${h:+$h/}state.db ".backup '$B/sqlite/<tag>-state.db'"; done
tar -C ~ --exclude='.hermes/models_dev_cache.json' -czf "$B/hermes-home.tar.gz" .hermes
tar -C ~/Library/"Application Support" -czf "$B/app-support-Hermes.tar.gz" Hermes      # macOS desktop app state
cp -P ~/.local/bin/{hermes,hermes-agent,hermes-acp,<desk wrappers>} "$B/local-bin/"
cp -p <satellite plists/units> ~/.zprofile ~/.zshrc "$B/"; launchctl list > "$B/launchctl-list.txt"
chmod -R go-rwx "$B"   # + ROLLBACK.md beside it
```

Verify the SQLite copies with an immutable open (`sqlite3 "file:$f?immutable=1" 'pragma integrity_check'`;
`-readonly` fails on a WAL-mode copy). Back up every file you are about to edit
again right before editing it (host config, fleet.env, configs after the installer).

## 3. Park the old runtime

- macOS app: `osascript -e 'quit app "Hermes Agent"'` (graceful; its `serve`
  backend and helpers exit with it). Kill orphaned `browser_harness.daemon`
  processes it left behind. Check System Events login items and LaunchAgents so
  nothing relaunches it. Leave it installed.
- Why it must stop: the app's `serve` ticks cron for EVERY profile (a second
  cron ticker beside the fork gateway), it auto-updates its payload, and a bundled
  app cannot be pointed at another checkout (its bundled payload wins over every
  override). Tell the owner not to open it while the gateway runs.
- What it does NOT do: overwrite a regular file. On launch v0.22.0 re-creates
  `~/.local/bin/{hermes,hermes-agent,hermes-acp}` only when the entry is missing
  or a dangling symlink into its own payload (Electron `electron-main.mjs` and the
  payload's `_symlink_sealed_launchers` both skip `!isSymbolicLink()` / "a real
  file we did not write"). The step-5 shims are regular files, so a stray launch
  cannot hijack them; symlinked entry points would be retaken.
- Login items: System Events' list covers the classic ones; the Background Task
  Management store needs admin (`sfltool dumpbtm` fails with
  `errAuthorizationInteractionNotAllowed` over SSH). Have the owner run
  `sudo sfltool dumpbtm | grep -i -A3 nous` once, or record it as unobserved.
- Old upstream checkouts (`~/Code/hermes-agent`): retire as consumers, do not delete.

## 4. Install the fork

```sh
git clone --branch main https://github.com/delorenj/hermes-agent.git ~/.hermes/hermes-agent   # FULL clone
HERMES_REPO_URL=https://github.com/delorenj/hermes-agent.git HERMES_HOME=$HOME/.hermes \
  bash ~/.hermes/hermes-agent/scripts/install.sh --dir ~/.hermes/hermes-agent --branch main --skip-setup --verbose
git -C ~/.hermes/hermes-agent remote rename origin delorenj
git -C ~/.hermes/hermes-agent branch --set-upstream-to=delorenj/main main
hermes pm install --extra voice --extra stt-whisper --extra messaging     # what the host actually uses
```

- Full clone, not blobless: `hermes-single-install-check.py` fails a partial clone.
  ~1.1 GB; on a home uplink this took ~30 min - start it first and prepare the
  rest in parallel.
- `install.sh` defaults `REPO_URL` to NousResearch; pin `HERMES_REPO_URL`. With an
  existing checkout it fetches `origin` and fast-forwards (or resets!) to
  `origin/<branch>`, so run it while `origin` is the fork, then rename.
- Keep **no** NousResearch remote: a stray `hermes update` or installer rerun
  cannot then reset the tree onto upstream. The single-install check requires the
  branch to track `delorenj/*`.
- flume's `hermes.checkout_identity` reads the remote the pinned branch tracks
  (`branch.<git_ref>.remote`, else `origin`) and matches any URL spelling of the
  pin (scp, ssh://, https, with or without `.git`) since flume eb63508.
  Older flume read `origin` only: a host with no `origin` was
  "unobserved", and the operator's own checkouts (origin = upstream, `main`
  tracking `delorenj`) failed. Do not add an `origin` just to satisfy an old flume.
- The installer also: stages `~/.hermes/tools/*` (python 3.14, uv, node, ffmpeg,
  ripgrep, chromium, agent-browser, cua-driver), syncs bundled skills into the
  base home (desks with `.no-bundled-skills` opt out), migrates configs (an
  unversioned profile config is rewritten v0 -> v50), and appends a guarded
  `~/.local/bin` PATH line to the login rc (`.zprofile`). It does not take over
  `~/.local/bin/*` entries it does not own.
- Prove: `hermes --version` names the fork sha; `load_config()` under each
  `HERMES_HOME` returns the expected `_config_version`; `hermes doctor` runs.

## 5. Entry points

Every consumer execs the one launcher. Two-line `/bin/sh` shims in `~/.local/bin`:

```sh
exec ~/.hermes/hermes-agent/.hermes/bin/hermes "$@"                       # hermes
exec ~/.hermes/hermes-agent/.hermes/bin/hermes -p <desk> "$@"             # one per desk
exec ~/.hermes/hermes-agent/.hermes/bin/hermes --run-module run_agent "$@" # hermes-agent (the fork's own convention)
exec ~/.hermes/hermes-agent/.hermes/bin/hermes-acp "$@"                   # hermes-acp
```

Then scan every PATH directory (login and interactive) for anything still
resolving to the old payload or checkout.

## 6. 33GOD tooling on the host

- Clones (`flume`, `hermes-agent-template`, `skillex`, `voxxy`):
  `git -c submodule.recurse=false -c fetch.recurseSubmodules=false pull --rebase --autostash`.
  Never discard local edits; never `submodule update` someone's catalog checkout.
- flume: `git submodule update --init templates/hermes-agent`, then
  `mise x node@24 -- npm ci && npm run build`; expose it with a wrapper that runs
  Node 24 and puts a PyYAML python first on PATH (flume shells out to `python3`):
  ```sh
  PATH="$HOME/.local/share/hermes-template/venv/bin:/opt/homebrew/bin:$PATH" exec ~/.local/bin/mise x node@24 -- node ~/code/flume/packages/flume-hr/dist/index.js "$@"
  ```
- Template python: `uv venv --python 3.12 ~/.local/share/hermes-template/venv && uv pip install pyyaml`,
  plus a `hermes-profile-config` wrapper for `scripts/hermes-profile-config.py`.
- bash >= 4.4 (macOS: `brew install bash`); the template's guard re-execs under it.
- Skillex: the contract needs npm `@delorenj/skillex` >= 0.1.3 on Node 24. If the
  host's `skillex` on PATH is another program (here: a Python uv tool that owns the
  Codex skill root), install npm skillex privately and never `mise use -g` it
  (`mise activate` would shadow the other one in every shell):
  ```sh
  mise x node@24 -- npm install -g --prefix ~/.local/share/skillex-npm @delorenj/skillex@0.3.1
  # ~/.local/bin/skillex-npm: exec ~/.local/bin/mise x node@24 -- node ~/.local/share/skillex-npm/lib/node_modules/@delorenj/skillex/dist/cli.js "$@"
  ```
  Pass it as `SKILLEX_BIN` to the skillex cutover and resync scripts (skillex >= aa59c85).
- macOS needs skillex >= d20578c (`fix(host)`). Earlier releases stamp receipts,
  the vendor journal and lock claims with `os.hostname()`, which on a Mac with no
  `scutil --get HostName` is whatever DHCP/Bonjour handed out last: here it went
  `Mac.attlocal.net` -> `HelloSub.local` minutes after the desks were synced, every
  desk receipt became "another host's" (`E_RECEIPT_INVALID`, exit 3) and the resync
  agent went ATTENTION. d20578c keys macOS on `darwin:<IOPlatformUUID>` and still
  accepts records stamped with the current hostname. Until it is on npm, install it
  from a packed build of skillex main (`npm pack --ignore-scripts` of the built
  package, version `<release>+<sha>`), into the same private prefix.
- Receipts stamped with a hostname the Mac no longer reports are refused, not
  migrated. Move each one aside (back it up) and re-run
  `skillex-npm profile sync <desk> --project <desk>/.skillex-selection --skillex-only`;
  `profile show` then reports exit 0, pending false, and the receipt carries the
  stable id. Receipts other desks wrote under the current hostname stay valid until
  their next sync rewrites them.

## 7. Host config and fleet.env

- `flume handbook bootstrap` writes `~/.config/hermes-agent-template/config.toml`
  only when absent; an older file is reported "present". Retarget it either way:
  `[fleet]` launcher/repo/sha of the fork, `vox_url`, `vox_plugin_dir`,
  `onepassword_vault` (empty when the host has no vault); `[paths] code_root`,
  `template_repo`; `[skillex] registry_root`; `[plane]` the host's own Plane; drop
  retired keys (`canonical_skills_dir`, `symlinked_runtime_skills`).
- `fleet.env`: `HERMES_FLEET_HOME`, `HERMES_FLEET_REPO`, `HERMES_FLEET_BIN`,
  `HERMES_FLEET_REGISTRY_FILE`. `parse-fleet-env.py --upsert` works on Darwin
  (`renamex_np`), **but** it writes `$'...'` values and flume's release-provenance
  comparison reads the raw text, so write plain `KEY=/absolute/path` lines (the
  operator's own file is plain). Preserve unrelated keys already there.

## 8. Fleet base `~/.hermes/config.yaml`

Convert the host's own config in place (scripted, idempotent): keep every
preference the owner set, add the invariants flume enforces:

- `model.provider: automaticai`, `model.default: automaticai/<...>` (no
  `model.base_url`); `providers.automaticai {api, key_env: AUTOMATICAI_GATEWAY_KEY,
  models, extra_body.reasoning_effort}`; `delegation.provider: automaticai` +
  route, empty `base_url`/`api_key`; every auxiliary task on a gateway route,
  `auxiliary.free_only: true`, `auxiliary.discovery: false`. "Every task" means
  every key in the fork's `DEFAULT_CONFIG["auxiliary"]`, not the ones the host
  config happens to list: a task left out inherits the fork default (`provider:
  auto`, `model: ""`), which is the MAIN model. Here `voice_chat` (set it to the
  fast route) and `review` (a full `/review` subagent on the delegation rail: set
  it to the delegation route) rode `auto/orchestrator` until set explicitly;
  `fallback_providers: []`; `moa.presets.default.enabled: false` with gateway slots.
- `hooks:` the bb-hook events calling `~/.agents/hooks/bb-hook --cli hermes --native <event>`
  (copy the client from `bloodbank/services/hook-hub/client/bb-hook`, 0755; it is
  stdlib-only and fails open), `hooks_auto_accept: true`, and `BB_HOOK_HUB=off` in
  every `.env` until the host has a hub and a reachable bus.
- `memory.provider: hindsight` (+ `bank_id_template`), memory not in
  `agent.disabled_toolsets`; `tts.provider: vox` with the host's vox URL;
  `plugins.enabled: [hindsight, tts/vox]`; `gateway.multiplex_profiles: true`;
  `platform_toolsets.bloodbank` and `timeouts.tools.{sequential_call,concurrent_batch}: 1800`;
  `skills.external_dirs: []`.
- `auth.adopt_external_logins: false` in the base, and clear the direct-provider
  credentials the host's Hermes already holds: `hermes -p default auth list`, then
  `hermes -p default auth remove <provider> <n>` for each (openai-codex, openrouter,
  copilot, ...) and `hermes -p default logout --provider openai-codex` to clear the
  stale `active_provider`. All local: nothing is revoked, `~/.codex/auth.json` and
  the gh CLI are untouched, and each source is suppressed so it is not re-seeded.
  Desks borrow the root store, so one cleanup covers them. A Nous anonymous guest
  identity (`account_tier: anonymous`) is not an account and is re-minted on demand;
  record it rather than chase it. Save the auth files first (Codex refresh tokens are
  single-use; a restore must be the file, not a re-login race).
- Never copy operator-only content: DeLoSecrets refs, personalities,
  `global_instruction_files`, trusted project dirs, `/home/<operator>` paths.
- Validate with the fork's own `load_config()`, not by eye.

## 9. Secrets

Pipe each credential from the operator side straight into the target file; it
never appears in argv, logs or output:

```sh
op read '<ref>' | ssh host '<python that upserts KEY into each .env with umask 077, atomic replace, mode 0600>'
```

Preserve every other line; chmod every `.env*` (including old `.env.bak` copies
and copies inside old migration backups, e.g. `~/.hermes/migration/openclaw/*/backups/**/.env`)
to 0600 (`find ~/.hermes -name '.env*' -type f -not -name '*.lock' -not -perm 600`);
prove with length/prefix only. Never write a 1Password item path into anything
left on the host (logs and notes copied there included); say "operator-side ref". A local Hindsight bearer token is copied
on the host itself, never transferred. With target-local `.env` files (no vault),
`hermes.gateway-routing` reports "secrets.onepassword.env maps no
AUTOMATICAI_GATEWAY_KEY" - record it as the operator's accepted exception.

## 10. Desks

Each existing profile becomes a generated desk (real directory, never a symlink):

1. `hermes-profile-config init --profile <desk>` (dry run first) recovers a delta.
2. Replace it with **override-only** content: keep the desk's real choices
   (lockdowns, toolsets, reasoning), drop model pins and anything the base now
   owns (`memory.provider: ''`, `hooks: {}`, `plugins.enabled: []` - the last also
   trips `hermes.delta-list-override`), drop toolset names the fork does not know
   (`hermes config check` warns "unknown toolset"), and set
   `platforms.telegram.enabled: false`, `platforms.slack.enabled: false`,
   `skills.external_dirs: []`. A delta leaf equal to the base is either dropped or
   kept on purpose as a lockdown pin (so a later base change cannot widen the desk);
   say which in a comment beside it.
3. `render`, then `check` must say every config equals `expected_config(base, delta)`
   and the header must read `GENERATED FILE -- DO NOT EDIT.` After ANY base edit,
   `render` every generated profile `check` lists, including desks another agent
   added meanwhile; a base change you leave unrendered is their drift.
4. `profile.yaml` with the handbook's `identity.allowed_keys` only (`name`,
   `display_name`, `role`, `description`, `description_auto`, `ui_meta`); flume
   review flags anything else.
5. `SOUL.md` stays hand-written; rewrite only what the operator decided.

## 11. Plugins (per home: base AND every desk)

User plugins are discovered under `$HERMES_HOME/plugins`, so each desk needs its own.

```sh
hermes -p default  plugins install hindsight --ref d56c4acdf59c41957613d399094cdf8c489b060c --yes-deps --no-enable
hermes -p <desk>   plugins install hindsight --ref d56c4acdf59c41957613d399094cdf8c489b060c --yes-deps --no-enable
ln -s ~/code/voxxy/plugins/tts/vox <home>/plugins/tts/vox
```

- Without `--ref` the install takes the LIVE catalog pin (it showed `d952b6e6`),
  not the fork's `plugin-catalog/hindsight.yaml`.
- Without `--yes-deps` a non-interactive install refuses the plugin's Python deps
  and installs nothing.
- `HERMES_HOME=~/.hermes` alone lands in the sticky active profile (step 1).
- `memory.provider` is the switch; `--no-enable` keeps the generated configs clean.
  Re-run `check` afterwards.

## 12. Org chart

flume cannot adopt an existing desk today: `hire --definition` refuses an occupied
profile, `onboard` needs the hire marker, and the specialist projection would
regenerate `SOUL.md` and pin operator-only values (DeLoSecrets member-token ref,
`api.hs.delo.sh`). So:

- Author `~/.agents/workforce/<id>/agent.yaml` (schema 2: id, display_name, role,
  charter, skills, memory) as the definition record.
- Hand-write `~/.hermes/agents-registry.yaml` under `flock <registry>.lock`
  (atomic replace, re-parse): `schema_version: 1`, a `gateways.bloodbank` block
  for this host (launchd label on a Mac), one portable-specialist row per desk in
  the rererere shape (`employment`, `definition_path`, `desk_path`, `identity`,
  `profile_name`, `display_name`, `role`, `type: hermes`, `hindsight`,
  `bloodbank {enabled, gateway_scope: fleet, target_agent_id}`, `hermes` pin with
  THIS host's paths and the fork sha). Quarantine `bloodbank.enabled: false` while
  the host's bus has no reachable port, and say why in a `note`. Channels without a
  verified credential get `provisioning_status: deferred`.
- `flume roster` must be "in good standing".

## 13. Identity memory

- Each desk: `hindsight/config.json` with `mode: local_external` and the host's
  `api_url`, then `hermes-profile-config memory-pin --profile <desk> --bank-id agent-<desk>`
  (memory-pin writes only `bank_id`, so seed mode/url first).
- `HINDSIGHT_API_KEY` in each desk `.env`.
- Create each bank and prove one retain + recall roundtrip with a nonce through
  the desk's own config and key.
- A locked-down Hindsight (here: a synthetic-only, recall-only bearer boundary)
  needs an explicit widening for exactly the desk banks, a backup of its boundary
  files, its own documented restart procedure, a before/after boundary check, and
  an authorization note in the owner's repo.

## 14. Skills (strict Skillex-only)

- npm skillex requires `--project`. A desk with no role set follows flume's
  portable-specialist layout: `<desk>/.skillex-selection/.agents/skills.json`
  `{"inherit_global": true}` (exactly the host's global selection; a role set
  would add `"sets": [...]`).
- `SKILLEX_BIN=<npm skillex> python3 <skillex>/scripts/hermes-skillex-cutover.py
  --profile <desk> --project <desk>/.skillex-selection --registry-root <skillex>
  --renderer <template>/scripts/hermes-profile-config.py` (preview, then `--apply`):
  foreign entries are quarantined under `~/.hermes/.skill-quarantine/`, the strict
  sync runs, a second dry run must show 0 changes, `profile show` exit 0.
- Resync: `SKILLEX_BIN=<npm skillex> bash <skillex>/scripts/install-hermes-resync.sh install`
  (Darwin: LaunchAgent `io.33god.skillex-hermes-resync`, the pin persists in the
  plist), then `status` must say healthy after its first run.
- Never edit `~/.agents/skills*` or the catalog checkout to make a sync pass.

## 15. Gateway

```sh
HERMES_HOME=$HOME/.hermes hermes -p default gateway install --start-now --start-on-login
```

- One multiplexed service (`ai.hermes.gateway` / `hermes-gateway.service`) with
  `HERMES_HOME=~/.hermes` serves the base and every desk.
- macOS: installed over SSH (launchctl managername `Background`) Hermes loads the
  job into `user/<uid>`, not `gui/<uid>` - the recommended domain on macOS 26+.
  `launchctl print user/$(id -u)/ai.hermes.gateway`. flume >= c92e969 probes both.
- The plist's `LimitLoadToSessionType [Aqua, Background]` cannot double-load it:
  launchd keeps one instance of a label per user across `gui/<uid>` and
  `user/<uid>`. Proven on macOS 26.3 with throwaway labels in both orders and with
  the real gateway: the second `launchctl bootstrap` fails `5: Input/output error`
  and the running job is untouched (same pid, runs 1). At the next login the job
  may come back in `gui/<uid>` instead; both probes find it. The fork's host lock
  is the second layer (a late second gateway attaches and exits 0, parked by
  `KeepAlive.SuccessfulExit=false`).
- Anything that stops it (rollback, uninstall) must boot it out of BOTH domains
  before deleting the plist: `for d in gui user; do launchctl bootout $d/$(id -u)/ai.hermes.gateway; done`
  (same for `io.33god.skillex-hermes-resync`). A bootout of the wrong domain fails
  silently under `2>/dev/null` and leaves the job running with its plist gone.
- With zero chat platforms the fork logs "No messaging platforms enabled. Gateway
  will continue running for cron job execution." and stays up: it is the cron
  ticker for every home and the kanban dispatcher. Keep it running; the deferred
  state lives in the platform layer (delta `enabled: false`, registry
  `provisioning_status: deferred`). Prove stability over >60 s (same pid, runs 1,
  never exited) and read `gateway_state.json` (`served_profiles`, `code_sha`).

## 16. Satellite jobs

Anything that imported the old runtime (here a LaunchAgent transcriber that ran
the app payload's python with `APP_ROOT=<payload>/hermes-agent`): point it at the
fork without pinning a versioned interpreter path.

- Script: `APP_ROOT = ~/.hermes/hermes-agent`; `sys.path.insert(0, APP_ROOT)` then
  `import hermes_bootstrap` before importing Hermes modules (activates the install
  environment).
- Launch it through a wrapper that reads the interpreter from the published
  launcher each run: `sed -n 's|^exec \(/[^ ]*/bin/python3\) -I .*|\1|p' ~/.hermes/hermes-agent/.hermes/bin/hermes`
  and execs it with `-I`. (`hermes --run-module` only runs modules on the
  checkout's path; `--print-runtime-command` gives the same interpreter.)
- Reload the job, run it once, prove its real dependency path (here: local STT on
  a synthetic sample outside the watched folder).

## 17. Proof

- One oneshot turn per desk: `<desk> chat -q "Reply with exactly OK" --oneshot`.
- Ledger: on the operator host, `docker --context aai-cloud exec newapi-postgres
  psql -U newapi -d newapi` -> `logs` rows for the consumer token at those
  timestamps: route, `channel_id`, `other.automaticai_account`, and the excluded
  alternatives (scope confinement).
- `python3 hermes-agent-template/scripts/hermes-single-install-check.py` -> OK.
- `flume roster`, `flume review`, `flume audit --employee <id>`. Host rules skip
  with "No Hermes roles present" unless run in a repo with a role dir; to evaluate
  them on a specialist-only host, run `flume audit <tmp repo> --rules ...` against
  a throwaway repo holding a minimal `agents/hermes/pm/role.yaml`, then delete it.

## Verification gotchas (independent re-check, 2026-10-28)

- A claim of "no `op://` anywhere on the host" is only provable for the Hermes
  runtime config; other projects on the host may legitimately carry refs. Scope the
  claim, and redact the item path from any migration log copied to the host.
- `flume audit --employee` resolves the definition's `global` skill set against the
  host's catalog checkout; a broken checkout (staged deletions, a `*.failed-*`
  update folder) makes it "unable to assess". That checkout is the owner's to
  repair; do not reset it to get a green audit.
- A commit an agent makes in the owner's repo uses the agent identity
  (`-c user.name=... -c user.email=...` plus a Co-Authored-By trailer), not the
  host's default git identity.

## Known gaps (as of 2026-10-28)

- flume has no adopt path; `audit --employee` stays "unable to assess" for adopted
  desks (no hire marker) and `review` reports "singleton entries are not a link
  into this agent's role-local runtime" for every portable specialist (the
  operator's own rererere included).
- `hermes.gateway-routing` hard-codes the operator's curated route list and
  requires an `op://` mapping, so a second host on its own gateway channel with
  target-local secrets cannot pass it; record both as operator exceptions.
- `hermes.skillex-resync` still only knows the systemd units; on a Mac it warns
  "could not be fully assessed" even when the LaunchAgent is healthy.
- flume compares `fleet.env` text without decoding the `$'...'` quoting the
  template writes.
- A Bloodbank command lane and live hook publishing need a reachable bus and a
  hook-hub service on the host; until then rows are quarantined and
  `BB_HOOK_HUB=off`.
