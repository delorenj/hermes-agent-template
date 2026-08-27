#!/usr/bin/env python3
"""Real base+delta inheritance for Hermes profile configs.

Hermes has NO native profile config inheritance. ``load_config()`` merges
exactly two layers: the shipped ``DEFAULT_CONFIG`` and the single user file at
``$HERMES_HOME/config.yaml`` (plus an optional root-owned managed-scope overlay
that *wins* at the leaf, so it cannot serve as an overridable base). The
``config.inherit_from: default`` / ``save_mode: delta`` block that the fleet
docs describe -- and that several ``profile.yaml`` files already carry -- is
read by zero lines of Hermes code: ``profile.yaml`` is metadata ABOUT a profile
(description, role), never config.

Without inheritance the fleet drifted into two failure modes, both silent:

  * ``config.yaml`` symlinked to the fleet base -- inherits everything but can
    override nothing, and detaches permanently the first time Hermes saves
    config (``atomic_yaml_write`` does ``os.replace``, which swaps the symlink
    for a regular file).
  * ``config.yaml`` as a small real file -- overrides fine but inherits NOTHING
    from the fleet base, so a 13-line profile silently runs near-stock Hermes
    with no MCP servers, no skill dirs, and no fleet model.

This tool makes the intended contract real:

    ~/.hermes/config.yaml                     canonical base (hand-edited)
    ~/.hermes/profiles/<p>/config.delta.yaml  SSOT override (hand-edited, tiny)
    ~/.hermes/profiles/<p>/config.yaml        GENERATED = deep_merge(base, delta)

A missing or empty delta means "identical to base", which is exactly the
expected behavior. The merge replicates Hermes' own ``_deep_merge`` semantics
so the generated file is byte-for-byte what Hermes would have resolved.

Commands:
    init     one-time migration: recover each profile's true delta from the
             config it has today, then render. Idempotent.
    render   regenerate config.yaml from base + delta.
    check    verify every config.yaml matches its base+delta render (drift
             gate; exits non-zero on drift).
    absorb   fold an out-of-band edit to a generated config.yaml back into the
             delta, so in-agent writes (``/model``, onboarding) survive.
    status   show each profile's delta size and drift state.

Backups are written to ~/.hermes/.profile-config-backups/<timestamp>/<profile>/
-- deliberately NOT beside the file, because several profile dirs are symlinks
into component repos where a stray .bak would be committed.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML required: pip install pyyaml")

HERMES_HOME = Path(os.environ.get("HERMES_FLEET_HOME", Path.home() / ".hermes"))
BASE = HERMES_HOME / "config.yaml"
PROFILES = HERMES_HOME / "profiles"
BACKUP_ROOT = HERMES_HOME / ".profile-config-backups"

GENERATED_HEADER = """\
# ---------------------------------------------------------------------------
# GENERATED FILE -- DO NOT EDIT.
#
#   source of truth : config.delta.yaml (in this directory)
#   base            : ~/.hermes/config.yaml
#   rendered by     : hermes-profile-config.py render
#
# Edit config.delta.yaml and re-run `hermes-profile-config.py render`.
# An edit made here directly (or by Hermes itself, e.g. /model) can be folded
# back into the delta with `hermes-profile-config.py absorb --profile <name>`.
# ---------------------------------------------------------------------------
"""

# Directories under profiles/ that are not real profiles.
SKIP = {"33god-pm.bak"}


# --------------------------------------------------------------------------
# merge semantics -- Hermes deep merge plus one template-owned list patch
# --------------------------------------------------------------------------
LIST_PATCH_KEY = "x-pjangler-merge"


def _plain_deep_merge(base: dict, override: dict) -> dict:
    """Hermes-compatible recursive merge without template directives."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _plain_deep_merge(result[key], value)
        elif key in result and isinstance(result[key], dict) and value is None:
            continue
        else:
            result[key] = copy.deepcopy(value)
    return result


def _apply_list_patches(result: dict, directive: Any) -> None:
    if directive is None:
        return
    if not isinstance(directive, dict):
        raise ValueError(f"{LIST_PATCH_KEY} must be a mapping")
    patches = directive.get("list_patches", {})
    if not isinstance(patches, dict):
        raise ValueError(f"{LIST_PATCH_KEY}.list_patches must be a mapping")
    for dotted, rule in patches.items():
        if not isinstance(dotted, str) or not dotted or any(
            not part or not part.replace("_", "").replace("-", "").isalnum()
            for part in dotted.split(".")
        ):
            raise ValueError("list patch path is invalid")
        if not isinstance(rule, dict):
            raise ValueError(f"list patch for {dotted} must be a mapping")
        additions = rule.get("add", []) or []
        removals = rule.get("remove", []) or []
        if not isinstance(additions, list) or not isinstance(removals, list) or not all(
            isinstance(item, str) for item in [*additions, *removals]
        ):
            raise ValueError(f"list patch for {dotted} must contain string lists")
        cursor = result
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"list patch parent for {dotted} is not a mapping")
            cursor = child
        leaf = parts[-1]
        current = cursor.get(leaf, []) or []
        if not isinstance(current, list):
            raise ValueError(f"list patch target {dotted} is not a list")
        removed = set(removals)
        merged = [item for item in current if item not in removed]
        for item in additions:
            if item not in merged:
                merged.append(item)
        cursor[leaf] = merged


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*.

    Mirrors Hermes' ``_deep_merge``: override wins; two dicts recurse; a
    ``None`` override of a dict base is ignored (an empty YAML section must not
    blow away a whole default subtree); everything else -- lists included --
    is replaced wholesale rather than concatenated.  The reserved
    ``x-pjangler-merge.list_patches`` directive is then applied and stripped
    from generated config.  It lets a role own one list addition/removal while
    future base-list edits continue to flow through.
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        raise ValueError("base and delta must be mappings")
    directive = override.get(LIST_PATCH_KEY)
    ordinary = {key: value for key, value in override.items() if key != LIST_PATCH_KEY}
    result = _plain_deep_merge(base, ordinary)
    _apply_list_patches(result, directive)
    return result


# Keys the base alone owns. A profile must never pin these as an "override":
#   _config_version -- a migration marker; a stale pin re-runs old migrations.
#   secret-shaped   -- credentials live in ~/.hermes/.env as ${VAR} refs. A
#                      frozen profile copy still holds the pre-migration
#                      literals, and writing them into a delta would re-spray
#                      plaintext secrets across the fleet.
BASE_OWNED_KEYS = {"_config_version"}

# Dotted paths the FLEET owns outright. A profile silently opting out of the
# fleet memory architecture is how "memory: disabled" spread: the 2026-08 Honcho
# removal wrote these into individual profiles, and they long outlived the
# Honcho removal itself. Memory scoping is a fleet decision now (the provider
# slot carries identity memory via bank_id_template); a profile that needs a
# different bank overrides `memory.bank_id`, not the whole subsystem.
FLEET_OWNED_PATHS = {
    "memory.provider",
    "memory.memory_enabled",
    "memory.user_profile_enabled",
}
SECRET_KEY_RE = __import__("re").compile(
    r"(api_key|password|passwd|secret|token|credential|hash)", __import__("re").I
)


def minimal_delta(
    current: Any, base: Any, *, history: list[dict] | None = None, path: str = ""
) -> Any:
    """Return the smallest subtree of *current* that re-creates it over *base*.

    Invariant: ``deep_merge(base, minimal_delta(current, base))`` yields a dict
    whose every key present in *current* holds *current*'s value.

    Three filters keep a recovered delta honest rather than merely correct:

    * ``BASE_OWNED_KEYS`` and secret-shaped keys are dropped outright.
    * When *history* (older base snapshots) is supplied, a leaf whose value
      matches some historical base value is treated as inherited staleness, not
      intent, and dropped -- a profile that froze a copy of the base months ago
      "differs" from today's base on every setting that has moved since, and
      none of those are overrides the operator chose.

    One expressible limit: a delta can override or add a key, but cannot
    *delete* one the base defines. That matches Hermes' merge -- there is no
    tombstone -- and `status` calls it out when it bites.
    """
    if not isinstance(current, dict) or not isinstance(base, dict):
        return copy.deepcopy(current)
    out: Dict[str, Any] = {}
    for key, cur_val in current.items():
        dotted = f"{path}.{key}" if path else key
        if key in BASE_OWNED_KEYS or SECRET_KEY_RE.search(key):
            continue
        if dotted in FLEET_OWNED_PATHS:
            continue
        # An empty-string leaf is never a real override: either it adds a key the
        # base does not define (a no-op), or it blanks a populated fleet default,
        # which only happens because the base was empty when the copy was frozen.
        if cur_val == "" and base.get(key) != "":
            continue
        hist_vals = [h[key] for h in (history or []) if isinstance(h, dict) and key in h]
        if key not in base:
            out[key] = copy.deepcopy(cur_val)
        elif isinstance(cur_val, dict) and isinstance(base[key], dict):
            sub = minimal_delta(
                cur_val,
                base[key],
                history=[h for h in hist_vals if isinstance(h, dict)],
                path=dotted,
            )
            if sub:
                out[key] = sub
        elif cur_val != base[key]:
            # Inherited staleness: this exact value was the fleet default once.
            if any(cur_val == hv for hv in hist_vals):
                continue
            out[key] = copy.deepcopy(cur_val)
    return out


def load_history() -> list[dict]:
    """Older fleet-base snapshots, newest first, used to spot inherited drift."""
    snaps = sorted(
        HERMES_HOME.glob("config.yaml.bak*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [load_yaml(p) for p in snaps]


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------
def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:
        sys.exit(f"FATAL: cannot parse {path}: {exc}")


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False, width=100)


def profile_dirs() -> list[Path]:
    if not PROFILES.is_dir():
        sys.exit(f"FATAL: {PROFILES} not found")
    out = []
    for child in sorted(PROFILES.iterdir()):
        if child.name in SKIP:
            continue
        # Follow dir symlinks (several profiles point into component repos).
        if child.is_dir():
            out.append(child)
    return out


def backup(paths: list[Path], tag: str) -> Path | None:
    live = [p for p in paths if p.exists() or p.is_symlink()]
    if not live:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    dest_root = BACKUP_ROOT / f"{stamp}-{tag}"
    for path in live:
        dest = dest_root / path.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / path.name
        if path.is_symlink():
            # Record where it pointed; the target itself is the shared base.
            target.with_suffix(target.suffix + ".symlink").write_text(
                os.readlink(path) + "\n", encoding="utf-8"
            )
        else:
            shutil.copy2(path, target)
    return dest_root


def write_generated(path: Path, merged: dict) -> None:
    """Write the rendered config, replacing a symlink with a real file."""
    if path.is_symlink():
        path.unlink()
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(GENERATED_HEADER + dump_yaml(merged), encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# per-profile state
# --------------------------------------------------------------------------
def profile_state(pdir: Path, base: dict) -> Tuple[dict, dict, dict, bool]:
    """Return (delta, current_cfg, expected_render, is_symlink)."""
    cfg_path = pdir / "config.yaml"
    delta_path = pdir / "config.delta.yaml"
    is_link = cfg_path.is_symlink()
    delta = load_yaml(delta_path)
    current = load_yaml(cfg_path)
    expected = deep_merge(base, delta)
    return delta, current, expected, is_link


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    base = load_yaml(BASE)
    history = [] if args.no_prune_stale else load_history()
    if history:
        print(f"using {len(history)} historical base snapshot(s) to drop inherited staleness\n")
    targets = _select(args)
    to_backup = []
    for pdir in targets:
        to_backup += [pdir / "config.yaml", pdir / "config.delta.yaml"]
    if not args.dry_run:
        loc = backup(to_backup, "init")
        if loc:
            print(f"backup -> {loc}\n")

    for pdir in targets:
        cfg_path = pdir / "config.yaml"
        delta_path = pdir / "config.delta.yaml"
        is_link = cfg_path.is_symlink()

        if delta_path.exists():
            delta = load_yaml(delta_path)
            origin = "existing delta"
        elif is_link:
            # Symlinked to the base: identical by definition, so no overrides.
            delta = {}
            origin = "symlink -> base (empty delta)"
        else:
            current = load_yaml(cfg_path)
            raw_delta = minimal_delta(current, base)
            delta = minimal_delta(current, base, history=history)
            dropped = len(dump_yaml(raw_delta).splitlines()) - len(
                dump_yaml(delta).splitlines() if delta else []
            )
            origin = "recovered from current config"
            if dropped > 0:
                origin += f", {dropped} stale line(s) pruned"

        merged = deep_merge(base, delta)
        lines = len(dump_yaml(delta).splitlines()) if delta else 0
        gained = len(merged) - len(load_yaml(cfg_path)) if not is_link else 0

        print(f"{pdir.name:36s} delta={lines:>3d} lines  ({origin})")
        if gained > 0:
            print(f"{'':36s}   +{gained} top-level keys inherited from base")

        if args.dry_run:
            continue
        delta_path.write_text(
            "# Override-only delta for this Hermes profile.\n"
            "# Merged over ~/.hermes/config.yaml to produce config.yaml.\n"
            "# Empty/missing == identical to the fleet base.\n\n"
            + (dump_yaml(delta) if delta else "{}\n"),
            encoding="utf-8",
        )
        os.chmod(delta_path, 0o600)
        write_generated(cfg_path, merged)
    return 0


def cmd_render(args) -> int:
    base = load_yaml(BASE)
    targets = _select(args)
    if not args.dry_run:
        loc = backup([p / "config.yaml" for p in targets], "render")
        if loc:
            print(f"backup -> {loc}\n")
    for pdir in targets:
        delta, _cur, expected, _link = profile_state(pdir, base)
        print(f"render {pdir.name:36s} ({len(dump_yaml(delta).splitlines()) if delta else 0} delta lines)")
        if not args.dry_run:
            write_generated(pdir / "config.yaml", expected)
    return 0


def cmd_check(args) -> int:
    base = load_yaml(BASE)
    drifted = []
    for pdir in _select(args):
        delta, current, expected, is_link = profile_state(pdir, base)
        if is_link:
            drifted.append((pdir.name, "config.yaml is a SYMLINK (no override capability)"))
        elif not (pdir / "config.delta.yaml").exists():
            drifted.append((pdir.name, "no config.delta.yaml (not under inheritance)"))
        elif current != expected:
            keys = sorted(
                k for k in set(current) | set(expected) if current.get(k) != expected.get(k)
            )
            drifted.append((pdir.name, f"drift in: {', '.join(keys[:6])}"))
    if drifted:
        print("PROFILE CONFIG DRIFT:\n")
        for name, why in drifted:
            print(f"  {name:36s} {why}")
        print(f"\n{len(drifted)} profile(s) drifted. Fix: hermes-profile-config.py render --all")
        return 1
    print("OK: every profile config.yaml == deep_merge(base, delta)")
    return 0


def cmd_absorb(args) -> int:
    base = load_yaml(BASE)
    targets = _select(args)
    if not args.dry_run:
        loc = backup([p / "config.delta.yaml" for p in targets], "absorb")
        if loc:
            print(f"backup -> {loc}\n")
    for pdir in targets:
        cfg_path = pdir / "config.yaml"
        if cfg_path.is_symlink():
            print(f"skip   {pdir.name:36s} (symlink -- run init first)")
            continue
        old_delta = load_yaml(pdir / "config.delta.yaml")
        current = load_yaml(cfg_path)
        directive = copy.deepcopy(old_delta.get(LIST_PATCH_KEY))
        effective_base = deep_merge(
            base, {LIST_PATCH_KEY: directive} if directive is not None else {}
        )
        new_delta = minimal_delta(current, effective_base)
        if directive is not None:
            new_delta[LIST_PATCH_KEY] = directive
        if new_delta == old_delta:
            print(f"clean  {pdir.name}")
            continue
        added = sorted(set(new_delta) - set(old_delta))
        print(f"absorb {pdir.name:36s} +{len(added)} key(s): {', '.join(added[:6]) or '(nested)'}")
        if not args.dry_run:
            (pdir / "config.delta.yaml").write_text(
                "# Override-only delta for this Hermes profile.\n\n" + dump_yaml(new_delta),
                encoding="utf-8",
            )
            os.chmod(pdir / "config.delta.yaml", 0o600)
    return 0


def cmd_status(args) -> int:
    base = load_yaml(BASE)
    print(f"{'profile':36s} {'mode':10s} {'delta':>5s}  {'state'}")
    print("-" * 78)
    for pdir in _select(args):
        delta, current, expected, is_link = profile_state(pdir, base)
        has_delta = (pdir / "config.delta.yaml").exists()
        mode = "symlink" if is_link else ("delta" if has_delta else "standalone")
        n = len(dump_yaml(delta).splitlines()) if delta else 0
        if is_link:
            state = "cannot override base"
        elif not has_delta:
            state = f"NOT inheriting base ({len(current)} own keys)"
        elif current != expected:
            state = "DRIFT"
        else:
            state = "ok"
        print(f"{pdir.name:36s} {mode:10s} {n:>5d}  {state}")
    return 0


def cmd_memory_pin(args) -> int:
    """Pin each agent's identity-memory bank explicitly.

    The shared provider config uses ``bank_id_template: agent-{profile}``, where
    ``{profile}`` comes from ``hermes_cli.profiles.get_active_profile_name()``.
    That resolver calls ``Path.resolve()`` on HERMES_HOME and then requires the
    result to sit directly under ``~/.hermes/profiles/`` and match a strict id
    regex. Two real cases break it, and both fail *silently* to the literal
    ``"custom"`` -- which would place every affected agent in ONE shared bank:

      1. the profile directory is a symlink into a component repo, so
         ``.resolve()`` lands outside ``profiles/``;
      2. the profile name has uppercase and fails the id regex.

    Six profiles hit this today. Rather than depend on that resolver, pin
    ``bank_id`` per profile so identity is deterministic and a future rename or
    re-symlink cannot silently merge two agents' private memory.
    """
    import json

    broken = args.only_broken
    pinned = skipped = 0
    for pdir in _select(args):
        name = pdir.name
        # What the resolver WOULD produce, without importing Hermes.
        resolved = pdir.resolve()
        ok = (
            resolved.parent == PROFILES.resolve()
            and __import__("re").fullmatch(r"[a-z0-9][a-z0-9_-]*", resolved.name) is not None
        )
        if broken and ok:
            skipped += 1
            continue
        target = pdir / "hindsight" / "config.json"
        payload = {}
        if target.exists():
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        payload["bank_id"] = f"agent-{name}"
        payload.pop("bank_id_template", None)  # explicit pin wins; no ambiguity
        why = "resolver-ok" if ok else "resolver would yield 'custom'"
        print(f"pin {name:34s} -> agent-{name:32s} ({why})")
        if args.dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        _ensure_runtime_ignored(target)
        pinned += 1
    print(f"\npinned {pinned}, skipped {skipped}")
    return 0


def _ensure_runtime_ignored(path: Path) -> None:
    """Keep a pinned config out of any repo that happens to contain it.

    Several profile dirs symlink into component repos, so this file can land on
    a tracked path. It is per-agent runtime state, never source -- add an ignore
    rule rather than let a repo start tracking a daemon's config.
    """
    import subprocess

    real = path.resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(real.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if top.returncode != 0:
            return
        repo = Path(top.stdout.strip())
        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", str(real)],
            capture_output=True, timeout=10,
        ).returncode == 0
        if ignored:
            return
        rel = real.relative_to(repo)
        gi = repo / ".gitignore"
        line = f"/{rel.parent}/"
        body = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if line in body:
            return
        with open(gi, "a", encoding="utf-8") as fh:
            if body and not body.endswith("\n"):
                fh.write("\n")
            fh.write(f"\n# Hermes per-agent runtime state (never source)\n{line}\n")
        print(f"     .gitignore += {line}  ({repo})")
    except Exception:
        pass


def _select(args) -> list[Path]:
    if getattr(args, "profile", None):
        pdir = PROFILES / args.profile
        if not pdir.is_dir():
            sys.exit(f"FATAL: no such profile: {args.profile}")
        return [pdir]
    return profile_dirs()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in [
        ("init", cmd_init, "recover deltas from current configs and render"),
        ("render", cmd_render, "regenerate config.yaml from base + delta"),
        ("check", cmd_check, "drift gate (non-zero exit on drift)"),
        ("absorb", cmd_absorb, "fold out-of-band config.yaml edits into the delta"),
        ("status", cmd_status, "show delta size and drift state per profile"),
        ("memory-pin", cmd_memory_pin, "pin each agent's identity-memory bank explicitly"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--profile", help="operate on one profile (default: all)")
        p.add_argument("--all", action="store_true", help="all profiles (default)")
        p.add_argument("--dry-run", action="store_true", help="show, do not write")
        p.add_argument(
            "--no-prune-stale",
            action="store_true",
            help="keep every difference from base, even values that were "
            "themselves an older fleet default (init only)",
        )
        p.add_argument(
            "--only-broken",
            action="store_true",
            help="memory-pin: pin only the profiles whose identity would "
            "silently resolve to 'custom' and collide",
        )
        p.set_defaults(func=fn)
    args = ap.parse_args()
    if not BASE.exists():
        sys.exit(f"FATAL: fleet base not found: {BASE}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
