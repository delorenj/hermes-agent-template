"""bin/hermes-pm finds the nearest PM launcher; bare `hermes` is never shadowed.

Story 7.1 AC4 (2026-11-19): the PM launcher left PATH. `hermes-pm` is the
explicit name for "this repo's PM", resolved by walking up from the cwd.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "bin" / "hermes-pm"


def _launcher(repo: Path, tag: str) -> Path:
    role = repo / "agents" / "hermes" / "pm"
    role.mkdir(parents=True)
    path = role / "hermes"
    path.write_text(f'#!/bin/sh\nprintf "{tag} %s\\n" "$*"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(cwd)}
    return subprocess.run([str(RESOLVER), *args], cwd=cwd, env={**base, **(env or {})},
                          capture_output=True, text=True, check=False)


def test_nearest_launcher_wins_and_receives_arguments(tmp_path: Path) -> None:
    parent = tmp_path / "platform"
    child = parent / "component"
    _launcher(parent, "parent")
    nested = child / "src" / "deep"
    nested.mkdir(parents=True)
    # A child repo without a desk talks to the parent's PM (what mise's merged PATH did).
    assert _run(nested, "chat", "status").stdout == "parent chat status\n"
    _launcher(child, "child")
    assert _run(nested, "chat", "status").stdout == "child chat status\n"


def test_pm_launcher_flag_prints_without_running(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path / "repo", "ran")
    result = _run(tmp_path / "repo", "--pm-launcher")
    assert result.returncode == 0
    assert result.stdout == f"{launcher}\n"


def test_hermes_pm_from_overrides_cwd(tmp_path: Path) -> None:
    _launcher(tmp_path / "repo", "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run(elsewhere, "status", env={"HERMES_PM_FROM": str(tmp_path / "repo")})
    assert result.stdout == "repo status\n"


def test_no_launcher_is_a_usage_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "chat")
    assert result.returncode == 2
    assert "no PM launcher" in result.stderr


def test_resolver_is_executable_posix_sh() -> None:
    assert os.access(RESOLVER, os.X_OK)
    assert RESOLVER.read_text().startswith("#!/bin/sh\n")
