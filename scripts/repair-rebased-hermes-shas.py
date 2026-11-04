#!/usr/bin/env python3
"""Refresh registry Hermes SHAs after a rebase, without relaxing the drift guard.

Dry run by default. A replacement must be the unique reachable commit with the
same author, author date, and subject; otherwise leave the registry untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def identity(repo: Path, sha: str) -> str:
    return git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%s", sha)


def replacements(repo: Path, rows: dict) -> dict[str, str]:
    head = git(repo, "rev-parse", "HEAD")
    shas = {str((row.get("hermes") or {}).get("git_sha") or "") for row in rows.values()}
    shas.discard("")
    reachable = git(repo, "log", "--format=%H%x00%an%x00%ae%x00%aI%x00%s", "HEAD").splitlines()
    by_identity: dict[str, list[str]] = {}
    for line in reachable:
        sha, metadata = line.split("\0", 1)
        by_identity.setdefault(metadata, []).append(sha)
    changes: dict[str, str] = {}
    for sha in sorted(shas):
        git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, head], check=False).returncode == 0:
            continue
        matches = by_identity.get(identity(repo, sha), [])
        if len(matches) != 1:
            raise ValueError(f"{sha[:10]}: expected one reachable commit with matching author/date/subject, found {len(matches)}")
        changes[sha] = matches[0]
    return changes


def update_lines(text: str, rows: dict, changes: dict[str, str]) -> str:
    agents = rows
    agent = None
    in_hermes = False
    updated: set[str] = set()
    out = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^  ([\w.-]+):\s*$", line)
        if match:
            agent, in_hermes = match.group(1), False
        elif re.match(r"^    hermes:\s*$", line):
            in_hermes = True
        elif re.match(r"^    \S", line) or re.match(r"^\S", line):
            in_hermes = False
        if in_hermes and agent in agents:
            match = re.match(r"^(\s{6}git_sha:\s*)([a-f0-9]{40})(\s*(?:#.*)?(?:\r?\n)?)$", line)
            if match and match.group(2) in changes:
                if match.group(2) != agents[agent].get("hermes", {}).get("git_sha"):
                    raise ValueError(f"{agent}: registry text does not match parsed SHA")
                line = match.group(1) + changes[match.group(2)] + match.group(3)
                updated.add(agent)
        out.append(line)
    expected = {agent for agent, row in agents.items() if (row.get("hermes") or {}).get("git_sha") in changes}
    if updated != expected:
        raise ValueError(f"unmatched registry rows: {sorted(expected - updated)}")
    return "".join(out)


def repair(registry: Path, repo: Path, apply: bool) -> int:
    if registry.is_symlink() or not registry.is_file():
        raise ValueError("registry must be a regular file, not a symlink")
    lock = registry.with_name(registry.name + ".lock")
    if lock.is_symlink():
        raise ValueError("registry lock must not be a symlink")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        original = registry.read_text(encoding="utf-8")
        rows = (yaml.safe_load(original) or {}).get("agents") or {}
        if not isinstance(rows, dict):
            raise ValueError("registry agents must be a mapping")
        changes = replacements(repo, rows)
        updated = update_lines(original, rows, changes)
        for before, after in changes.items():
            print(f"{'APPLY' if apply else 'DRY RUN'} {before[:10]} -> {after[:10]}")
        if apply and updated != original:
            fd, temp = tempfile.mkstemp(prefix=f".{registry.name}.", dir=registry.parent)
            try:
                os.fchmod(fd, registry.stat().st_mode & 0o777)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    output.write(updated)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, registry)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        print(f"{len(changes)} rebased SHAs {'repaired' if apply else 'would be repaired'}")
        return len(changes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.home() / ".hermes/hermes-agent")
    parser.add_argument("--registry", type=Path, default=Path.home() / ".hermes/agents-registry.yaml")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    repair(args.registry, args.repo, args.apply)


if __name__ == "__main__":
    main()
