#!/usr/bin/env python3
"""Hermes drift guard: every consumer must resolve to ONE install and ONE sha.

Why this exists
---------------
Hermes ran from two code lines on big-chungus for two months: the CLI wrappers
(and the registry) pinned an "immutable" release directory, while ``hermes`` on
PATH and ``hermes-gateway.service`` ran the dev checkout. Neither line was a
superset of the other, so the same prompt took different code paths depending on
whether it arrived through Telegram or through ``agents/hermes/pm/hermes``
(2026-10-17: CLI turns published no ``final_response`` for the artifact sidecar,
gateway turns did). Nothing noticed, because nothing compared the consumers.

This compares them. Each consumer is reduced to the install root it execs (the
nearest ancestor directory holding ``.git``, after ``realpath``) and the HEAD
sha of that root:

    gateway     systemd user units + drop-ins that exec a hermes binary (Linux), or
                ~/Library/LaunchAgents/ai.hermes.gateway*.plist ProgramArguments (macOS)
    PATH        ~/.local/bin/hermes{,-acp,-agent}
    fleet.env   HERMES_FLEET_BIN / HERMES_FLEET_REPO
    config      ~/.config/hermes-agent-template/config.toml [fleet]
    registry    agents-registry.yaml hermes.bin / hermes.repo / hermes.git_sha
    wrappers    agents/hermes/*/hermes (embedded default; the env/config value wins at run time)

FAIL (exit 1)
    - two consumers resolve to different install roots
    - a binary does not exist
    - a registry git_sha is not in the install's history (a different line)
    - the install is not tracking a ``delorenj/*`` remote branch
    - the install is a blobless partial clone (cannot push the work done in it)
WARN (exit 0)
    - a wrapper's embedded fallback names another path (shadowed by env/config today)
    - a registry git_sha is an ancestor of HEAD but behind it (refresh with fleet-sync)
    - the running gateway process booted from a different sha than HEAD (restart needed)

Usage
-----
    hermes-single-install-check.py            # human report, exit 1 on FAIL
    hermes-single-install-check.py --json     # machine report
    hermes-single-install-check.py --quiet    # only FAIL/WARN lines

On a pass it touches ``~/.cache/hermes-single-install/ok`` so a periodic
``file_age`` probe (infra-doctor) goes red when the guard stops passing.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:  # registry parsing is a nicety; a regex fallback keeps the guard stdlib-only
    import yaml
except ImportError:  # pragma: no cover
    yaml = None
try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

BIN_NAMES = ("hermes", "hermes-acp", "hermes-agent")


class Report:
    def __init__(self) -> None:
        self.consumers: list[dict] = []
        self.fails: list[str] = []
        self.warns: list[str] = []

    def consumer(self, kind: str, label: str, declared: str, root: str | None, sha: str | None, note: str = "") -> None:
        self.consumers.append(
            {"kind": kind, "label": label, "declared": declared, "root": root, "sha": sha, "note": note}
        )


def git(root: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", root, *args],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def install_root(path: str | os.PathLike) -> str | None:
    """Nearest ancestor of realpath(path) holding a .git; None when the path is dangling."""
    try:
        cur = Path(os.path.realpath(path))
    except OSError:
        return None
    if not cur.exists():
        return None
    if cur.is_file():
        # A tiny launcher script (~/.local/bin/hermes) resolves to whatever it execs.
        inner = exec_target_of_script(cur) if cur.stat().st_size < 65536 else None
        if inner and inner != str(cur) and Path(inner).name.startswith("hermes") and Path(inner).exists():
            return install_root(inner)
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return str(candidate)
        if str(candidate) == "/":
            break
    return None


def expand(value: str, home: Path) -> str:
    value = value.strip().strip("'\"")
    value = value.replace("$HOME", str(home)).replace("${HOME}", str(home))
    if value.startswith("~"):
        value = str(home) + value[1:]
    return value


def read_env_file(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)=(.*)$", line)
        if m and m.group(1) in keys:
            try:
                parts = shlex.split(m.group(2), posix=True)
            except ValueError:
                parts = [m.group(2)]
            out[m.group(1)] = parts[0] if parts else ""
    return out


def exec_target_of_script(path: Path) -> str | None:
    """The hermes binary a tiny launcher script execs (``exec /path/hermes "$@"``)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r"^\s*exec\s+(?:env\s+\S+=\S+\s+)*[\"']?(/[^\s\"']+)[\"']?", line)
        if m:
            return m.group(1)
    return None


def systemd_execs(home: Path) -> list[tuple[str, str]]:
    """(label, binary) for every hermes ExecStart in active/enabled user units and their drop-ins."""
    unit_dir = home / ".config/systemd/user"
    found: list[tuple[str, str]] = []
    if not unit_dir.is_dir():
        return found
    for unit in sorted(unit_dir.glob("hermes*.service")):
        enabled = (unit_dir / "default.target.wants" / unit.name).exists() or any(
            (unit_dir / f"{t}.wants" / unit.name).exists() for t in ("default.target", "multi-user.target")
        )
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit.name], capture_output=True, text=True, check=False
        ).stdout.strip() == "active" if os.environ.get("HERMES_GUARD_NO_SYSTEMCTL") != "1" else False
        if not (enabled or active):
            continue  # a disabled leftover execs nothing
        files = [unit, *sorted((unit_dir / f"{unit.name}.d").glob("*.conf"))]
        last: str | None = None
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                m = re.match(r"^\s*ExecStart=(.*)$", line)
                if not m:
                    continue
                value = m.group(1).strip()
                if not value:
                    last = None  # an empty ExecStart= resets the list
                    continue
                token = value.lstrip("-@+!:").strip()
                token = shlex.split(token)[0] if token else ""
                last = token or last
        if last and Path(last).name.startswith("hermes"):
            found.append((f"systemd:{unit.name}", last))
        elif last and Path(last).is_file():  # a launcher script: look inside for the binary it execs
            inner = exec_target_of_script(Path(last)) or ""
            found.append((f"systemd:{unit.name} via {Path(last).name}", inner or last))
    return found


LAUNCHD_EXEC_QUOTED = re.compile(r"exec\s+(?:'([^']+)'|(/[^\s'\"\\]+))")
LAUNCHD_SYS_PATH = re.compile(r"sys\.path\.insert\(0,\s*['\"]([^'\"]+)['\"]\)")


def launchd_program_binary(arguments: list[str]) -> str | None:
    """The hermes binary a LaunchAgent's ProgramArguments run.

    The fork writes one of three shapes (hermes_cli/gateway_launchd.py):
      [<install>/.hermes/bin/hermes, ...]                    direct launcher
      [/usr/bin/osascript, -l, JavaScript, -e, <js>]         <js> holds `exec <hermes> ... >> log`
      [<python>, -c, <bootstrap>]                            bootstrap does sys.path.insert(0, <install>)
    """
    if not arguments:
        return None
    first = arguments[0]
    if Path(first).name.startswith("hermes") and first.startswith("/"):
        return first
    for argument in arguments[1:]:
        match = LAUNCHD_EXEC_QUOTED.search(argument)
        if match:
            found = match.group(1) or match.group(2)
            if Path(found).name.startswith("hermes"):
                return found
        match = LAUNCHD_SYS_PATH.search(argument)
        if match:
            return str(Path(match.group(1)) / ".hermes/bin/hermes")
    return None


def launchd_execs(home: Path) -> list[tuple[str, str]]:
    """(label, binary) for every hermes LaunchAgent: ~/Library/LaunchAgents/ai.hermes.gateway*.plist.

    A plist that is not Disabled loads at login (RunAtLoad), so it counts as a gateway consumer
    whether or not launchd has it loaded right now, the way an enabled-but-stopped systemd unit does.
    """
    agent_dir = home / "Library/LaunchAgents"
    found: list[tuple[str, str]] = []
    if not agent_dir.is_dir():
        return found
    for plist in sorted(agent_dir.glob("ai.hermes.gateway*.plist")):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if not isinstance(data, dict) or data.get("Disabled") is True:
            continue
        arguments = [str(a) for a in data.get("ProgramArguments") or []]
        if not arguments and data.get("Program"):
            arguments = [str(data["Program"])]
        binary = launchd_program_binary(arguments)
        if binary:
            found.append((f"launchd:{plist.stem}", binary))
    return found


def registry_rows(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    if yaml is not None:
        try:
            agents = (yaml.safe_load(text) or {}).get("agents") or {}
            return {k: (v.get("hermes") or {}) for k, v in agents.items() if isinstance(v, dict)}
        except Exception:
            pass
    rows: dict[str, dict] = {}
    agent = None
    in_hermes = False
    for line in text.splitlines():
        if re.match(r"^  [A-Za-z0-9_.-]+:\s*$", line):
            agent, in_hermes = line.strip().rstrip(":"), False
            rows[agent] = {}
        elif agent and re.match(r"^    hermes:\s*$", line):
            in_hermes = True
        elif agent and in_hermes:
            m = re.match(r"^      ([a-z_]+):\s*(.*)$", line)
            if m:
                rows[agent][m.group(1)] = m.group(2).strip().strip("'\"")
            elif re.match(r"^    \S", line):
                in_hermes = False
    return rows


def wrapper_defaults(home: Path, registry: Path) -> list[tuple[str, str]]:
    """(label, embedded-default binary) of every deployed ``agents/hermes/*/hermes`` wrapper."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    paths: list[Path] = []
    code = Path(os.environ["HERMES_CODE_ROOT"]).expanduser() if os.environ.get("HERMES_CODE_ROOT") else home / "code"
    if code.is_dir():
        for pattern in ("*/agents/hermes/*/hermes", "*/*/agents/hermes/*/hermes"):
            paths.extend(code.glob(pattern))
    for p in sorted(set(paths)):
        if not p.is_file():
            continue
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'HERMES_BIN="\$\{HERMES_BIN:-([^}\n]+)\}"', text)
        if m and "HERMES_FLEET_BIN" not in m.group(1):
            out.append((f"wrapper:{p.relative_to(code)}", expand(m.group(1), home)))
    return out


def running_gateway_sha(home: Path) -> tuple[str | None, str | None]:
    state = home / ".hermes/gateway_state.json"
    try:
        data = json.loads(state.read_text())
    except (OSError, ValueError):
        return None, None
    return data.get("code_sha"), data.get("gateway_state")


def check(home: Path, registry: Path, fleet_env: Path, config: Path, quiet: bool = False) -> Report:
    rep = Report()
    declared: list[tuple[str, str, str]] = []  # (kind, label, path)

    for label, binary in systemd_execs(home):
        declared.append(("gateway", label, binary))
    for label, binary in launchd_execs(home):
        declared.append(("gateway", label, binary))
    for name in BIN_NAMES:
        p = home / ".local/bin" / name
        if p.exists() or p.is_symlink():
            target = exec_target_of_script(p) if p.is_file() and not p.is_symlink() else str(p)
            declared.append(("PATH", f"~/.local/bin/{name}", target or str(p)))
    env = read_env_file(fleet_env, ("HERMES_FLEET_BIN", "HERMES_FLEET_REPO"))
    for key, value in env.items():
        declared.append(("fleet.env", key, expand(value, home)))
    cfg: dict = {}
    if config.is_file() and tomllib is not None:
        try:
            cfg = tomllib.loads(config.read_text(encoding="utf-8")).get("fleet", {})
        except Exception:
            cfg = {}
    for key in ("hermes_bin", "hermes_repo"):
        if cfg.get(key):
            declared.append(("config", f"fleet.{key}", expand(str(cfg[key]), home)))
    rows = registry_rows(registry)
    for agent, h in sorted(rows.items()):
        for key in ("bin", "repo"):
            if h.get(key):
                declared.append(("registry", f"{agent}.hermes.{key}", expand(str(h[key]), home)))

    roots: dict[str, set[str]] = {}
    for kind, label, path in declared:
        root = install_root(path)
        sha = git(root, "rev-parse", "HEAD") if root else None
        rep.consumer(kind, label, path, root, sha)
        if root is None:
            rep.fails.append(f"{kind} {label}: {path} does not resolve to an install (missing or not in a git checkout)")
            continue
        roots.setdefault(root, set()).add(f"{kind}:{label}")
        if kind in ("gateway", "PATH", "fleet.env", "config") and path.endswith(tuple(BIN_NAMES)) and not Path(path).exists():
            rep.fails.append(f"{kind} {label}: binary {path} does not exist")

    # Embedded wrapper defaults are shadowed by fleet.env/config at run time: drift here is a WARN.
    for label, path in wrapper_defaults(home, registry):
        root = install_root(path)
        sha = git(root, "rev-parse", "HEAD") if root else None
        rep.consumer("wrapper-default", label, path, root, sha, note="shadowed by fleet.env/config")
        if root is None or (roots and root not in roots):
            rep.warns.append(f"{label}: embedded fallback {path} is not the shared install (shadowed today; re-run fleet-sync)")

    if len(roots) > 1:
        detail = "; ".join(f"{r} <- {', '.join(sorted(c)[:4])}{'…' if len(c) > 4 else ''}" for r, c in sorted(roots.items()))
        rep.fails.append(f"{len(roots)} different Hermes installs are in use: {detail}")
    canonical = next(iter(roots)) if len(roots) == 1 else None
    if canonical is None and roots:
        # Name the install the PATH launcher and the gateway agree on, for the tracking checks below.
        canonical = next(iter(sorted(roots)))
    head = git(canonical, "rev-parse", "HEAD") if canonical else None

    if canonical:
        upstream = git(canonical, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if not upstream or not upstream.startswith("delorenj/"):
            rep.fails.append(f"install {canonical} tracks {upstream or 'nothing'}, not a delorenj/* branch (deploys by git pull --ff-only need it)")
        filt = git(canonical, "config", "--get", "remote.delorenj.partialclonefilter") or git(canonical, "config", "--get", "remote.origin.partialclonefilter")
        if filt:
            rep.fails.append(f"install {canonical} is a partial clone ({filt}); it cannot push the work done in it")
        for agent, h in sorted(rows.items()):
            sha = (h.get("git_sha") or "").strip()
            if not sha:
                rep.warns.append(f"registry {agent}: records no hermes.git_sha")
                continue
            if sha == head:
                continue
            if git(canonical, "cat-file", "-e", f"{sha}^{{commit}}") is None:
                rep.fails.append(f"registry {agent}: git_sha {sha[:10]} is not in the install's history (a different line)")
            elif subprocess.run(["git", "-C", canonical, "merge-base", "--is-ancestor", sha, head or "HEAD"], check=False).returncode == 0:
                rep.warns.append(f"registry {agent}: git_sha {sha[:10]} is behind install HEAD {str(head)[:10]} (fleet-sync refreshes it)")
            else:
                rep.fails.append(f"registry {agent}: git_sha {sha[:10]} is not an ancestor of install HEAD {str(head)[:10]}")

    code_sha, gw_state = running_gateway_sha(home)
    if code_sha and head and code_sha != head and gw_state == "running":
        rep.warns.append(f"the running gateway booted from {code_sha[:10]} but the install is at {head[:10]} (restart hermes-gateway.service when idle)")
    return rep


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-stamp", action="store_true", help="do not touch the ok stamp / write status.json")
    args = p.parse_args(argv)

    home = Path(os.environ.get("HOME", str(Path.home())))
    registry = Path(os.path.expanduser(os.environ.get("HERMES_FLEET_REGISTRY_FILE", ""))) if os.environ.get("HERMES_FLEET_REGISTRY_FILE", "").startswith(("/", "~")) else home / ".hermes/agents-registry.yaml"
    fleet_env = Path(os.environ.get("HERMES_FLEET_ENV", home / ".hermes/fleet.env"))
    config = Path(os.environ.get("HERMES_TEMPLATE_CONFIG", Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "hermes-agent-template/config.toml"))

    rep = check(home, registry, fleet_env, config, quiet=args.quiet)
    status = {
        "ts": int(time.time()), "ok": not rep.fails, "fails": rep.fails, "warns": rep.warns,
        "installs": sorted({c["root"] for c in rep.consumers if c["root"]}),
        "sha": sorted({c["sha"] for c in rep.consumers if c["sha"]}),
        "consumers": len(rep.consumers),
    }
    if args.json:
        print(json.dumps({**status, "detail": rep.consumers}, indent=2))
    else:
        if not args.quiet:
            by_root: dict[str, list[dict]] = {}
            for c in rep.consumers:
                by_root.setdefault(c["root"] or "<unresolved>", []).append(c)
            for root, cs in sorted(by_root.items()):
                sha = next((c["sha"] for c in cs if c["sha"]), None)
                print(f"install {root}  sha {str(sha)[:10]}  consumers {len(cs)}")
                kinds: dict[str, int] = {}
                for c in cs:
                    kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
                print("   " + ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())))
        for w in rep.warns:
            print(f"WARN  {w}")
        for f in rep.fails:
            print(f"FAIL  {f}")
        print("hermes-single-install: " + ("OK" if not rep.fails else f"DRIFT ({len(rep.fails)} failure(s))"))
    if not args.no_stamp:
        cache = home / ".cache/hermes-single-install"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "status.json").write_text(json.dumps(status, indent=2))
            stamp = cache / "ok"
            if status["ok"]:
                stamp.touch()
        except OSError:
            pass
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
