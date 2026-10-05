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
    (fleet / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "automaticai/personal/sol"}, "operator": {"base": "keep"}, "skills": {"external_dirs": ["/foreign"], "inherit_global": True}, "providers": {"automaticai": {"enabled": False}}}))
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
    fleet, profile, request, env = fixture(tmp_path)
    base = (fleet / "config.yaml").read_bytes()
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["skills"]["external_dirs"] == []
    assert config["skills"]["project_discovery"] is False
    assert config["skills"]["inherit_global"] is False
    assert yaml.safe_load((profile / "config.delta.yaml").read_text())["skills"]["inherit_global"] is False
    assert json.loads((profile / "specialist-projection.json").read_text())["config"]["skills.inherit_global"] is False
    assert config["model"]["provider"] == "automaticai"
    assert config["providers"]["automaticai"]["api"] == "https://api.automaticai.io/v1"
    assert config["providers"]["automaticai"]["enabled"] is True
    assert config["providers"]["automaticai"]["key_env"] == "AUTOMATICAI_GATEWAY_KEY"
    assert config["secrets"]["onepassword"]["env"]["AUTOMATICAI_GATEWAY_KEY"] == "op://DeLoSecrets/hermes-specialist/credential"
    assert (fleet / "config.yaml").read_bytes() == base
    assert json.loads((profile / "hindsight/config.json").read_text())["bank_id"] == "agent-specialist"
    assert json.loads((profile / "hindsight/config.json").read_text())["recall_types"] == ["world", "experience", "observation"]
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


def test_global_inheritance_policy_refuses_handwritten_override(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    base = (fleet / "config.yaml").read_bytes()
    assert run(request, env).returncode == 0
    path = profile / "config.delta.yaml"
    delta = yaml.safe_load(path.read_text())
    delta["skills"]["inherit_global"] = True
    path.write_text(yaml.safe_dump(delta))
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0
    assert "ownership conflict: config skills.inherit_global" in result.stderr
    assert snapshot(profile) == before
    assert (fleet / "config.yaml").read_bytes() == base


@pytest.mark.parametrize("recall_types", [["observation"], []])
def test_memory_recall_type_override_survives_refresh_and_noop(tmp_path, recall_types):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    path = profile / "hindsight/config.json"
    memory = json.loads(path.read_text())
    memory["recall_types"] = recall_types
    memory["operator_note"] = "preserve"
    path.write_text(json.dumps(memory))
    request["charter"]["purpose"] = "Updated charter"
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    memory = json.loads(path.read_text())
    assert memory["recall_types"] == recall_types
    assert memory["operator_note"] == "preserve"
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changed"] == []
    assert snapshot(profile) == before


@pytest.mark.parametrize("name", ["config.delta.yaml", "profile.yaml", "hindsight/config.json"])
def test_raw_projected_config_credentials_refuse_without_writes(tmp_path, name):
    fleet, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    path = profile / name
    data = json.loads(path.read_text()) if name.endswith("json") else yaml.safe_load(path.read_text())
    data["api_key"] = "fixture-unvaulted-value"
    path.write_text(json.dumps(data) if name.endswith("json") else yaml.safe_dump(data))
    before, base = snapshot(profile), (fleet / "config.yaml").read_bytes()
    for args in [("--check",), ()]:
        result = run(request, env, *args)
        assert result.returncode != 0
        assert "raw credential cannot be projected" in result.stderr
        assert snapshot(profile) == before
        assert (fleet / "config.yaml").read_bytes() == base


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("after", ["SOUL.md", "config.delta.yaml", "config.yaml", "profile.yaml", "hindsight/config.json", ".skillex-only", "specialist-projection.json"])
def test_interrupted_publication_recovers_with_readonly_preview(tmp_path, refresh, after):
    fleet, profile, request, env = fixture(tmp_path)
    if refresh:
        assert run(request, env).returncode == 0
        request["charter"]["purpose"] = "New charter"
        request["display_name"] = "Updated Specialist"
        request["memory"]["recall_banks"] = ["docker"]
        delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
        delta["operator"] = {"revision": "preserve"}
        (profile / "config.delta.yaml").write_text(yaml.safe_dump(delta))
        memory = json.loads((profile / "hindsight/config.json").read_text())
        memory["operator_note"] = "preserve"
        (profile / "hindsight/config.json").write_text(json.dumps(memory))
    base = (fleet / "config.yaml").read_bytes()
    interrupted = run(request, {**env, "FLUME_TEST_SPECIALIST_INTERRUPT_AFTER": after})
    assert interrupted.returncode == 91, interrupted.stderr
    assert (profile / ".specialist-pending.json").is_file()
    before = snapshot(profile)
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert ".specialist-pending.json" in json.loads(preview.stdout)["changed"]
    assert snapshot(profile) == before
    recovered = run(request, env)
    assert recovered.returncode == 0, recovered.stderr
    assert not (profile / ".specialist-pending.json").exists()
    assert request["charter"]["purpose"] in (profile / "SOUL.md").read_text()
    if refresh:
        assert yaml.safe_load((profile / "config.yaml").read_text())["operator"]["revision"] == "preserve"
        assert json.loads((profile / "hindsight/config.json").read_text())["operator_note"] == "preserve"
    complete = snapshot(profile)
    again = run(request, env)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] == []
    assert snapshot(profile) == complete
    assert (fleet / "config.yaml").read_bytes() == base


@pytest.mark.parametrize("refresh", [False, True])
def test_interrupted_publication_does_not_adopt_handwritten_conflicts(tmp_path, refresh):
    _, profile, request, env = fixture(tmp_path)
    if refresh:
        assert run(request, env).returncode == 0
        request["charter"]["purpose"] = "Changed charter"
    assert run(request, {**env, "FLUME_TEST_SPECIALIST_INTERRUPT_AFTER": "SOUL.md"}).returncode == 91
    (profile / "SOUL.md").write_text("Handwritten conflicting charter")
    before = snapshot(profile)
    for args in [("--check",), ()]:
        refused = run(request, env, *args)
        assert refused.returncode != 0
        assert "ownership conflict: interrupted publication SOUL.md" in refused.stderr
        assert snapshot(profile) == before


def test_reserved_default_refuses_without_fleet_effects(tmp_path):
    fleet, _, request, env = fixture(tmp_path)
    request["id"] = "default"
    request["memory"]["write_bank"] = "agent-default"
    before = snapshot(fleet)
    result = run(request, env)
    assert result.returncode != 0
    assert "reserved Hermes profile identity" in result.stderr
    assert snapshot(fleet) == before


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
