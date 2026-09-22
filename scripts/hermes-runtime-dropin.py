#!/usr/bin/env python3
"""Generate the ``10-versioned-runtime.conf`` systemd drop-in for each gateway.

Why this exists
---------------
Twenty gateway units carried this drop-in with **no generator anywhere on
disk** and no commit that ever contained the string (design-workforce-
delegation.md §7.4). It pins the runtime release and, since 2026-09-21, the
``-p <profile>`` flag that `hermes profile list` needs to recognise a running
gateway at all (§7.5.1). All of that lived only on one disk, in no repo: one
regeneration by hand and every gateway silently reports "stopped" again.

The drop-in is a pure function of (base unit, pinned release), so this needs
no new source of truth. Everything per-unit is read back out of the unit that
systemd already has:

    profile   <- Environment=HERMES_HOME=...  (only when under ~/.hermes/profiles/)
    cwd       <- agents-registry.yaml project_path, else the base unit's
                 WorkingDirectory, else its TERMINAL_CWD, else whatever the
                 existing drop-in already says
    release   <- unanimous HERMES_RUNTIME_RELEASE across existing drop-ins,
                 or --release

The registry is primary because three units' base files carry no cwd at all --
delocontainers-pm's says only HERMES_HOME -- and the last fallback exists
because three units (automatic-ai-pm, fleet-bloodbank-gateway,
intelliforia-voice-agent-pm) are in NO registry entry and must not silently
lose their working directory to a regeneration.

Rules that are easy to get wrong
--------------------------------
* **A drop-in's ExecStart REPLACES the base's**, so a unit whose base execs
  ``credential-launch.sh`` but which already carries a drop-in is running hermes
  directly, bypassing that launcher. That is the existing arrangement for most of
  the fleet and this regenerates it as-is; it does not move anyone onto or off
  the wrapper. A wrapper-launched unit with NO drop-in is left alone entirely --
  deckard/infra/ssbnk/TonnyBox/voxxy, whose ``-p`` lives inside
  ``credential-launch.sh`` because that wrapper validates ``$1`` as its mode and
  exits 2 on an unexpected token.
* **``-p`` only for a real profile.** hermes-agent-pm's HERMES_HOME is
  ``~/.hermes/hermes-agent/agents/hermes/pm/runtime``, not a profile, so it gets
  the release pin and no flag -- ``-p runtime`` would resolve to nothing.
* **``WorkingDirectory`` is emitted only when the base unit lacks one**, which is
  what the hand-written files did; re-asserting it everywhere would rewrite three
  units for no reason.
* Only ``10-versioned-runtime.conf`` is ever written. Siblings
  (``20-start-limit.conf``, ``vox-voice.conf``) and anything ``.quarantined-*``
  are left untouched.

Usage
-----
    hermes-runtime-dropin.py            # check (read-only); exit 1 on drift
    hermes-runtime-dropin.py apply      # write drift, then daemon-reload
    hermes-runtime-dropin.py apply --release <sha>

``apply`` never restarts a gateway: a drop-in change only takes effect on the
next start, and restarting 15 agents costs ~19 1Password reads each against a
~1000/day account budget. Restart deliberately, when you mean to.
"""
from __future__ import annotations

import argparse
import re

try:
    import yaml
except ImportError:  # registry is a nicety, not a hard dep
    yaml = None
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path.home() / ".config/systemd/user"
PROFILES_DIR = Path.home() / ".hermes/profiles"
RELEASES_DIR = Path.home() / ".local/share/hermes-agent/releases"
DROPIN_NAME = "10-versioned-runtime.conf"
REGISTRY = Path.home() / ".hermes/agents-registry.yaml"

# Env the drop-in pins on every gateway, in emission order. 1Password's desktop
# integrations must stay off: a headless gateway has no biometric prompt to
# answer and would block on one.
OP_FLAGS = (
    ("OP_BIOMETRIC_UNLOCK_ENABLED", "false"),
    ("OP_LOAD_DESKTOP_APP_SETTINGS", "false"),
    ("OP_SESSION_DELEGATION_ENABLED", "false"),
)

_ENV_RE = re.compile(r'^Environment=(?:"([^"=]+)=([^"]*)"|([^=\s]+)=(.*))$')


def read_unit(unit: Path) -> str:
    """Base unit text only -- never the merged view, which already has our output."""
    return unit.read_text(encoding="utf-8", errors="replace")


def unit_env(text: str, key: str) -> str | None:
    """Last Environment= wins, matching systemd, and both quoting styles occur here."""
    found = None
    for line in text.splitlines():
        m = _ENV_RE.match(line.strip())
        if not m:
            continue
        name = m.group(1) or m.group(3)
        if name == key:
            found = (m.group(2) if m.group(1) else m.group(4)).strip()
    return found


def unit_directive(text: str, key: str) -> str | None:
    found = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and line != f"{key}=":
            found = line[len(key) + 1 :].strip().strip('"')
    return found


def registry_paths() -> dict[str, str]:
    """profile_name -> project_path. Empty when the registry is absent/unreadable."""
    if yaml is None or not REGISTRY.is_file():
        return {}
    try:
        agents = (yaml.safe_load(REGISTRY.read_text()) or {}).get("agents") or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for agent_id, entry in agents.items():
        if not isinstance(entry, dict):
            continue
        path = entry.get("project_path")
        if path:
            out[entry.get("profile_name") or agent_id] = str(path)
    return out


def is_direct_hermes_exec(exec_start: str | None) -> bool:
    """True when the unit execs a hermes binary rather than a launcher script."""
    if not exec_start:
        return False
    first = exec_start.strip().strip('"').split()[0]
    return Path(first).name == "hermes"


def detect_release(units: list[Path]) -> str:
    """The release every existing drop-in agrees on; ambiguity is an error, not a guess."""
    seen: set[str] = set()
    for unit in units:
        dropin = UNIT_DIR / f"{unit.name}.d" / DROPIN_NAME
        if not dropin.is_file():
            continue
        value = unit_env(dropin.read_text(encoding="utf-8", errors="replace"), "HERMES_RUNTIME_RELEASE")
        if value:
            seen.add(value)
    if len(seen) == 1:
        return seen.pop()
    if not seen:
        raise SystemExit(
            "no existing drop-in to infer the release from — pass --release <sha>"
        )
    raise SystemExit(
        "existing drop-ins disagree on HERMES_RUNTIME_RELEASE "
        f"({', '.join(sorted(seen))}) — pass --release <sha> to settle it"
    )


def render(*, release: str, profile: str | None, cwd: str | None, need_workdir: bool) -> str:
    venv = f"{RELEASES_DIR}/{release}/.venv/bin"
    flag = f" -p {profile}" if profile else ""
    lines = [
        "[Service]",
        "ExecStart=",
        f"ExecStart={venv}/hermes{flag} gateway run --replace",
    ]
    if need_workdir and cwd:
        lines.append(f"WorkingDirectory={cwd}")
    lines.append(f"Environment=HERMES_RUNTIME_RELEASE={release}")
    if cwd:
        lines.append(f"Environment=TERMINAL_CWD={cwd}")
    lines.append(
        f"Environment=PATH={Path.home()}/.local/bin:{venv}:/usr/local/bin:/usr/bin:/bin"
    )
    lines += [f"Environment={k}={v}" for k, v in OP_FLAGS]
    return "\n".join(lines) + "\n"


def plan(release: str) -> tuple[list[tuple[Path, str, str]], list[tuple[str, str]]]:
    """Return (writes, skips). A write is (path, desired, reason-if-changed)."""
    writes: list[tuple[Path, str, str]] = []
    skips: list[tuple[str, str]] = []
    reg = registry_paths()
    for unit in sorted(UNIT_DIR.glob("hermes-*-gateway.service")):
        text = read_unit(unit)
        path = UNIT_DIR / f"{unit.name}.d" / DROPIN_NAME
        current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

        # In scope: anything that already has a drop-in (regenerate it), plus a
        # unit whose base execs hermes directly (a new agent). A wrapper-launched
        # unit with no drop-in is not ours to create one for.
        if current is None and not is_direct_hermes_exec(unit_directive(text, "ExecStart")):
            skips.append((unit.name, "wrapper-launched, no drop-in (flag lives in its launcher)"))
            continue

        home = unit_env(text, "HERMES_HOME")
        profile = None
        if home and Path(home).parent == PROFILES_DIR:
            profile = Path(home).name
        elif home:
            skips.append((unit.name, f"HERMES_HOME is not a profile ({home}) — release pinned, no -p"))

        base_workdir = unit_directive(text, "WorkingDirectory")
        cwd = (
            (reg.get(profile) if profile else None)
            or base_workdir
            or unit_env(text, "TERMINAL_CWD")
            or (unit_env(current, "TERMINAL_CWD") if current else None)
        )
        if profile and profile not in reg:
            skips.append((unit.name, "no agents-registry entry — cwd preserved from disk"))

        desired = render(
            release=release, profile=profile, cwd=cwd, need_workdir=base_workdir is None
        )
        if current != desired:
            writes.append((path, desired, "missing" if current is None else "differs"))
    return writes, skips


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", default="check", choices=["check", "apply"])
    p.add_argument("--release", help="runtime release sha to pin (default: infer from existing drop-ins)")
    p.add_argument("--verbose", action="store_true", help="list skipped units too")
    args = p.parse_args(argv)

    if not UNIT_DIR.is_dir():
        print(f"no systemd user unit dir at {UNIT_DIR}", file=sys.stderr)
        return 1

    release = args.release or detect_release(sorted(UNIT_DIR.glob("hermes-*-gateway.service")))
    if not (RELEASES_DIR / release / ".venv/bin/hermes").is_file():
        print(f"release {release} has no hermes binary under {RELEASES_DIR}", file=sys.stderr)
        return 1

    writes, skips = plan(release)

    if args.verbose:
        for name, why in skips:
            print(f"  skip  {name}: {why}")

    if not writes:
        print(f"runtime-dropin: OK — every gateway drop-in matches release {release[:12]}")
        return 0

    if args.mode == "check":
        print(f"runtime-dropin: {len(writes)} drop-in(s) out of date (run `apply`):")
        for path, _, why in writes:
            print(f"  - {path.parent.name}/{path.name} ({why})")
        return 1

    for path, desired, why in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desired, encoding="utf-8")
        print(f"runtime-dropin: wrote {path.parent.name}/{path.name} ({why})")
    reload_ = subprocess.run(
        ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
    )
    if reload_.returncode:
        print(f"runtime-dropin: daemon-reload failed: {reload_.stderr.strip()}", file=sys.stderr)
        return 1
    print(
        f"runtime-dropin: {len(writes)} file(s) written, daemon-reload done. "
        "Each gateway picks this up on its NEXT start — restart deliberately."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
