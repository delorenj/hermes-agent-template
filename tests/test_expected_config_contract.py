"""ONE expected config: ``expected_config`` = deep_merge(base, delta) + every renderer pin.

2026-10-18: the renderer started pinning ``terminal.cwd`` to the registry ``project_path``, but five
consumers still called ``deep_merge(base, delta)`` "expected" and four helpers carried a private merge.
Every pinned desk was refused as out-of-band drift (skills-policy, project-skills: 26/26 blocked), and
any writer that did run rewrote config.yaml WITHOUT the pin. These tests hold every reader and writer
to the renderer's one definition, and pin the fresh-hire ordering (row written after step 10).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts" / "hermes-profile-config.py"
LIB = ROOT / "template" / ".scripts" / "lib"


def _load(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "fleet"
    project = tmp_path / "project"
    project.mkdir()
    profile = home / "profiles" / "x-pm"
    profile.mkdir(parents=True)
    base = {"terminal": {"backend": "local", "cwd": "."}, "skills": {"external_dirs": ["legacy"]},
            "operator": {"fleet": "keep"}}
    (home / "config.yaml").write_text(yaml.safe_dump(base))
    (profile / "config.delta.yaml").write_text("# operator header\noperator:\n  profile: keep\n")
    (home / "agents-registry.yaml").write_text(yaml.safe_dump({"agents": {"x-pm": {
        "role": "pm", "profile_name": "x-pm", "project_path": str(project)}}}))
    monkeypatch.setenv("HERMES_FLEET_HOME", str(home))
    monkeypatch.delenv("HERMES_AGENTS_REGISTRY", raising=False)
    renderer = _load(RENDERER, "expected_contract_renderer")
    delta = renderer.load_yaml(profile / "config.delta.yaml")
    return dict(home=home, project=project, profile=profile, base=base, delta=delta, renderer=renderer)


def _cli(fleet, *args, **extra):
    env = {**os.environ, "HERMES_FLEET_HOME": str(fleet["home"]), **extra}
    env.pop("HERMES_AGENTS_REGISTRY", None)
    return subprocess.run([sys.executable, "-I", str(RENDERER), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def _config(fleet) -> dict:
    return yaml.safe_load((fleet["profile"] / "config.yaml").read_text())


def _write_config(fleet, config: dict) -> None:
    fleet["renderer"].write_generated(fleet["profile"] / "config.yaml", config)


def test_expected_config_is_the_merge_plus_every_renderer_pin(fleet):
    r = fleet["renderer"]
    expected = r.expected_config(fleet["base"], fleet["delta"], "x-pm")
    merged = r.deep_merge(fleet["base"], fleet["delta"])
    assert expected["terminal"]["cwd"] == str(fleet["project"])
    assert merged["terminal"]["cwd"] == "."
    assert r.without_render_pins(expected) == r.without_render_pins(merged)
    # A Path names the same profile as its directory name.
    assert r.expected_config(fleet["base"], fleet["delta"], fleet["profile"]) == expected
    # render == check == expected
    assert _cli(fleet, "render", "--profile", "x-pm").returncode == 0
    assert _config(fleet) == expected
    check = _cli(fleet, "check")
    assert check.returncode == 0 and check.stdout.startswith("OK:")
    shown = _cli(fleet, "expected", "--profile", "x-pm")
    assert json.loads(shown.stdout) == expected


def test_is_render_of_tolerates_only_stale_renderer_pins(fleet):
    r = fleet["renderer"]
    stale = r.deep_merge(fleet["base"], fleet["delta"])  # rendered before the registry row existed
    assert r.is_render_of(stale, fleet["base"], fleet["delta"], "x-pm")
    edited = {**stale, "operator": {**stale["operator"], "out_of_band": True}}
    assert not r.is_render_of(edited, fleet["base"], fleet["delta"], "x-pm")
    assert not r.is_render_of(None, fleet["base"], fleet["delta"], "x-pm")


def test_repin_rewrites_a_pin_only_difference_and_refuses_real_drift(fleet):
    r = fleet["renderer"]
    _write_config(fleet, r.deep_merge(fleet["base"], fleet["delta"]))
    drift = _cli(fleet, "check")
    assert drift.returncode == 1
    assert "drift in: terminal" in drift.stdout and "ONLY on renderer pins" in drift.stdout
    assert _cli(fleet, "repin", "--dry-run").returncode == 0
    assert _config(fleet)["terminal"]["cwd"] == "."
    repinned = _cli(fleet, "repin", "--profile", "x-pm")
    assert repinned.returncode == 0 and "repin  x-pm" in repinned.stdout
    assert _config(fleet)["terminal"]["cwd"] == str(fleet["project"])
    assert "ok     x-pm" in _cli(fleet, "repin", "--profile", "x-pm").stdout

    edited = _config(fleet)
    edited["operator"]["out_of_band"] = True
    _write_config(fleet, edited)
    before = (fleet["profile"] / "config.yaml").read_bytes()
    refused = _cli(fleet, "repin", "--profile", "x-pm")
    assert refused.returncode == 1 and "DRIFT" in refused.stdout
    assert (fleet["profile"] / "config.yaml").read_bytes() == before


def test_check_warns_loudly_on_a_stale_registry_project_path(fleet):
    (fleet["home"] / "agents-registry.yaml").write_text(yaml.safe_dump({"agents": {"x-pm": {
        "profile_name": "x-pm", "project_path": str(fleet["home"] / "moved-away")}}}))
    assert _cli(fleet, "render", "--profile", "x-pm").returncode == 0
    assert _config(fleet)["terminal"]["cwd"] == "."
    check = _cli(fleet, "check")
    assert check.returncode == 0
    assert check.stdout.startswith("OK:")  # flume's observer keys on the first line
    assert "STALE registry project_path" in check.stdout and "x-pm" in check.stdout
    assert "WARN: x-pm" in check.stderr


def test_skills_policy_keeps_the_pin_and_accepts_a_stale_render(fleet):
    r = fleet["renderer"]
    _write_config(fleet, r.deep_merge(fleet["base"], fleet["delta"]))  # no pin yet
    assert _cli(fleet, "skills-policy", "--profile", "x-pm", "--dry-run").returncode == 0
    applied = _cli(fleet, "skills-policy", "--profile", "x-pm")
    assert applied.returncode == 0, applied.stderr
    config = _config(fleet)
    assert config["terminal"]["cwd"] == str(fleet["project"])
    assert config["skills"]["external_dirs"] == []
    assert _cli(fleet, "check").returncode == 0
    before = (fleet["profile"] / "config.yaml").read_bytes()
    again = _cli(fleet, "skills-policy", "--profile", "x-pm", "--dry-run")
    assert again.returncode == 0 and "unchanged" in again.stdout
    assert (fleet["profile"] / "config.yaml").read_bytes() == before


def test_voice_config_check_and_reconcile_preserve_the_pin(fleet):
    voice = LIB / "voice-config.py"
    _cli(fleet, "render", "--profile", "x-pm")
    args = ["--base", str(fleet["home"] / "config.yaml"), "--delta", str(fleet["profile"] / "config.delta.yaml"),
            "--generated", str(fleet["profile"] / "config.yaml"), "--plugin", "vox", "--voice", "carlin"]
    env = {**os.environ, "PROFILE_RENDERER": str(RENDERER)}
    reconciled = subprocess.run([sys.executable, "-I", str(voice), "reconcile", *args],
                                env=env, capture_output=True, text=True, timeout=30)
    assert reconciled.returncode == 0, reconciled.stderr
    assert _config(fleet)["terminal"]["cwd"] == str(fleet["project"])
    checked = subprocess.run([sys.executable, "-I", str(voice), "check", *args],
                             env=env, capture_output=True, text=True, timeout=30)
    assert checked.stdout.strip() == "ok"
    assert _cli(fleet, "check").returncode == 0


def test_profile_renderer_locator_prefers_explicit_then_its_own_checkout(tmp_path, monkeypatch):
    locator = _load(LIB / "profile-renderer.py", "locator_under_test")
    monkeypatch.delenv("PROFILE_RENDERER", raising=False)
    assert locator.locate() == RENDERER  # template/.scripts/lib -> scripts/
    missing = tmp_path / "missing.py"
    monkeypatch.setenv("PROFILE_RENDERER", str(missing))
    assert locator.candidates()[0] == missing
    assert locator.locate() == RENDERER  # a missing explicit path falls through, never guessed
    module = locator.load(tmp_path / "some-fleet")
    assert module.HERMES_HOME == tmp_path / "some-fleet"
    assert callable(module.expected_config)


PRIVATE_MERGE = re.compile(r"def\s+(?:_?plain_merge|_?plain_deep_merge|_?deep_merge|apply_list_patches)\s*\(")


def test_no_consumer_carries_a_private_merge_or_calls_a_bare_deep_merge():
    """Only the renderer may define the merge; consumers ask it for expected_config."""
    sources = [*(ROOT / "scripts").glob("*.py"), *(ROOT / "scripts").glob("*.sh"),
               *(ROOT / "template" / ".scripts").rglob("*.py"), *(ROOT / "template" / ".scripts").rglob("*.sh")]
    offenders = []
    for path in sources:
        if path == RENDERER or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if PRIVATE_MERGE.search(text) or re.search(r"\bdeep_merge\(", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
