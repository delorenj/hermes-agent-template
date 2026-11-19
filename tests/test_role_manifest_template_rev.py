"""A rendered role manifest records the template commit it was rendered from.

The pjangler shell prompt shows it as the PM's manifest version, and compares it
with Flume's pinned template to say when a desk is behind.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, text=True, capture_output=True,
    ).stdout.strip()


def _render(source: Path, role: Path, *extra: str) -> str:
    rendered = subprocess.run(
        ["copier", "copy", "--skip-tasks", "--defaults", "--trust", *extra,
         "--data", "target_repo=disposable", str(source), str(role)],
        text=True, capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    return (role / "role.yaml").read_text()


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "template-source"
    source.mkdir()
    shutil.copy2(ROOT / "copier.yml", source / "copier.yml")
    shutil.copytree(ROOT / "template", source / "template", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return source


def test_role_manifest_records_the_rendering_commit(tmp_path: Path) -> None:
    source = _source(tmp_path)
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "fixture")
    head = _git(source, "rev-parse", "HEAD")
    manifest = _render(source, tmp_path / "project/agents/hermes/pm", "--vcs-ref=HEAD")
    assert f'template_rev: "{head}"' in manifest


def test_a_non_git_source_records_no_commit_rather_than_a_guess(tmp_path: Path) -> None:
    manifest = _render(_source(tmp_path), tmp_path / "project/agents/hermes/pm")
    assert 'template_rev: ""' in manifest
