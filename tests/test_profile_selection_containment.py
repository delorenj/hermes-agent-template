"""PJAN-73: selection is an all-or-nothing, non-provisioning preflight."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/hermes-profile-config.py"
COMMANDS = (
    "init", "render", "check", "repin", "expected", "absorb", "status",
    "skills-policy", "memory-pin", "memory-template", "memory-endpoint", "project-skills",
)
WRITERS = (
    "init", "render", "repin", "absorb", "skills-policy", "memory-pin",
    "memory-template", "memory-endpoint", "project-skills",
)
BAD_NAMES = (
    "", " ", " desk", "desk ", ".", "..", "../outside", "desk/../outside",
    "/outside", "desk/other", r"desk\other", r"C:\outside",
    "C:", r"\\server\share", "//server/share", "a.b", "-desk",
)


@pytest.fixture
def fleet(tmp_path):
    home = tmp_path / "fleet"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal:\n  cwd: .\n")
    for name in ("alpha", "Bravo_2"):
        desk = profiles / name
        desk.mkdir()
        (profiles / f".{name}.config.lock").touch()
        (desk / "config.delta.yaml").write_text("{}\n")
        (desk / "config.yaml").write_text("terminal:\n  cwd: .\n")
    return home


def run(fleet, command, *options):
    env = dict(os.environ, HERMES_FLEET_HOME=str(fleet),
               HERMES_AGENTS_REGISTRY=str(fleet / "agents-registry.yaml"),
               PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-B", str(SCRIPT), command, *options],
                          capture_output=True, text=True, env=env, cwd=fleet)


def contents(root):
    """Include metadata paths and file bytes to catch backups, locks and repairs."""
    return {str(p.relative_to(root)): ("link", os.readlink(p)) if p.is_symlink()
            else ("file", p.read_bytes()) if p.is_file() else ("dir", None)
            for p in root.rglob("*")}


@pytest.mark.parametrize("name", BAD_NAMES)
@pytest.mark.parametrize("command", COMMANDS)
def test_invalid_spelling_has_no_effect(fleet, name, command):
    before = contents(fleet)
    for dry in (False, True):
        options = [f"--profile={name}"]
        if dry:
            options.append("--dry-run")
        result = run(fleet, command, *options)
        assert result.returncode != 0, (command, name, result.stdout, result.stderr)
        assert "invalid profile name" in result.stderr
        assert contents(fleet) == before


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("kind", ("missing", "file", "inside-link", "outside-link", "broken-link"))
def test_invalid_desk_has_no_effect(fleet, command, kind):
    profiles = fleet / "profiles"
    if kind == "file":
        (profiles / "bad").write_text("not a directory")
    elif kind == "inside-link":
        (profiles / "bad").symlink_to(profiles / "alpha", target_is_directory=True)
    elif kind == "outside-link":
        outside = fleet.parent / "outside"
        outside.mkdir()
        (profiles / "bad").symlink_to(outside, target_is_directory=True)
    elif kind == "broken-link":
        (profiles / "bad").symlink_to(profiles / "absent", target_is_directory=True)
    before = contents(fleet)
    for dry in (False, True):
        result = run(fleet, command, "--profile", "bad", *(["--dry-run"] if dry else []))
        assert result.returncode != 0, (command, kind, result.stdout, result.stderr)
        assert "existing real directory" in result.stderr
        assert contents(fleet) == before


@pytest.mark.parametrize("command", COMMANDS[:-1])
@pytest.mark.parametrize("kind", ("inside-link", "outside-link", "broken-link", "file", "invalid-name"))
def test_batch_rejects_bad_entry_before_alpha_changes(fleet, command, kind):
    profiles = fleet / "profiles"
    if kind == "invalid-name":
        (profiles / "bad.name").mkdir()
    elif kind == "file":
        (profiles / "bad").write_text("file")
    else:
        target = {"inside-link": profiles / "alpha", "outside-link": fleet.parent,
                  "broken-link": profiles / "absent"}[kind]
        (profiles / "bad").symlink_to(target, target_is_directory=True)
    before = contents(fleet)
    for dry in (False, True):
        result = run(fleet, command, "--all", *(["--dry-run"] if dry else []))
        assert result.returncode != 0, (command, kind, result.stdout, result.stderr)
        assert contents(fleet) == before


@pytest.mark.parametrize("kind", ("inside-link", "outside-link", "broken-link", "missing", "file", "invalid-name"))
def test_registered_pm_batch_rejects_before_probe_or_write(fleet, kind):
    profiles = fleet / "profiles"
    if kind == "invalid-name":
        name = "bad.name"
        (profiles / name).mkdir()
    else:
        name = "bad"
        if kind == "file":
            (profiles / name).write_text("file")
        elif kind.endswith("link"):
            target = {"inside-link": profiles / "alpha", "outside-link": fleet.parent,
                      "broken-link": profiles / "absent"}[kind]
            (profiles / name).symlink_to(target, target_is_directory=True)
    (fleet / "agents-registry.yaml").write_text(yaml.safe_dump({"agents": {
        "first": {"role": "pm", "profile_name": "alpha"},
        "second": {"role": "pm", "profile_name": name},
    }}))
    before = contents(fleet)
    for dry in (False, True):
        result = run(fleet, "project-skills", "--registered-pms", *(["--dry-run"] if dry else []))
        assert result.returncode != 0, (kind, result.stdout, result.stderr)
        assert "profile" in result.stderr.lower()
        assert contents(fleet) == before


def test_spy_no_subprocess_for_invalid_selectors(fleet, monkeypatch):
    """Even delegated helper validation must not reach the runtime adapter."""
    monkeypatch.setenv("HERMES_FLEET_HOME", str(fleet))
    spec = importlib.util.spec_from_file_location("pjan73_renderer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(a))
    for name in BAD_NAMES:
        with pytest.raises(ValueError):
            module.validate_profile(name)
    (fleet / "profiles" / "alias").symlink_to(fleet / "profiles" / "alpha", target_is_directory=True)
    with pytest.raises(ValueError):
        module.profile_dirs()
    assert called == []


def test_positive_single_and_batch_read_only_and_writers(fleet):
    for command in ("expected", "check", "status"):
        for args in (("--profile", "Bravo_2"), ("--all",)):
            before = contents(fleet)
            result = run(fleet, command, *args)
            assert result.returncode in (0, 1), (command, result.stderr)
            assert contents(fleet) == before
    for command in ("render", "init", "absorb", "repin", "memory-pin", "memory-template", "memory-endpoint"):
        for args in (("--profile", "Bravo_2"), ("--all",)):
            before = contents(fleet)
            result = run(fleet, command, *args, "--dry-run")
            assert result.returncode == 0, (command, result.stdout, result.stderr)
            assert contents(fleet) == before
    result = run(fleet, "render", "--profile", "Bravo_2")
    assert result.returncode == 0, result.stderr
    assert "GENERATED FILE" in (fleet / "profiles/Bravo_2/config.yaml").read_text()
    result = run(fleet, "render", "--all")
    assert result.returncode == 0, result.stderr
    assert "GENERATED FILE" in (fleet / "profiles/alpha/config.yaml").read_text()
