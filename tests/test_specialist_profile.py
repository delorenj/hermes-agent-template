"""Canonical adapter projection, conflict and lock regressions (no inference)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).parents[1]
ADAPTER = ROOT / "scripts/hermes-specialist-profile.py"
RENDERER = ROOT / "scripts/hermes-profile-config.py"


def fixture(tmp_path):
    fleet = tmp_path / "fleet"
    (fleet / "profiles").mkdir(parents=True)
    (fleet / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "automaticai/personal/sol"}, "operator": {"base": "keep"}, "skills": {"external_dirs": ["/foreign"]}}))
    request = {"id": "specialist", "display_name": "Specialist", "definition": str(tmp_path / "desk/agent.yaml"), "charter": {"purpose": "Portable charter"}, "memory": {"write_bank": "agent-specialist", "recall_banks": ["infra"]}}
    env = {**os.environ, "HERMES_FLEET_HOME": str(fleet), "PYTHONDONTWRITEBYTECODE": "1"}
    profile = fleet / "profiles/specialist"
    return fleet, profile, request, env


def run(request, env, *args):
    return subprocess.run([sys.executable, str(ADAPTER), *args], input=json.dumps(request), env=env, text=True, capture_output=True, timeout=10)


def snapshot(profile):
    return {str(p.relative_to(profile)): (p.read_bytes(), p.stat().st_mtime_ns) for p in profile.rglob("*") if p.is_file()}


def wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"no barrier: {path}"
        time.sleep(.02)


def test_project_refresh_noop_and_preservation(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["skills"]["external_dirs"] == []
    assert config["skills"]["project_discovery"] is False
    assert config["model"]["provider"] == "automaticai"
    assert config["providers"]["automaticai"]["api"] == "https://api.automaticai.io/v1"
    assert json.loads((profile / "hindsight/config.json").read_text())["bank_id"] == "agent-specialist"
    (profile / "handwritten.md").write_text("keep")
    metadata = yaml.safe_load((profile / "profile.yaml").read_text())
    metadata["config"]["operator_note"] = "keep"
    (profile / "profile.yaml").write_text(yaml.safe_dump(metadata, sort_keys=False))
    request["charter"]["purpose"] = "New charter"
    request["memory"]["recall_banks"] = ["docker"]
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    assert "New charter" in (profile / "SOUL.md").read_text()
    assert yaml.safe_load((profile / "profile.yaml").read_text())["config"]["operator_note"] == "keep"
    assert (profile / "handwritten.md").read_text() == "keep"
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changed"] == []
    assert snapshot(profile) == before


@pytest.mark.parametrize("conflict", ["SOUL.md", "config.yaml", "config.delta.yaml", "hindsight/config.json"])
def test_ownership_conflict_leaves_projection_untouched(tmp_path, conflict):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    path = profile / conflict
    if conflict == "config.delta.yaml":
        data = yaml.safe_load(path.read_text()); data["skills"]["external_dirs"] = ["/handwritten"]
        path.write_text(yaml.safe_dump(data))
    elif conflict == "config.yaml":
        data = yaml.safe_load(path.read_text()); data["handwritten"] = "keep"
        path.write_text(yaml.safe_dump(data))
    elif conflict.endswith("json"):
        path.write_text(json.dumps({"bank_id": "agent-other"}))
    else:
        path.write_text("handwritten")
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0
    assert "ownership conflict" in result.stderr
    assert snapshot(profile) == before


def test_raw_inherited_credential_refused_before_profile_creation(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    (fleet / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "fixture"}, "provider": {"api_key": "fixture-raw-value"}}))
    result = run(request, env)
    assert result.returncode != 0
    assert "raw credential" in result.stderr
    assert not profile.exists()


def test_dashboard_auth_secret_is_not_inherited_or_written(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    base = yaml.safe_load((fleet / "config.yaml").read_text())
    base["dashboard"] = {"basic_auth": {"enabled": True, "secret": "fixture-dashboard-value"}, "theme": "keep"}
    (fleet / "config.yaml").write_text(yaml.safe_dump(base))
    before = (fleet / "config.yaml").read_bytes()
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert not profile.exists()
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["dashboard"]["enabled"] is False
    assert config["dashboard"]["basic_auth"] == {"enabled": False, "secret": ""}
    assert config["dashboard"]["theme"] == "keep"
    for path in profile.rglob("*"):
        if path.is_file():
            assert "fixture-dashboard-value" not in path.read_text()
    assert (fleet / "config.yaml").read_bytes() == before


def test_profile_lock_precedes_snapshot_and_preserves_concurrent_settings(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    ready, resume, attempt = [tmp_path / name for name in ["ready", "resume", "attempt"]]
    holder_code = """
import importlib.util, os, pathlib, time
spec=importlib.util.spec_from_file_location('renderer',os.environ['TEST_RENDERER']);r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
p=r.PROFILES/'specialist'
with r.PROFILE_LOCK.ProfileConfigLock(p):
 pathlib.Path(os.environ['TEST_READY']).write_text('held')
 while not pathlib.Path(os.environ['TEST_RESUME']).exists():time.sleep(.02)
 delta=r.load_yaml(p/'config.delta.yaml');delta['operator']={'concurrent':'keep'}
 (p/'config.delta.yaml').write_text(r.dump_yaml(delta));r.write_generated(p/'config.yaml',r.deep_merge(r.load_yaml(r.BASE),delta))
"""
    holder_env = {**env, "TEST_RENDERER": str(RENDERER), "TEST_READY": str(ready), "TEST_RESUME": str(resume)}
    holder = subprocess.Popen([sys.executable, "-c", holder_code], env=holder_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child = None
    try:
        wait_for(ready)
        child = subprocess.Popen([sys.executable, str(ADAPTER)], env={**env, "PJANGLER_TEST_PROFILE_CONFIG_LOCK_ATTEMPT": str(attempt)}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child.stdin.write(json.dumps(request)); child.stdin.close(); child.stdin = None
        wait_for(attempt)
        assert child.poll() is None
        resume.write_text("go")
        out, err = holder.communicate(timeout=5)
        assert holder.returncode == 0, err
        out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
        config = yaml.safe_load((profile / "config.yaml").read_text())
        assert config["operator"] == {"base": "keep", "concurrent": "keep"}
        assert json.loads(out)["changed"]
    finally:
        for process in [child, holder]:
            if process and process.poll() is None:
                process.kill(); process.wait()
