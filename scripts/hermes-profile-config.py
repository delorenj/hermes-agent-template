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
    ~/.hermes/profiles/<p>/config.yaml        GENERATED = expected_config(base, delta, <p>)
                                              = deep_merge(base, delta) + renderer pins
                                                (terminal.cwd <- registry project_path,
                                                 codegraph --path <- the desk's repo)

``expected_config`` is the ONE definition of the generated config. Every consumer that compares or
writes one imports this module (or runs ``expected``) instead of re-assembling the merge.

A missing or empty delta means "identical to base", which is exactly the
expected behavior. The merge replicates Hermes' own ``_deep_merge`` semantics
so the generated file is byte-for-byte what Hermes would have resolved.

Commands:
    init     one-time migration: recover each profile's true delta from the
             config it has today, then render. Idempotent.
    render   regenerate config.yaml from base + delta.
    check    verify every config.yaml matches expected_config (drift gate; exits
             non-zero on drift; WARNs on a stale registry project_path).
    repin    re-render only configs whose difference is renderer pins alone (a
             registry row written or moved after the last render).
    expected print THE expected config for one profile as JSON.
    absorb   fold an out-of-band edit to a generated config.yaml back into the
             delta, so in-agent writes (``/model``, onboarding) survive.
    status   show each profile's delta size and drift state.
    skills-policy  enforce a named desk's strict loadout under the shared lock.
    project-skills bind proven PM projects through the pinned runtime adapter.

Backups are written to ~/.hermes/.profile-config-backups/<timestamp>/<profile>/
-- deliberately NOT beside the file, because several profile dirs are symlinks
into component repos where a stray .bak would be committed.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML required: pip install pyyaml")


def load_profile_lock_module():
    source = (
        Path(__file__).resolve().parent.parent
        / "template"
        / ".scripts"
        / "lib"
        / "profile-config-lock.py"
    )
    if source.is_symlink() or not source.is_file():
        sys.exit(f"FATAL: trusted profile config lock helper is unavailable: {source}")
    spec = importlib.util.spec_from_file_location(
        "pjangler_profile_config_lock", source
    )
    if spec is None or spec.loader is None:
        sys.exit(f"FATAL: cannot load profile config lock helper: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROFILE_LOCK = load_profile_lock_module()

HERMES_HOME = Path(os.environ.get("HERMES_FLEET_HOME", Path.home() / ".hermes"))
BASE = HERMES_HOME / "config.yaml"
PROFILES = HERMES_HOME / "profiles"
BACKUP_ROOT = HERMES_HOME / ".profile-config-backups"
REGISTRY = Path(os.environ.get("HERMES_AGENTS_REGISTRY", HERMES_HOME / "agents-registry.yaml"))

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
# A *reference* is not a secret: ``op://vault/item/field`` and ``${VAR}`` name where a credential
# lives and carry none. SECRET_KEY_RE exists to stop plaintext literals being re-sprayed into
# deltas; it must never eat references. 2026-10-21: ``absorb`` matched the top-level ``secrets``
# key ("secret"), deleted every ``secrets.onepassword.env`` op:// reference from 25 PM deltas, and
# the next gateway start came up with 12 Telegram bots and a Slack app dark.
REFERENCE_RE = __import__("re").compile(r"^\s*(op://\S+|\$\{[A-Za-z_][A-Za-z0-9_]*\})\s*$")


_MISSING = object()


def is_reference(value: Any) -> bool:
    """True for a string that is an ``op://`` or ``${VAR}`` pointer, not a credential literal."""
    return isinstance(value, str) and REFERENCE_RE.match(value) is not None


def scrub_literals(value: Any, *, strict: bool = False, key: str = "") -> Any:
    """*value* without plaintext credential literals; ``_MISSING`` when nothing survives.

    A string is a plaintext literal when it is not a reference and either its own *key* is
    secret-shaped or it sits under a *strict* ancestor (a secret-shaped key other than the top-level
    ``secrets`` block, which is 1Password wiring and holds ordinary config such as ``enabled``).
    Non-string scalars (``enabled: true``, ``max_tokens: 4096``) are never credentials and stay.
    A list is dropped whole when it holds any literal.
    """
    secretish = bool(SECRET_KEY_RE.search(key))
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kept = scrub_literals(
                v, strict=strict or (secretish and key != "secrets"), key=str(k)
            )
            if kept is not _MISSING:
                out[k] = kept
        return out if out or not value else _MISSING
    if isinstance(value, list):
        leaky = any(isinstance(i, str) and i and not is_reference(i) for i in value)
        return _MISSING if leaky and (strict or secretish) else copy.deepcopy(value)
    if isinstance(value, str) and value and not is_reference(value) and (strict or secretish):
        return _MISSING
    return copy.deepcopy(value)


def reference_paths(tree: Any, path: tuple = ()) -> Dict[tuple, Any]:
    """Every credential-free leaf of *tree* below a secret-shaped key, by path tuple.

    That is each reference (``op://`` / ``${VAR}``) and each non-string scalar (``enabled: true``)
    -- everything under ``secrets`` or a ``*_token`` key that is safe to carry and costly to lose.
    """
    found: Dict[tuple, Any] = {}
    if isinstance(tree, dict):
        for key, val in tree.items():
            found.update(reference_paths(val, path + (str(key),)))
    elif (is_reference(tree) or isinstance(tree, (bool, int, float))) and any(
        SECRET_KEY_RE.search(part) for part in path
    ):
        found[path] = tree
    return found


def _lookup(tree: Any, path: tuple) -> Any:
    for part in path:
        if not isinstance(tree, dict) or part not in tree:
            return _MISSING
        tree = tree[part]
    return tree



def minimal_delta(
    current: Any,
    base: Any,
    *,
    history: list[dict] | None = None,
    path: str = "",
    _strict: bool = False,
) -> Any:
    """Return the smallest subtree of *current* that re-creates it over *base*.

    Invariant: ``deep_merge(base, minimal_delta(current, base))`` yields a dict
    whose every key present in *current* holds *current*'s value (plaintext secret literals aside).

    Filters keep a recovered delta honest rather than merely correct:

    * ``BASE_OWNED_KEYS`` is dropped outright. Plaintext credential literals (a string under a
      secret-shaped key) are dropped so they are never re-sprayed into a delta, but references
      (``op://...`` / ``${VAR}``) and ordinary config under the ``secrets`` block survive.
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
        if key in BASE_OWNED_KEYS:
            continue
        # Plaintext credential literals are never carried; references and plain config are.
        cur_val = scrub_literals(cur_val, strict=_strict, key=key)
        if cur_val is _MISSING:
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
                _strict=_strict or (bool(SECRET_KEY_RE.search(key)) and key != "secrets"),
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
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.render-", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(GENERATED_HEADER + dump_yaml(merged))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# per-profile state
# --------------------------------------------------------------------------
def pin_codegraph_root(config: dict) -> dict:
    """Never render a ``codegraph serve --mcp`` entry that can index the wrong tree.

    A Hermes gateway runs with cwd ``~/.hermes`` (not a git repo; ``installs/`` alone is ~200k
    vendored files). Hermes's MCP client does not send a project rootUri, so a ``codegraph serve
    --mcp`` with no ``--path`` indexed ``~/.hermes`` itself on 2026-10-11: 49.7 GiB of
    ``.codegraph/codegraph.db`` plus an unflushed WAL in 37 minutes. The desk's repo is the first
    ``skills.trusted_project_dirs`` entry the project binding wrote (a git checkout); pin it with
    ``--path``. A profile with no resolvable repo gets no codegraph server at all rather than one
    that indexes its cwd. ``codegraph install`` writes the unpinned form into ``~/.hermes/config.yaml``,
    so the pin has to be applied at render, not hoped for in the base.
    """
    servers = config.get("mcp_servers")
    entry = servers.get("codegraph") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return config
    args = [str(a) for a in (entry.get("args") or [])]
    if any(a in ("--path", "-p") or a.startswith("--path=") for a in args):
        return config
    trusted = ((config.get("skills") or {}).get("trusted_project_dirs") or [])
    repo = next((Path(str(d)).expanduser() for d in trusted if (Path(str(d)).expanduser() / ".git").exists()), None)
    if repo is None:
        print("note: dropped mcp_servers.codegraph (no git repo to --path it to)", file=sys.stderr)
        del servers["codegraph"]
        return config
    entry["args"] = [*args, "--path", str(repo)]
    return config


CWD_PLACEHOLDERS = frozenset({"", ".", "auto", "cwd"})


_WARNED_STALE_PROJECT_PATHS: set = set()


def stale_registry_project_path(profile: str, registry: Path | None = None) -> str | None:
    """The registry ``project_path`` of *profile* when it is set but unusable, else None.

    A row that names a project the renderer cannot pin (relative, deleted, moved) is the one case
    where ``terminal.cwd`` silently stays the placeholder ``.`` -- which the multiplexed gateway
    resolves to ``$HOME``, so every turn of that desk runs in, and is attributed to, the home
    directory. ``check`` reports it loudly instead of letting it pass as "in sync".
    """
    row = _registry_row(profile, registry)
    raw = str((row or {}).get("project_path") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return None if candidate.is_absolute() and candidate.is_dir() else raw


def _registry_row(profile: str, registry: Path | None = None) -> dict | None:
    path = registry or REGISTRY
    try:
        agents = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("agents") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    if not isinstance(agents, dict):
        return None
    for key, row in agents.items():
        if isinstance(row, dict) and (row.get("profile_name") or key) == profile:
            return row
    return None


def registry_project_path(profile: str, registry: Path | None = None) -> str | None:
    """The absolute project directory the org chart gives *profile*, or None.

    The registry row whose ``profile_name`` (or key) is *profile* and whose ``project_path`` is an
    absolute, existing directory. Anything else (portable specialist with no project, stale path,
    unreadable registry) is None, which leaves the profile's cwd behaviour exactly as it was. A
    stale path is warned about on stderr (once per profile per process) and by ``check``.
    """
    row = _registry_row(profile, registry)
    raw = str((row or {}).get("project_path") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() and candidate.is_dir():
        return str(candidate)
    if profile not in _WARNED_STALE_PROJECT_PATHS:
        _WARNED_STALE_PROJECT_PATHS.add(profile)
        print(
            f"WARN: {profile}: registry project_path {raw!r} is not an existing absolute directory; "
            "terminal.cwd stays '.', which the multiplexed gateway resolves to $HOME. "
            "Fix the agents-registry.yaml row (re-run 80-registry.sh), then repin.",
            file=sys.stderr,
        )
    return None


def pin_terminal_cwd(config: dict, profile: str, registry: Path | None = None) -> dict:
    """Give a PM desk its project as ``terminal.cwd`` instead of the placeholder ``.``.

    Under the ONE multiplexed gateway (``WorkingDirectory=~/.hermes``) Hermes resolves a
    placeholder ``terminal.cwd`` ('.', 'auto', 'cwd' or unset) for a local backend to ``$HOME``
    (``gateway/cwd_placeholder.py``; ``tools/terminal_scope.py`` per routed turn), so every
    gateway turn ran in /home/delorenj: the terminal tool, context-file discovery, and the
    ``working_directory`` the Bloodbank artifact sidecar attributes a reply by. Before the
    2026-10-10 multiplex each PM had its own gateway unit whose cwd WAS its repo, which is why
    ``.`` used to work. The org chart (``agents-registry.yaml`` ``project_path``) names the
    project, so the renderer writes it. Hermes reads an absolute ``terminal.cwd`` in both the
    per-profile terminal scope and the standalone bridge, so one value serves every surface.
    An explicit non-placeholder ``terminal.cwd`` in a delta is an operator choice and wins; a
    profile with no ``project_path`` keeps the placeholder.
    """
    terminal = config.get("terminal")
    if terminal is None:
        terminal = {}
    if not isinstance(terminal, dict):
        return config
    current = terminal.get("cwd")
    if str(current if current is not None else "").strip() not in CWD_PLACEHOLDERS:
        return config
    project = registry_project_path(profile, registry)
    if project:
        config["terminal"] = {**terminal, "cwd": project}
    return config


# --------------------------------------------------------------------------
# THE expected config: base + delta + every renderer pin
# --------------------------------------------------------------------------
# Dotted paths whose generated value the RENDERER owns (derived from the host or the org chart, never
# from the delta). A consumer that compared config.yaml against a bare deep_merge(base, delta) called
# every pinned desk "out-of-band drift" and, when it wrote, stripped the pin again (2026-10-18).
RENDER_PIN_PATHS = ("terminal.cwd", "mcp_servers.codegraph")


def apply_render_pins(config: dict, profile: str, *, registry: Path | None = None) -> dict:
    """Every renderer pin, in render order. Add a new pin HERE and in ``RENDER_PIN_PATHS`` only."""
    return pin_terminal_cwd(pin_codegraph_root(config), profile, registry)


def expected_config(base: dict, delta: dict, profile, *, registry: Path | None = None) -> dict:
    """THE generated ``config.yaml`` for *profile*: ``deep_merge(base, delta)`` plus every renderer pin.

    The one definition of "expected config". ``render`` writes it, ``check`` compares against it, and
    every other reader or writer of a generated profile config -- skills-policy, project-skills, the
    Skillex cutover, flume's role projection and hire restriction, the specialist publisher -- must
    call this (``import`` the renderer, or ``hermes-profile-config.py expected``) rather than
    re-assembling ``deep_merge`` + pins itself. *profile* is the profile name or its directory.
    """
    name = profile.name if isinstance(profile, Path) else str(profile)
    return apply_render_pins(deep_merge(base, delta), name, registry=registry)


def without_render_pins(config: Any) -> Any:
    """*config* with every renderer-owned path removed (an emptied parent mapping is dropped too)."""
    if not isinstance(config, dict):
        return copy.deepcopy(config)
    out = copy.deepcopy(config)
    for dotted in RENDER_PIN_PATHS:
        parts = dotted.split(".")
        chain = [out]
        for part in parts[:-1]:
            child = chain[-1].get(part)
            if not isinstance(child, dict):
                break
            chain.append(child)
        else:
            chain[-1].pop(parts[-1], None)
            for depth in range(len(chain) - 1, 0, -1):
                if chain[depth]:
                    break
                chain[depth - 1].pop(parts[depth - 1], None)
    return out


def is_render_of(current: Any, base: dict, delta: dict, profile, *, registry: Path | None = None) -> bool:
    """Whether *current* is a render of base + delta: exact, or stale only on renderer-owned pins.

    A pin-only difference is not out-of-band drift: it is a config rendered before the org chart (or
    the host) gave the renderer the pin's input -- a fresh hire's step 10 runs before 80-registry.sh
    writes its row -- or rendered by a writer that predates the pin. Writers accept it and write
    ``expected_config``; ``check`` still reports it, because the file on disk is not yet what the
    runtime should read.
    """
    if not isinstance(current, dict):
        return False
    expected = expected_config(base, delta, profile, registry=registry)
    return current == expected or without_render_pins(current) == without_render_pins(expected)


def profile_state(pdir: Path, base: dict) -> Tuple[dict, dict, dict, bool]:
    """Return (delta, current_cfg, expected_render, is_symlink)."""
    cfg_path = pdir / "config.yaml"
    delta_path = pdir / "config.delta.yaml"
    is_link = cfg_path.is_symlink()
    delta = load_yaml(delta_path)
    current = load_yaml(cfg_path)
    expected = expected_config(base, delta, pdir.name)
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
        with PROFILE_LOCK.ProfileConfigLock(pdir):
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

            merged = expected_config(base, delta, pdir.name)
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
        with PROFILE_LOCK.ProfileConfigLock(pdir):
            delta, _cur, expected, _link = profile_state(pdir, base)
            print(
                f"render {pdir.name:36s} "
                f"({len(dump_yaml(delta).splitlines()) if delta else 0} delta lines)"
            )
            if not args.dry_run:
                write_generated(pdir / "config.yaml", expected)
    return 0


def cmd_check(args) -> int:
    base = load_yaml(BASE)
    drifted = []
    pin_only = []
    stale = []
    for pdir in _select(args):
        with PROFILE_LOCK.ProfileConfigLock(pdir):
            delta, current, expected, is_link = profile_state(pdir, base)
            raw = stale_registry_project_path(pdir.name)
            if raw is not None:
                stale.append((pdir.name, raw))
            if is_link:
                drifted.append(
                    (pdir.name, "config.yaml is a SYMLINK (no override capability)")
                )
            elif not (pdir / "config.delta.yaml").exists():
                drifted.append((pdir.name, "no config.delta.yaml (not under inheritance)"))
            elif current != expected:
                keys = sorted(
                    k
                    for k in set(current) | set(expected)
                    if current.get(k) != expected.get(k)
                )
                drifted.append((pdir.name, f"drift in: {', '.join(keys[:6])}"))
                if without_render_pins(current) == without_render_pins(expected):
                    pin_only.append(pdir.name)
    if drifted:
        print("PROFILE CONFIG DRIFT:\n")
        for name, why in drifted:
            print(f"  {name:36s} {why}")
        print(f"\n{len(drifted)} profile(s) drifted. Fix: hermes-profile-config.py render --all")
        if pin_only:
            print(
                f"{len(pin_only)} of them differ ONLY on renderer pins ({', '.join(RENDER_PIN_PATHS)}): "
                "hermes-profile-config.py repin --all re-renders exactly those."
            )
    else:
        print("OK: every profile config.yaml == expected_config(base, delta) (deep_merge + renderer pins)")
    if stale:
        # Loud, but after the verdict line: flume's observer keys on the first line ("OK:" / "PROFILE
        # CONFIG DRIFT:"). A stale path is not drift -- the render is faithful -- it is a desk whose
        # every gateway turn runs in $HOME.
        print(f"\nWARN: {len(stale)} profile(s) have a STALE registry project_path; terminal.cwd falls back to '.' ($HOME under the multiplexed gateway):")
        for name, raw in stale:
            print(f"WARN:   {name:34s} project_path={raw!r} is not an existing absolute directory")
        for name, raw in stale:
            print(f"WARN: {name}: stale registry project_path {raw!r}", file=sys.stderr)
    return 1 if drifted else 0


def cmd_repin(args) -> int:
    """Re-render only profiles whose config differs from the expected render ONLY on renderer pins.

    The pin inputs live outside the delta (the org chart's ``project_path``, the desk's repo), so a
    change there -- 80-registry.sh writing a fresh hire's row, a project moving -- leaves a faithful
    render stale. This rewrites exactly those configs and refuses (exit 1) any profile with real
    out-of-band drift, which ``absorb`` must fold into the delta first. Profiles that are a symlink or
    have no delta are skipped: they are not under inheritance.
    """
    base = load_yaml(BASE)
    refused = 0
    for pdir in _select(args):
        with PROFILE_LOCK.ProfileConfigLock(pdir):
            cfg_path = pdir / "config.yaml"
            if cfg_path.is_symlink() or not (pdir / "config.delta.yaml").is_file():
                print(f"skip   {pdir.name:36s} (not under inheritance)")
                continue
            delta, current, expected, _link = profile_state(pdir, base)
            if current == expected:
                print(f"ok     {pdir.name}")
            elif without_render_pins(current) == without_render_pins(expected):
                print(f"repin  {pdir.name}")
                if not args.dry_run:
                    write_generated(cfg_path, expected)
            else:
                refused += 1
                print(f"DRIFT  {pdir.name:36s} out-of-band edits beyond renderer pins; review with `absorb --profile {pdir.name} --dry-run` first (never sweep absorb across profiles)")
    return 1 if refused else 0


def cmd_expected(args) -> int:
    """Print THE expected config for one profile as JSON, for consumers that are not Python.

    ``--delta-file`` evaluates a proposed delta instead of the profile's own (``-`` reads stdin).
    """
    import json

    if not args.profile:
        sys.exit("FATAL: expected requires --profile")
    pdir = _select(args)[0]
    if args.delta_file == "-":
        delta = yaml.safe_load(sys.stdin.read()) or {}
    elif args.delta_file:
        delta = load_yaml(Path(args.delta_file))
    else:
        delta = load_yaml(pdir / "config.delta.yaml")
    print(json.dumps(expected_config(load_yaml(BASE), delta, pdir.name), indent=2, sort_keys=False, default=str))
    return 0


def cmd_absorb(args) -> int:
    base = load_yaml(BASE)
    targets = _select(args)
    if not args.dry_run:
        loc = backup([p / "config.delta.yaml" for p in targets], "absorb")
        if loc:
            print(f"backup -> {loc}\n")
    for pdir in targets:
        with PROFILE_LOCK.ProfileConfigLock(pdir):
            cfg_path = pdir / "config.yaml"
            if cfg_path.is_symlink():
                print(f"skip   {pdir.name:36s} (symlink -- run init first)")
                continue
            old_delta = load_yaml(pdir / "config.delta.yaml")
            current = load_yaml(cfg_path)
            directive = copy.deepcopy(old_delta.get(LIST_PATCH_KEY))
            effective_base = expected_config(
                base, {LIST_PATCH_KEY: directive} if directive is not None else {}, pdir.name
            )
            new_delta = minimal_delta(current, effective_base)
            if directive is not None:
                new_delta[LIST_PATCH_KEY] = directive
            # Belt and braces: a secret reference (and its sibling switches) the old delta carried is never silently deleted.
            # minimal_delta already keeps references; this catches any path it still cannot see
            # (a stripped config.yaml, a future filter) and carries the reference over instead.
            carried = []
            for ref_path, ref_val in sorted(reference_paths(old_delta).items()):
                if _lookup(new_delta, ref_path) is not _MISSING:
                    continue
                if _lookup(effective_base, ref_path) == ref_val:
                    continue
                node = new_delta
                for part in ref_path[:-1]:
                    node = node.setdefault(part, {})
                node[ref_path[-1]] = ref_val
                carried.append(".".join(ref_path))
            if carried:
                print(
                    f"keep   {pdir.name:36s} carried {len(carried)} secret reference(s) "
                    f"over: {', '.join(carried[:6])}"
                )
            if new_delta == old_delta:
                print(f"clean  {pdir.name}")
                continue
            added = sorted(set(new_delta) - set(old_delta))
            print(
                f"absorb {pdir.name:36s} +{len(added)} key(s): "
                f"{', '.join(added[:6]) or '(nested)'}"
            )
            if not args.dry_run:
                (pdir / "config.delta.yaml").write_text(
                    "# Override-only delta for this Hermes profile.\n\n"
                    + dump_yaml(new_delta),
                    encoding="utf-8",
                )
                os.chmod(pdir / "config.delta.yaml", 0o600)
    return 0


def cmd_status(args) -> int:
    base = load_yaml(BASE)
    print(f"{'profile':36s} {'mode':10s} {'delta':>5s}  {'state'}")
    print("-" * 78)
    for pdir in _select(args):
        with PROFILE_LOCK.ProfileConfigLock(pdir):
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
    re-symlink cannot silently merge two agents' private memory. A travelling
    named agent may pass ``--bank-id agent-<identity>``; that declaration is
    deliberately exact and is not derived from the current post/profile name.
    """
    import json

    if args.bank_id and not args.profile:
        sys.exit("FATAL: --bank-id requires --profile; one declared personal bank cannot be applied to every profile")
    if args.bank_id and args.only_broken:
        sys.exit("FATAL: --bank-id cannot be combined with --only-broken")
    if args.bank_id and __import__("re").fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.bank_id) is None:
        sys.exit("FATAL: --bank-id must be a lower-case bank identifier")

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
        bank_id = args.bank_id or f"agent-{name}"
        payload["bank_id"] = bank_id
        payload.pop("bank_id_template", None)  # explicit pin wins; no ambiguity
        why = "declared bank" if args.bank_id else ("resolver-ok" if ok else "resolver would yield 'custom'")
        print(f"pin {name:34s} -> {bank_id:32s} ({why})")
        if args.dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        _ensure_runtime_ignored(target)
        pinned += 1
    print(f"\npinned {pinned}, skipped {skipped}")
    return 0


def _template_config_path() -> Path:
    """The host's hermes-agent-template config.toml, resolved like _lib.sh does."""
    explicit = os.environ.get("HERMES_TEMPLATE_CONFIG")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "hermes-agent-template" / "config.toml"


def _configured_agent_bank_template() -> str:
    """``[hindsight] agent_bank_template`` from config.toml, expanded, or ""."""
    path = _template_config_path()
    if not path.is_file():
        return ""
    try:
        import tomllib
    except ImportError:  # Python < 3.11: no stdlib TOML reader
        return ""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"FATAL: cannot parse {path}: {exc}")
    value = (data.get("hindsight") or {}).get("agent_bank_template") or ""
    if not isinstance(value, str):
        sys.exit(f"FATAL: {path}: [hindsight] agent_bank_template must be a string")
    return os.path.expandvars(os.path.expanduser(value.strip())) if value.strip() else ""


def cmd_memory_template(args) -> int:
    """Record the bank template an agent's identity bank should start from.

    Writes ``bank_template`` into ``<profile>/hindsight/config.json`` and
    touches nothing else there (``bank_id`` stays whatever memory-pin set).
    The Hermes Hindsight provider imports that manifest once, the first time
    it initializes against a bank that has no mission of its own, and never
    over a mission already on the bank. So this is safe to run on a desk
    whose bank is already steered: it records intent, the bank is unchanged.

    The template comes from ``--bank-template``, else from config.toml
    ``[hindsight] agent_bank_template``. ``--bank-template ''`` removes it.
    """
    import json

    explicit = args.bank_template is not None
    template = args.bank_template if explicit else _configured_agent_bank_template()
    template = os.path.expandvars(os.path.expanduser(template.strip())) if template else ""
    if not template and not explicit:
        print("no [hindsight] agent_bank_template configured in "
              f"{_template_config_path()}; nothing to record")
        return 0
    if template:
        tpl = Path(template)
        if not tpl.is_file():
            sys.exit(f"FATAL: bank template not found: {template}")
        try:
            manifest = json.loads(tpl.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.exit(f"FATAL: bank template is not JSON: {template}: {exc}")
        bank = manifest.get("bank") if isinstance(manifest, dict) else None
        if not isinstance(bank, dict) or not any(
            bank.get(key) for key in ("retain_mission", "reflect_mission", "observations_mission")
        ):
            sys.exit(f"FATAL: {template} sets no mission in its 'bank' block; the provider "
                     "could not tell an imported bank from an unsteered one")
        template = str(tpl.resolve())

    changed = 0
    for pdir in _select(args):
        target = pdir / "hindsight" / "config.json"
        if not target.is_file() or target.is_symlink():
            print(f"skip {pdir.name:34s} (no pinned hindsight/config.json; run memory-pin first)")
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.exit(f"FATAL: {target} is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            sys.exit(f"FATAL: {target} is not a JSON object")
        before = payload.get("bank_template")
        if template:
            payload["bank_template"] = template
        else:
            payload.pop("bank_template", None)
        if payload.get("bank_template") == before:
            print(f"ok   {pdir.name:34s} bank_template={before or '(none)'}")
            continue
        print(f"set  {pdir.name:34s} bank_template={template or '(removed)'}")
        if args.dry_run:
            continue
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        changed += 1
    print(f"\nchanged {changed}")
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
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.profile) is None:
            sys.exit("FATAL: invalid profile name")
        pdir = PROFILES / args.profile
        if not pdir.is_dir():
            sys.exit(f"FATAL: no such profile: {args.profile}")
        return [pdir]
    return profile_dirs()


def load_policy_helper(name):
    source = Path(__file__).resolve().parent.parent / "template/.scripts/lib" / name
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"trusted policy helper unavailable: {source}")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), source)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load trusted policy helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_skills_policy(args) -> int:
    if not args.profile:
        raise RuntimeError("skills-policy requires --profile; select a single desk explicitly")
    helper = load_policy_helper("skills-policy.py")
    for profile in _select(args):
        outcome = helper.set_policy(profile, renderer=sys.modules[__name__], dry_run=args.dry_run)
        print(f"skills-policy {profile.name}: {outcome}")
    return 0


def cmd_project_skills(args) -> int:
    helper = load_policy_helper("project-skills.py")
    return helper.command(args, sys.modules[__name__], load_policy_helper("skills-policy.py"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in [
        ("init", cmd_init, "recover deltas from current configs and render"),
        ("render", cmd_render, "regenerate config.yaml from base + delta"),
        ("check", cmd_check, "drift gate (non-zero exit on drift)"),
        ("repin", cmd_repin, "re-render configs that differ from the expected render ONLY on renderer pins"),
        ("expected", cmd_expected, "print THE expected config (base + delta + renderer pins) for one profile as JSON"),
        ("absorb", cmd_absorb, "fold out-of-band config.yaml edits into the delta"),
        ("status", cmd_status, "show delta size and drift state per profile"),
        ("skills-policy", cmd_skills_policy, "enforce strict loadout through a locked base+delta transaction"),
        ("memory-pin", cmd_memory_pin, "pin each agent's identity-memory bank explicitly"),
        ("memory-template", cmd_memory_template,
         "record the bank template the Hindsight provider imports into an unsteered identity bank"),
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
        p.add_argument(
            "--bank-id",
            help="memory-pin: pin one profile to this explicit named-agent bank; requires --profile",
        )
        p.add_argument(
            "--bank-template",
            help="memory-template: manifest path (default: config.toml [hindsight] "
            "agent_bank_template); '' removes it",
        )
        p.add_argument(
            "--delta-file",
            help="expected: evaluate this delta instead of the profile's own ('-' reads stdin)",
        )
        p.set_defaults(func=fn)
    project = sub.add_parser("project-skills", help="configure exact trust for evidenced PM bindings and report native discovery")
    scope = project.add_mutually_exclusive_group(required=True)
    scope.add_argument("--profile")
    scope.add_argument("--registered-pms", action="store_true")
    project.add_argument("--project-root", help="explicit canonical project, never inferred from Git")
    project.add_argument("--role-dir", help="role corroborating the explicit canonical project")
    project.add_argument("--registry", default=str(HERMES_HOME / "agents-registry.yaml"))
    project.add_argument("--dry-run", action="store_true")
    project.add_argument("--json", action="store_true")
    project.set_defaults(func=cmd_project_skills)
    args = ap.parse_args()
    if not BASE.exists():
        sys.exit(f"FATAL: fleet base not found: {BASE}")
    try:
        return args.func(args)
    except PROFILE_LOCK.ProfileConfigLockError as exc:
        sys.exit(f"FATAL: {exc}")
    except (RuntimeError, ValueError, OSError) as exc:
        sys.exit(f"FATAL: {exc}")


if __name__ == "__main__":
    sys.exit(main())
