# Hermes on big-chungus: ONE install, ONE code path

Status: **single install (2026-10-17).** Hermes runs from `~/.hermes/hermes-agent`, a checkout of
`delorenj/hermes-agent` `main`, and from nowhere else.

| consumer | resolves to |
|---|---|
| `hermes-gateway.service` (the one host gateway, every profile, Telegram + the Bloodbank fleet lane) | `~/.hermes/hermes-agent/.hermes/bin/hermes` |
| `~/.local/bin/hermes`, `hermes-acp`, `hermes-agent` | same launcher |
| `~/.hermes/fleet.env` `HERMES_FLEET_BIN` / `HERMES_FLEET_REPO` | same launcher / same checkout |
| `~/.config/hermes-agent-template/config.toml` `[fleet]` | same |
| every `agents/hermes/*/hermes` wrapper | same (env, then config, then the embedded fallback) |
| `agents-registry.yaml` `hermes.bin` / `repo` / `git_sha` | same launcher / checkout / an ancestor of its HEAD |

## Why this is one line and not two

For two months Hermes ran from **two** code lines. The CLI wrappers and the registry pinned an "immutable"
release directory (`~/.local/share/hermes-agent/releases/<sha>`, added 2026-08-15 by DNET-4); `hermes` on
PATH and, from 2026-10-10, `hermes-gateway.service` ran the dev checkout (Hermes' own installer derives
`ExecStart` from whichever `hermes` is first on PATH). Neither line was a superset: the gateway lacked 32 of
the fork's commits (route-limit 403 fallback, op:// credential refusal, cron economics), the CLI lacked the
upstream gateway, the hook `final_response` the Bloodbank artifact sidecar needs, and four thousand upstream
commits. The same prompt behaved differently through Telegram and through `agents/hermes/pm/hermes`.

The "immutable release" reason did not survive contact: the release dir was fast-forwarded in place, carried
uncommitted edits, and was not what production ran. There is nothing to protect by keeping a second copy.

The fork's still-relevant commits were ported onto the upstream tree and published as `delorenj/hermes-agent`
`main` (the previous main is `backup/main-a-line-20261017`). The upstream base is NOT bumped as a side effect
of anything here: scope is unification, not upgrade.

## Deploying a change (the only procedure)

```bash
cd ~/.hermes/hermes-agent
git status --short            # must be clean (untracked agents/ is the one known exception)
git pull --ff-only            # tracks delorenj/main; never merge or reset here
# restart only when idle: gateway_state.json active_agents == 0, no turn lease held
systemctl --user restart hermes-gateway.service
python3 ~/code/33GOD/hermes-agent-template/scripts/hermes-single-install-check.py
```

Work on Hermes happens in a **clone** (`~/code/hermes-unify` style), is pushed to `delorenj/hermes-agent`
`main`, and lands in the install by the fast-forward above. Do not edit files in the install itself: the
running gateway imports from it, and a half-written edit is live on the next import.

Never run `hermes update` here: it pulls Nous `origin/main` (thousands of commits) over the fork's work and
rewrites every profile's `config.yaml`. The `main` branch of the install tracks `delorenj/main` on purpose.

## The drift guard

`scripts/hermes-single-install-check.py` reduces every consumer in the table to the install root it execs and
fails when two differ, when a binary is missing, when a registry `git_sha` is not in the install's history,
when the install does not track a `delorenj/*` branch, or when it is a blobless partial clone. It warns on a
wrapper's embedded fallback that names another path (shadowed by env/config today), a registry sha behind
HEAD, and a gateway that booted from an older sha than HEAD. Run it by hand, or let the timer do it:
`hermes-single-install-guard.timer` runs it every 15 minutes and `infra-doctor` watches its `ok` stamp
(`~/.cache/hermes-single-install/ok`, probe `hermes-single-install`).

## Rules that stay

1. **Never run `hermes update` on big-chungus** (above).
2. **Restart the gateway only when idle.** There is one gateway now; a restart interrupts every profile.
   Check `~/.hermes/gateway_state.json` (`active_agents: 0`) and the session turn leases first.
3. **State migrations are one-way.** Every profile `config.yaml` is at `_config_version 50` and `state.db` at
   schema 31 (written by the upstream tree). Never run an older tree against them.
4. **Release dirs are retired.** `~/.local/share/hermes-agent/retired-20261017/` holds the old ones; nothing
   references them. Do not recreate `releases/<sha>`: `hermes-runtime-dropin.py` and `install-local.sh` refuse.

## The 1Password budget is a first-class constraint

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

