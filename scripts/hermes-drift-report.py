#!/usr/bin/env python3
"""Weekly, check-only Hermes drift report. Two modes, one Candybar each; nothing here repairs anything.

  fork   Is the live install still the fork, and how far does the fork trail upstream?
         On 2026-11-24 a Desktop-app "update" detached the install onto the upstream release and the
         fleet ran without the fork's commits for five hours; update custody (fork tweak 25) now
         prevents that, and this report proves it stays prevented. It also says when a /hermes-sync is due.
  fleet  Does every consumer still agree with the install? Generated profile configs vs base+delta
         (hermes-profile-config.py check), the single-install guard (its WARN lines too), and registry
         hermes.git_sha rows a rebase left behind (repair-rebased-hermes-shas.py, dry run).

Exit 0 clean, 1 drift (an ntfy alert went out), 2 the check itself could not run (also alerted).
--no-alert prints only. Every finding names the command that fixes it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
INSTALL = Path(os.environ.get("HERMES_FLEET_REPO", HOME / ".hermes/hermes-agent"))
SCRIPTS = Path(__file__).resolve().parent
FORK_REMOTE = "delorenj"
UPSTREAM_REMOTE = "origin"


def sh(args: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def git(*args: str, timeout: int = 300) -> str:
    r = sh(["git", "-C", str(INSTALL), *args], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def fork_report(max_days: int, max_commits: int) -> tuple[list[str], list[str]]:
    """(drift, info)"""
    drift: list[str] = []
    info: list[str] = []
    git("fetch", "-q", FORK_REMOTE, timeout=600)
    git("fetch", "-q", UPSTREAM_REMOTE, timeout=600)
    branch = sh(["git", "-C", str(INSTALL), "symbolic-ref", "-q", "--short", "HEAD"]).stdout.strip()
    head = git("rev-parse", "HEAD")
    fork = git("rev-parse", f"{FORK_REMOTE}/main")
    if not branch:
        drift.append(f"install is DETACHED at {head[:10]} (the 2026-11-24 failure mode): git -C {INSTALL} checkout main && git -C {INSTALL} reset --hard {FORK_REMOTE}/main, then hermes-gateway-restart --reason 'reattach fork'")
    elif branch != "main":
        drift.append(f"install is on branch {branch}, not main: switch it back to main tracking {FORK_REMOTE}/main")
    else:
        upstream = sh(["git", "-C", str(INSTALL), "rev-parse", "--abbrev-ref", "@{u}"]).stdout.strip()
        if upstream != f"{FORK_REMOTE}/main":
            drift.append(f"install main tracks {upstream or 'nothing'}, not {FORK_REMOTE}/main: git -C {INSTALL} branch -u {FORK_REMOTE}/main")
    if head != fork:
        ahead = git("rev-list", "--count", f"{fork}..{head}")
        behind = git("rev-list", "--count", f"{head}..{fork}")
        drift.append(f"install HEAD {head[:10]} != {FORK_REMOTE}/main {fork[:10]} ({ahead} ahead, {behind} behind): deploy per /hermes-sync step 7")
    fork_only = int(git("rev-list", "--count", f"{UPSTREAM_REMOTE}/main..{fork}"))
    if fork_only == 0:
        drift.append(f"{FORK_REMOTE}/main has no commits above {UPSTREAM_REMOTE}/main: the fork's changes are gone")
    custody = (INSTALL / "hermes_cli" / "update_fork_custody.py").exists()
    if not custody:
        drift.append("update custody (fork tweak 25, hermes_cli/update_fork_custody.py) is missing: a Desktop/CLI update can detach the install onto upstream again")
    base = git("merge-base", fork, f"{UPSTREAM_REMOTE}/main")
    trailing = int(git("rev-list", "--count", f"{base}..{UPSTREAM_REMOTE}/main"))
    base_date = dt.datetime.fromtimestamp(int(git("show", "-s", "--format=%ct", base)), dt.timezone.utc)
    up_date = dt.datetime.fromtimestamp(int(git("show", "-s", "--format=%ct", f"{UPSTREAM_REMOTE}/main")), dt.timezone.utc)
    lag_days = (up_date - base_date).days
    info.append(f"fork {fork[:10]}: {fork_only} fork-only commits on upstream base {base[:10]} ({base_date:%Y-%m-%d}); upstream is {trailing} commits / {lag_days} days ahead")
    if trailing > max_commits or lag_days > max_days:
        drift.append(f"fork trails upstream by {trailing} commits / {lag_days} days (limits {max_commits} / {max_days}): run /hermes-sync in an agent session")
    return drift, info


def fleet_report() -> tuple[list[str], list[str]]:
    drift: list[str] = []
    info: list[str] = []
    r = sh([sys.executable, "-B", str(SCRIPTS / "hermes-profile-config.py"), "check"], timeout=600)
    if r.returncode != 0:
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        drift.append(f"profile configs drifted from base+delta ({len(lines)} lines; first: {lines[0] if lines else '?'}): python3 {SCRIPTS}/hermes-profile-config.py render --all (then check)")
    else:
        info.append("profile configs: every config.yaml == base+delta")
    r = sh([sys.executable, "-B", str(SCRIPTS / "hermes-single-install-check.py")], timeout=600)
    out = r.stdout + r.stderr
    warns = [l for l in out.splitlines() if l.startswith("WARN")]
    if r.returncode != 0:
        fails = [l for l in out.splitlines() if l.startswith(("FAIL", "ERROR"))]
        drift.append(f"single-install guard FAILED: {'; '.join(fails[:3]) or out.strip().splitlines()[-1:]}")
    booted = [w for w in warns if "running gateway booted from" in w]
    if booted:
        drift.append(f"{booted[0][6:]} (hermes-gateway-restart --reason 'pick up install HEAD')")
    sha = [w for w in warns if re.search(r"registry \S+: git_sha", w)]
    if sha:
        r2 = sh([sys.executable, "-B", str(SCRIPTS / "repair-rebased-hermes-shas.py")], timeout=300)
        info.append(f"{len(sha)} registry rows record an older hermes.git_sha (fleet-sync refreshes them; rebase repair dry run: {(r2.stdout.strip().splitlines() or ['?'])[-1]})")
    other = [w for w in warns if w not in booted and w not in sha]
    if other:
        drift.append(f"{len(other)} other single-install warnings, first: {other[0][6:]}")
    # fleet-sync's own dry run: every deployed desk vs the template and fleet contract (never --apply here).
    r = sh(["bash", str(SCRIPTS / "fleet-sync.sh")], timeout=900)
    rows = [l for l in r.stdout.splitlines() if re.match(r"^\S+\s+DRIFT\s", l)]
    if rows:
        agents = sorted({l.split()[0] for l in rows})
        manual = sum(1 for l in rows if "MANUAL" in l)
        kinds: dict[str, int] = {}
        for l in rows:
            kind = re.sub(r"\s*\(.*$|:.*$|-> .*$", "", l.split("DRIFT", 1)[1].strip())
            kinds[kind] = kinds.get(kind, 0) + 1
        top = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
        drift.append(f"fleet-sync: {len(rows)} drift items on {len(agents)} desks ({manual} MANUAL); top: {top}. Review: bash {SCRIPTS}/fleet-sync.sh; fix the fixable ones with --apply, then one hermes-gateway-restart")
    elif r.returncode == 0:
        info.append("fleet-sync: every desk matches the template and fleet contract")
    return drift, info


def alert(title: str, body: str, priority: str) -> None:
    sh([str(HOME / ".local/bin/ntfy-alert"), "--title", title, "--priority", priority, "--tags", "hermes,drift", body], timeout=60)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["fork", "fleet"])
    ap.add_argument("--max-days", type=int, default=21, help="fork mode: upstream lead in days before a sync is due")
    ap.add_argument("--max-commits", type=int, default=400, help="fork mode: upstream lead in commits before a sync is due")
    ap.add_argument("--no-alert", action="store_true")
    a = ap.parse_args()
    title = f"Hermes {a.mode} drift"
    try:
        drift, info = fork_report(a.max_days, a.max_commits) if a.mode == "fork" else fleet_report()
    except Exception as exc:  # the check could not run: say so, never report clean
        print(f"UNABLE: {exc}")
        if not a.no_alert:
            alert(f"{title}: check could not run", str(exc)[:900], "high")
        return 2
    for line in info:
        print(f"INFO  {line}")
    for line in drift:
        print(f"DRIFT {line}")
    if not drift:
        print(f"hermes-drift-report {a.mode}: clean")
        return 0
    if not a.no_alert:
        alert(title, "\n".join(f"- {d}" for d in drift)[:3500], "high" if a.mode == "fork" else "default")
    return 1


if __name__ == "__main__":
    sys.exit(main())
