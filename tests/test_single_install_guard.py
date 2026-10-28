"""The Hermes drift guard (scripts/hermes-single-install-check.py).

Hermes ran from two code lines for two months because nothing compared the consumers. These tests pin the
contract: every consumer resolves to ONE install root and ONE history, or the guard fails.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "hermes-single-install-check.py"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _install(path: Path, *, fork_tracking: bool = True) -> str:
    """A fake Hermes install: git checkout with the shared launcher; optionally tracking delorenj/main."""
    (path / ".hermes/bin").mkdir(parents=True)
    launcher = path / ".hermes/bin/hermes"
    launcher.write_text(f"#!/bin/sh\n# {path}\nexit 0\n")  # distinct content: identical trees would share a sha
    launcher.chmod(0o755)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    if fork_tracking:
        bare = path.parent / (path.name + ".fork.git")
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        _git(path, "remote", "add", "delorenj", str(bare))
        _git(path, "push", "-q", "-u", "delorenj", "main")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def host(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".config/hermes-agent-template").mkdir(parents=True)
    install = home / ".hermes/hermes-agent"
    install.mkdir()
    sha = _install(install)
    launcher = install / ".hermes/bin/hermes"
    path_hermes = home / ".local/bin/hermes"
    path_hermes.write_text(f'#!/bin/sh\nexec {launcher} "$@"\n')
    path_hermes.chmod(0o755)
    (home / ".hermes/fleet.env").write_text(f"HERMES_FLEET_BIN={launcher}\nHERMES_FLEET_REPO={install}\n")
    (home / ".config/hermes-agent-template/config.toml").write_text(
        f'[fleet]\nhermes_bin = "{launcher}"\nhermes_repo = "{install}"\n')
    (home / ".hermes/agents-registry.yaml").write_text(
        "agents:\n  demo-pm:\n    hermes:\n"
        f"      bin: {launcher}\n      repo: {install}\n      git_sha: {sha}\n")
    return {"home": home, "install": install, "launcher": launcher, "sha": sha}


def _run(home: Path, *extra: str):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "HERMES_GUARD_NO_SYSTEMCTL": "1"}
    return subprocess.run(["python3", str(GUARD), "--no-stamp", *extra], env=env, capture_output=True, text=True)


def test_one_install_passes(host):
    result = _run(host["home"])
    assert result.returncode == 0, result.stdout
    assert "hermes-single-install: OK" in result.stdout


def test_a_second_release_checkout_is_drift(host):
    release = host["home"] / ".local/share/hermes-agent/releases/abc"
    release.mkdir(parents=True)
    _install(release, fork_tracking=False)
    (host["home"] / ".hermes/fleet.env").write_text(
        f"HERMES_FLEET_BIN={release}/.hermes/bin/hermes\nHERMES_FLEET_REPO={release}\n")
    result = _run(host["home"])
    assert result.returncode == 1
    assert "2 different Hermes installs are in use" in result.stdout


def test_registry_sha_from_another_line_is_drift(host):
    other = host["home"] / "other"
    other.mkdir()
    foreign = _install(other, fork_tracking=False)
    registry = host["home"] / ".hermes/agents-registry.yaml"
    registry.write_text(registry.read_text().replace(host["sha"], foreign))
    result = _run(host["home"])
    assert result.returncode == 1
    assert "not in the install's history" in result.stdout


def test_registry_sha_behind_head_is_only_a_warning(host):
    (host["install"] / "x").write_text("x")
    _git(host["install"], "add", "-A")
    _git(host["install"], "commit", "-q", "-m", "next")
    _git(host["install"], "push", "-q")
    result = _run(host["home"])
    assert result.returncode == 0, result.stdout
    assert "is behind install HEAD" in result.stdout


def test_install_must_track_the_fork(host):
    _git(host["install"], "branch", "--unset-upstream")
    result = _run(host["home"])
    assert result.returncode == 1
    assert "not a delorenj/* branch" in result.stdout


def test_missing_binary_is_drift(host):
    host["launcher"].unlink()
    result = _run(host["home"])
    assert result.returncode == 1


def test_wrapper_fallback_to_another_path_is_a_shadowed_warning(host):
    wrapper = host["home"] / "code/proj/agents/hermes/pm/hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('HERMES_BIN="${HERMES_BIN:-$HOME/.local/share/hermes-agent/releases/old/.venv/bin/hermes}"\n')
    result = _run(host["home"])
    assert result.returncode == 0, result.stdout
    assert "embedded fallback" in result.stdout


# --- macOS: LaunchAgent gateway consumers -------------------------------------------------------
import json  # noqa: E402
import plistlib  # noqa: E402
import shlex  # noqa: E402


def _plist(home: Path, name: str, arguments: list[str], **extra) -> Path:
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / f"{name}.plist"
    path.write_bytes(plistlib.dumps({"Label": name, "ProgramArguments": arguments, **extra}))
    return path


def _osascript_arguments(command: list[str]) -> list[str]:
    """The shape hermes_cli/gateway_launchd.py:launchd_program_arguments writes."""
    shell = f"exec {shlex.join(command)} >> /tmp/gateway.log 2>> /tmp/gateway.error.log"
    js = f'ObjC.import("stdlib"); const status=$.system({json.dumps(shell)});'
    return ["/usr/bin/osascript", "-l", "JavaScript", "-e", js]


def test_launchd_gateway_on_the_shared_install_passes_and_is_counted(host):
    _plist(host["home"], "ai.hermes.gateway", _osascript_arguments([str(host["launcher"]), "--error-log", "/x", "--", "gateway", "run"]))
    result = _run(host["home"], "--json")
    assert result.returncode == 0, result.stdout
    detail = json.loads(result.stdout)["detail"]
    assert any(c["kind"] == "gateway" and c["label"] == "launchd:ai.hermes.gateway" and c["root"] == str(host["install"]) for c in detail)


def test_launchd_gateway_on_a_second_install_is_drift(host):
    other = host["home"] / ".local/share/hermes-agent/releases/abc"
    other.mkdir(parents=True)
    _install(other, fork_tracking=False)
    _plist(host["home"], "ai.hermes.gateway", _osascript_arguments([str(other / ".hermes/bin/hermes"), "gateway", "run"]))
    result = _run(host["home"])
    assert result.returncode == 1
    assert "2 different Hermes installs" in result.stdout and "launchd:ai.hermes.gateway" in result.stdout


def test_launchd_direct_launcher_and_python_bootstrap_shapes(host):
    _plist(host["home"], "ai.hermes.gateway", [str(host["launcher"]), "gateway", "run"])
    bootstrap = f"import os, sys, runpy; sys.path.insert(0, {str(host['install'])!r}); import hermes_bootstrap"
    _plist(host["home"], "ai.hermes.gateway-demo", ["/usr/bin/python3", "-c", bootstrap])
    result = _run(host["home"], "--json")
    assert result.returncode == 0, result.stdout
    labels = {c["label"] for c in json.loads(result.stdout)["detail"] if c["kind"] == "gateway"}
    assert labels == {"launchd:ai.hermes.gateway", "launchd:ai.hermes.gateway-demo"}


def test_disabled_and_unrelated_launchd_plists_are_ignored(host):
    other = host["home"] / "elsewhere"
    other.mkdir()
    _install(other, fork_tracking=False)
    _plist(host["home"], "ai.hermes.gateway", [str(other / ".hermes/bin/hermes"), "gateway", "run"], Disabled=True)
    _plist(host["home"], "io.example.other", [str(other / ".hermes/bin/hermes")])
    (host["home"] / "Library/LaunchAgents/ai.hermes.gateway-broken.plist").write_text("not a plist")
    result = _run(host["home"])
    assert result.returncode == 0, result.stdout
    assert "launchd:" not in result.stdout


def test_code_root_override_finds_wrappers_outside_home_code(host):
    root = host["home"] / "Code"
    wrapper = root / "proj/agents/hermes/pm/hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('HERMES_BIN="${HERMES_BIN:-$HOME/.local/share/hermes-agent/releases/old/.venv/bin/hermes}"\n')
    env = {"PATH": os.environ["PATH"], "HOME": str(host["home"]), "HERMES_GUARD_NO_SYSTEMCTL": "1", "HERMES_CODE_ROOT": str(root)}
    result = subprocess.run(["python3", str(GUARD), "--no-stamp"], env=env, capture_output=True, text=True)
    assert "embedded fallback" in result.stdout
