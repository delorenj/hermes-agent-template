# Upgrading hermes across the fleet

Status: **the fleet is deliberately pinned.** Read rule 1 before doing anything.

## 1. Never run `hermes update` on big-chungus while gateways are live

It is not merely useless here — it will take the fleet down.

`hermes update`'s systemd discovery glob is `hermes-gateway*`
(`hermes_cli/update_cmd_fleet.py:38`, and `_is_hermes_gateway_unit` at `:638`
requires a unit to be `hermes-gateway.service` or start with `hermes-gateway-`).
Every unit on this host is named `hermes-<profile>-gateway.service`. Verified:

```
$ systemctl --user list-units 'hermes-gateway*'  --plain --no-legend | wc -l
0
$ systemctl --user list-units 'hermes-*gateway*' --plain --no-legend | wc -l
16
```

That empty set flows into `_get_service_pids(all_profiles=True)`
(`hermes_cli/gateway.py:120`), so `service_pids` is empty. `find_gateway_pids`
then scans `/proc`, and every live gateway matches
`looks_like_gateway_command_line` — their real cmdline is
`.../hermes -p <profile> gateway run --replace`, and `gateway run` is exactly
what the matcher wants. **All of them are classified as *manual* processes**,
signalled, and handed detached respawn watchers that race systemd's
`Restart=on-failure`.

It also rewrites **every** profile's `config.yaml` non-interactively
(`update_cmd_config.py::_migrate_sibling_profile_configs`), colliding with the
generated base+delta output of `hermes-profile-config.py`.

There is nothing on the box that stops you typing it. That is the risk.

## 2. This is a fork re-sync, not a version bump

| surface | checkout | remote | version |
|---|---|---|---|
| `hermes` on PATH | `~/.hermes/hermes-agent` | `NousResearch/hermes-agent` (upstream) | 0.21.3 |
| every gateway | `~/.local/share/hermes-agent/releases/<sha>` | `delorenj/hermes-agent` (fork) | 0.20.1 |

`git rev-list --left-right --count delorenj/main...origin/main` → **19 / 15895**
across merge-base `45af7a71`. The upstream tree carries none of the fork-only
paths or the fork's gateway commits. Pinning the fleet at the upstream tree
deletes them silently.

Two one-way doors are **already open**: every config says
`_config_version: 45` while the running code knows 37, and six profiles'
`state.db` are at `SCHEMA_VERSION 30` against code that knows 26. Schema
migrations are forward-only — there is no clean rollback from those six.

## 3. The runtime is an editable install, so the release dir *is* the code

`site-packages/__editable__.hermes_agent-0.20.1.pth` points at the release
checkout, so "0.20.1" is frozen dist-info, not what runs. Consequences:

- Editing files in the release dir changes the running fleet on next start.
- The dir named `0408fec7a153…` was at HEAD `dbf5e7a0ce` with uncommitted work,
  including an untracked `cron/economics.py` that `cron/scheduler.py` imports at
  `:5117` and `:5521`. Landed 2026-09-22 as `5ca0851f`. **Check
  `git -C <release> status` is clean before any upgrade** — a `git stash` there
  breaks cron admission at call time.

## 3a. Release checkouts are partial clones and CANNOT push

`git remote -v` in a release dir shows `[blob:none]` — these are blobless
partial clones. Committing works; **pushing does not**. `pack-objects` stalls
trying to lazily backfill blobs it does not have, producing no output at all and
never reaching the network. Verified 2026-09-22 across SSH, HTTPS, `--no-thin`
and `pack.window=0`: every one hung silently until timeout while a fresh clone
of the same repo took seconds.

To land work that is sitting in a release checkout:

```bash
git -C <release> format-patch -1 <sha> --stdout > /tmp/x.patch
git clone git@github.com:delorenj/hermes-agent.git /tmp/hfork   # FULL, not --depth 1
git -C /tmp/hfork am /tmp/x.patch && git -C /tmp/hfork push origin main
git -C <release> fetch origin main
git -C <release> diff --stat HEAD origin/main    # MUST be empty before the next line
git -C <release> reset --hard origin/main
```

Do **not** use `--depth 1`: the pre-push guard refuses a shallow clone because it
cannot build ancestry to scan ("outgoing commits do not build from a clone").
That refusal is correct — unshallow rather than reaching for `GIT_GUARD_OFF=1`.

The final `diff --stat` is the safety check: `git am` produces a different sha for
identical content, and the reset is only safe because the content matches. The
release dir is the running code — confirm a checksum on a touched file is
unchanged across the reset.

## 4. The order that actually matters

1. **Preflight, all read-only.** `git -C <release> status` clean;
   `hermes-runtime-dropin.py check` green; `hermes-profile-config.py check`
   green; no unit flapping (`NRestarts`); **1Password budget** —
   `op service-account ratelimit`, account `read_write` must have room.
2. **Land and sync the fork.** Size it first with `git merge-tree` against the
   merge-base; do not start a merge you have not measured.
3. **Build a release from a pushed fork sha.** Never point a drop-in at a dirty
   or unpushed tree.
4. **Canary one agent** whose `state.db` is already at the newer schema — that
   agent has already walked through the one-way door, so the canary adds no new
   irreversible step. Watch a full minute, not a sample.
5. **Re-pin the drop-ins:** `hermes-runtime-dropin.py apply --release <sha>`.
   It writes all in-scope units in one pass and restarts nothing, by design.
   Once drop-ins disagree on a release, every later call **must** pass
   `--release` explicitly or the generator refuses to infer one.
6. **The five wrapper-launched units are invisible to that generator**
   (deckard, infra, ssbnk, TonnyBox, voxxy). Their release path lives in each
   repo's `agents/hermes/pm/.scripts/credential-launch.sh`. Update them
   separately or they stay on the old runtime.
7. **Restart in batches, Dumply last** — it is the one with a non-technical
   user on the other end, and she cannot tell a dead bot from a slow one.

## 5. The 1Password budget is a first-class constraint

hermes resolves ~19 `op://` refs per gateway start with no cross-restart cache.
The account cap is **1000 read_write/day**.

- A clean 15-agent rolling restart ≈ **285 reads** (~28% of the day).
- A restart storm at the `StartLimitBurst=5` ceiling ≈ **1425 reads** — above
  the cap, i.e. total fleet silence until reset. This has happened: 2026-09-20,
  Dumply went mute at 1000/1000.
- Units carry `StartLimitIntervalSec=300` / `StartLimitBurst=5` to cap it.
- `secrets.onepassword.cache_ttl_seconds` is **900** as of 2026-09-22 (was 0,
  which disabled the *disk* cache as well as the in-process one —
  `_cache.py:65` and `:136` both bail on `ttl_seconds <= 0`). 900 is chosen
  against the `StartLimitIntervalSec=300` window so an entire 5-start burst
  resolves from cache after the first start.

  Measured on Dumply, restarting twice inside the window:

  | | account reads consumed |
  |---|---|
  | restart #1 (cold, writes the cache) | **3** |
  | restart #2 (within TTL) | **0** |

  Both starts logged `1Password: applied 20 secrets` and came up with Telegram
  connected — the second resolved every one of them from disk. Note the real
  cold cost is ~3 account operations, not the ~19 the ref count suggests: `op`
  batches refs per request.

  The cost is that resolved secret values sit in
  `<profile>/cache/op_cache.json` for the TTL. They are written atomically as
  mode **0600** inside a **0700** directory (`_cache.py:180-199`), the same
  posture as the `.env` files already in those profiles. A rotated secret can
  linger up to 15 minutes; `rm` the file to force a re-fetch.

Do not attempt a fan-out on a day you have already spent reads.

## 6. Abort conditions

Stop, do not continue, if any of these is true:

- the release checkout is dirty or has unpushed commits
- `hermes-runtime-dropin.py check` or `hermes-profile-config.py check` is red
- any gateway is flapping (`NRestarts` climbing)
- 1Password `read_write` remaining is under ~400
- a drop-in is missing from a unit — its **base** `ExecStart` points at
  `~/.hermes/hermes-agent/.venv/bin/hermes`, which is the *other* version, so a
  unit that loses its drop-in silently jumps a major version alone on next start

## 7. Open question: unit naming

Renaming units to hermes' own convention (`hermes-gateway-<profile>.service`)
would make `hermes update --plan`, its fleet-restart phase, and
`hermes -p <p> gateway restart` all work natively, deleting more custom code
than it costs. But it is a migration — 25 base units, 20 drop-in dirs, plus
every script and registry field that names a unit — and it must land *together*
with an explicit rule about when `hermes update` may run, because renaming is
precisely what arms the mass-restart described in rule 1.
