from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_host_local_runtime_payloads_are_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".codegraph/daemon.pid" in gitignore
    assert ".omo/run-continuation/" in gitignore


def test_host_local_runtime_payloads_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    forbidden = re.compile(
        r"(?:^|/)(?:\.omo/run-continuation/|\.codegraph/[^/]+\.(?:pid|sock|socket)$)"
        r"|(?:^|/)(?:sessions?|runtime-state)/"
        r"|(?:^|/)__pycache__/|\.py[co]$"
        r"|(?:^|/)[^/]+\.(?:pid|sock|socket)$"
    )

    offenders = sorted(path for path in tracked if path and forbidden.search(path))
    assert offenders == [], f"tracked host-local runtime payloads: {offenders}"


def test_runtime_is_never_a_git_repository() -> None:
    """An agent runtime is host-local and may hold secrets and mutable state.

    It is never a Git repository, never a submodule, and never pushed anywhere
    (PJAN-41). Three runtimes were found on 2026-09-17 still carrying a `.git`
    file left over from the retired submodule model, each still pushing to a
    private `agent-hm-*` repo on GitHub — one of them with a commit titled
    "stop tracking secret". Durability belongs to Hindsight, not to a remote.
    """
    offenders = [
        str(path)
        for path in ROOT.glob("**/agents/hermes/*/runtime/.git")
    ]
    assert offenders == [], f"agent runtime is a Git repository: {offenders}"


def test_provisioning_never_records_a_runtime_repo() -> None:
    """Nothing may write a `runtime_repo` field back into the agents registry.

    The field asserted a Git-tracked runtime, so leaving the writer in place
    would re-create the model every time an agent was re-provisioned.
    """
    registry_writer = ROOT / "template/.scripts/80-registry.sh"
    if not registry_writer.exists():          # vendored copies may omit it
        return
    body = registry_writer.read_text(encoding="utf-8")
    emitting = [
        line.strip()
        for line in body.splitlines()
        if '"runtime_repo"' in line and not line.strip().startswith("#")
    ]
    assert emitting == [], f"80-registry.sh still emits runtime_repo: {emitting}"


def test_checkpoint_helper_is_gone() -> None:
    """checkpoint.sh existed only to commit and push a Git-tracked runtime."""
    assert not (ROOT / "template/.scripts/checkpoint.sh").exists()
