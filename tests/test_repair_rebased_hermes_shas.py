"""The one-time registry repair retains the guard's strict ancestry check."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("repair_rebased_hermes_shas", ROOT / "scripts/repair-rebased-hermes-shas.py")
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": "2026-10-17T12:00:00+00:00"}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "install"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "base").write_text("base")
    git(repo, "add", "base")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "feature").write_text("original")
    git(repo, "add", "feature")
    git(repo, "commit", "-q", "-m", "feature")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "reset", "--hard", base)
    (repo / "feature").write_text("rebased")
    git(repo, "add", "feature")
    git(repo, "commit", "-q", "-m", "feature")
    new = git(repo, "rev-parse", "HEAD")
    assert old != new
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text(f"agents:\n  demo-pm:\n    hermes:\n      git_sha: {old}\n"
                        f"  second-pm:\n    hermes:\n      git_sha: {old}\n")
    return repo, registry, old, new


def test_dry_run_and_apply_only_replace_rebased_shas(history):
    repo, registry, old, new = history
    original = registry.read_text()
    assert repair.repair(registry, repo, False) == 1
    assert registry.read_text() == original
    assert repair.repair(registry, repo, True) == 1
    rows = yaml.safe_load(registry.read_text())["agents"]
    assert {r["hermes"]["git_sha"] for r in rows.values()} == {new}
    assert repair.repair(registry, repo, True) == 0
    assert old not in registry.read_text()


def test_missing_replacement_refuses_to_change_registry(history):
    repo, registry, old, _ = history
    git(repo, "reset", "--hard", "HEAD~1")
    original = registry.read_text()
    with pytest.raises(ValueError, match="expected one reachable commit"):
        repair.repair(registry, repo, True)
    assert registry.read_text() == original


def test_ambiguous_replacement_refuses_to_change_registry(history):
    repo, registry, old, _ = history
    (repo / "feature").write_text("another")
    git(repo, "add", "feature")
    git(repo, "commit", "-q", "-m", "feature")
    original = registry.read_text()
    with pytest.raises(ValueError, match="found 2"):
        repair.repair(registry, repo, True)
    assert registry.read_text() == original
