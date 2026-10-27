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
    env = {**os.environ, "HERMES_FLEET_HOME": str(fleet), "PYTHONDONTWRITEBYTECODE": "1",
           "HERMES_TEMPLATE_CONFIG": str(tmp_path / "no-config"),
           "VOX_PLUGIN_DIR": str(tmp_path / "no-plugin"), "VOX_PLUGIN_NAME": "", "VOX_VOICE": "", "VOX_URL": ""}
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
    assert config["skills"]["inherit_global"] is True
    assert config["skills"]["create_dir"] == "learned-skills"
    assert config["curator"]["enabled"] is False
    assert yaml.safe_load((profile / "config.delta.yaml").read_text())["skills"]["inherit_global"] is True
    assert json.loads((profile / "specialist-projection.json").read_text())["config"]["skills.inherit_global"] is True
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
    delta["skills"]["inherit_global"] = False
    path.write_text(yaml.safe_dump(delta))
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0
    assert "ownership conflict: config skills.inherit_global" in result.stderr
    assert snapshot(profile) == before
    assert (fleet / "config.yaml").read_bytes() == base


def test_managed_false_inheritance_receipt_migrates_to_true(tmp_path):
    """A desk managed under the old inherit_global:false policy refreshes to true.

    The renderer's receipt ownership accepts the old managed value, then pins the
    new one; unrelated operator config survives, and the follow-up run is a no-op.
    """
    fleet, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    delta_path = profile / "config.delta.yaml"
    delta = yaml.safe_load(delta_path.read_text())
    delta["skills"]["inherit_global"] = False
    delta["operator"] = {"revision": "preserve"}
    delta_path.write_text(yaml.safe_dump(delta))
    receipt_path = profile / "specialist-projection.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["config"]["skills.inherit_global"] = False
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["changed"]
    assert not (profile / ".specialist-pending.json").exists()
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    assert snapshot(profile) != before
    delta = yaml.safe_load(delta_path.read_text())
    assert delta["skills"]["inherit_global"] is True
    assert delta["operator"] == {"revision": "preserve"}
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["skills"]["inherit_global"] is True
    assert config["skills"]["external_dirs"] == []
    assert config["operator"] == {"base": "keep", "revision": "preserve"}
    assert json.loads(receipt_path.read_text())["config"]["skills.inherit_global"] is True
    complete = snapshot(profile)
    again = run(request, env)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] == []
    assert snapshot(profile) == complete


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
 (p/'config.delta.yaml').write_text(r.dump_yaml(delta));r.write_generated(p/'config.yaml',r.expected_config(r.load_yaml(r.BASE),delta,p))
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


def test_flume39_gateway_key_accepted_in_delta(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    flume39_ref = "op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/hermes-specialist"
    delta_path = profile / "config.delta.yaml"
    delta = yaml.safe_load(delta_path.read_text())
    delta.setdefault("secrets", {}).setdefault("onepassword", {}).setdefault("env", {})["AUTOMATICAI_GATEWAY_KEY"] = flume39_ref
    delta_path.write_text(yaml.safe_dump(delta))

    # check mode detects difference in specialist-projection.json
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert "specialist-projection.json" in json.loads(preview.stdout)["changed"]

    # refresh mode applies and converges
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["secrets"]["onepassword"]["env"]["AUTOMATICAI_GATEWAY_KEY"] == flume39_ref
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    assert receipt["config"]["secrets.onepassword.env.AUTOMATICAI_GATEWAY_KEY"] == flume39_ref

    # subsequent check is a clean no-op
    clean = run(request, env, "--check")
    assert clean.returncode == 0, clean.stderr
    assert json.loads(clean.stdout)["changed"] == []


def test_foreign_gateway_key_refused_as_ownership_conflict(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    delta_path = profile / "config.delta.yaml"
    delta = yaml.safe_load(delta_path.read_text())
    delta.setdefault("secrets", {}).setdefault("onepassword", {}).setdefault("env", {})["AUTOMATICAI_GATEWAY_KEY"] = "op://DeLoSecrets/foreign-item/foreign-key"
    delta_path.write_text(yaml.safe_dump(delta))

    refused = run(request, env)
    assert refused.returncode != 0
    assert "ownership conflict: config secrets.onepassword.env.AUTOMATICAI_GATEWAY_KEY" in refused.stderr



def test_hermes_restyled_yaml_is_not_projection_drift(tmp_path):
    """Hermes re-dumps config.yaml/config.delta.yaml in its own style after a run; same value, no drift."""
    fleet, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    for name in ("config.yaml", "config.delta.yaml"):
        data = yaml.safe_load((profile / name).read_text())
        (profile / name).write_text(yaml.safe_dump(data, sort_keys=True, indent=4, width=10_000, default_style=None))
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["changed"] == []
    refresh = run(request, env)
    assert refresh.returncode == 0, refresh.stderr
    assert json.loads(refresh.stdout)["changed"] == []


def test_desk_from_before_a_new_pin_refreshes_after_hermes_restyle(tmp_path):
    """A desk projected before a pin existed, then re-dumped by Hermes, is not 'handwritten'."""
    fleet, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    for name in ("config.delta.yaml", "config.yaml"):
        data = yaml.safe_load((profile / name).read_text())
        data["skills"].pop("create_dir")
        data.pop("curator")
        (profile / name).write_text(yaml.safe_dump(data, sort_keys=True, indent=4, width=10_000))
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    receipt["files"]["config.yaml"] = "0" * 64
    for key in ("skills.create_dir", "curator.enabled"):
        receipt["config"].pop(key)
    (profile / "specialist-projection.json").write_text(json.dumps(receipt))
    refresh = run(request, env)
    assert refresh.returncode == 0, refresh.stderr
    assert yaml.safe_load((profile / "config.yaml").read_text())["skills"]["create_dir"] == "learned-skills"


def voice_env(env, tmp_path, plugin_dir=True):
    """Isolate voice resolution: a fixture template TOML and plugin dir."""
    toml_dir = tmp_path / "xdg/hermes-agent-template"
    toml_dir.mkdir(parents=True)
    plugin = tmp_path / "voxxy/plugins/tts/vox"
    if plugin_dir:
        plugin.mkdir(parents=True)
    (toml_dir / "config.toml").write_text(
        f'[fleet]\nvox_plugin_name = "vox"\nvox_plugin_dir = "{plugin}"\nvox_voice = "carlin"\n')
    return {**env, "HERMES_TEMPLATE_CONFIG": str(toml_dir / "config.toml"), "VOX_PLUGIN_DIR": ""}, plugin


def test_voice_contract_and_plugin_link_land(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    assert delta["tts"] == {"provider": "vox", "voice": "carlin", "vox": {"voice": "carlin", "base_url": "https://vox.delo.sh"}}
    patches = delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"]
    assert patches["add"] == ["tts/vox"]
    assert patches["remove"] == ["tts/voxxy"]
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["tts"]["provider"] == "vox"
    assert config["tts"]["voice"] == "carlin"
    assert config["tts"]["vox"]["voice"] == "carlin"
    link = profile / "plugins/tts/vox"
    assert link.is_symlink() and link.resolve() == plugin
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    assert receipt["voice"] == {"plugin": "vox", "voice": "carlin", "plugin_dir": str(plugin), "installed": True}
    # idempotent re-run: no managed bytes or receipts change
    before = snapshot(profile)
    again = run(request, env)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changed"] == []
    assert snapshot(profile) == before


def test_definition_voice_overrides_fleet_default(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    request["voice"] = "walken"
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    assert delta["tts"]["voice"] == "walken"
    assert delta["tts"]["vox"]["voice"] == "walken"
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["tts"]["voice"] == "walken"
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    assert receipt["voice"]["voice"] == "walken"


def test_invalid_definition_voice_refuses_before_effects(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    request["voice"] = "bad voice!"
    result = run(request, env)
    assert result.returncode != 0
    assert "voice contract refused" in result.stderr
    assert not profile.exists()


def test_missing_plugin_dir_defers_link_but_lands_contract(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path, plugin_dir=False)
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    assert "voice_deferred" in json.loads(result.stdout)
    assert not (profile / "plugins/tts/vox").exists()
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["tts"]["provider"] == "vox"
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    assert receipt["voice"]["installed"] is False


def test_foreign_plugin_occupation_refuses(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    assert run(request, env).returncode == 0
    link = profile / "plugins/tts/vox"
    link.unlink()
    link.mkdir()
    (link / "foreign").write_text("keep")
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0
    assert "ownership conflict" in result.stderr
    assert snapshot(profile) == before
    assert (link / "foreign").read_text() == "keep"


def test_check_reports_voice_drift(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    assert run(request, env).returncode == 0
    request["voice"] = "walken"
    before = snapshot(profile)
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    changed = json.loads(preview.stdout)["changed"]
    assert "config.delta.yaml" in changed and "config.yaml" in changed, changed
    assert snapshot(profile) == before
    # broken link is drift too
    link = profile / "plugins/tts/vox"
    link.unlink()
    drift = run(request, env, "--check")
    assert drift.returncode == 0, drift.stderr
    assert "plugins/tts/vox" in json.loads(drift.stdout)["changed"]
    assert not link.exists()


def test_sealed_legacy_plugins_snapshot_migrates(tmp_path):
    """A delta carrying the sealed 52d9445 fleet snapshot migrates instead of freezing."""
    fleet, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    sealed = ["bloodbank-platform", "copilot-provider", "fal", "gemini-provider", "google_meet",
              "kimi-coding-provider", "ntfy-platform", "openai-codex", "openrouter", "self-hosted",
              "slack-platform", "teams-platform", "teams_pipeline", "telegram-platform",
              "tts/vox", "web-brave-free", "web-tavily"]
    (fleet / "profiles/specialist").mkdir(parents=True, exist_ok=True)
    (fleet / "profiles/specialist/config.delta.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": sealed}}))
    (fleet / "profiles/specialist/specialist-projection.json").write_text(json.dumps(
        {"id": "specialist", "definition": request["definition"], "config": {}, "metadata": {}, "files": {}}))
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    assert "enabled" not in delta.get("plugins", {})
    migration = delta["x-pjangler-merge"]["migrations"]["plugins_enabled_52d9445"]
    assert migration["mode"] == "inherited-snapshot"
    assert delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"]["add"] == ["tts/vox"]


@pytest.mark.parametrize("absent_target", [False, True])
@pytest.mark.parametrize("installed", [False, True])
def test_wrong_target_link_refuses_without_unlink(tmp_path, absent_target, installed):
    _, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path, plugin_dir=installed)
    assert run(request, env).returncode == 0
    link = profile / "plugins/tts/vox"
    if link.is_symlink():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign-plugin"
    if not absent_target:
        foreign.mkdir()
    link.symlink_to(foreign)
    before = snapshot(profile)
    for args in [("--check",), ()]:
        result = run(request, env, *args)
        assert result.returncode != 0
        assert "ownership conflict" in result.stderr
        assert os.readlink(link) == str(foreign)
        assert snapshot(profile) == before


@pytest.mark.parametrize("ancestor", ["plugins", "plugins/tts"])
@pytest.mark.parametrize("installed", [False, True])
def test_plugin_ancestor_symlink_refuses_without_foreign_writes(tmp_path, ancestor, installed):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path, plugin_dir=installed)
    assert run(request, env).returncode == 0
    import shutil
    root = profile / ancestor
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep").write_text("unchanged")
    root.symlink_to(foreign)
    before = snapshot(profile)
    for args in [("--check",), ()]:
        result = run(request, env, *args)
        assert result.returncode != 0
        assert "unsafe symlink" in result.stderr
        assert snapshot(profile) == before
        assert list(foreign.iterdir()) == [foreign / "keep"]


@pytest.mark.parametrize("after", [".specialist-pending.json", "plugins/tts/vox", "SOUL.md", "specialist-projection.json"])
@pytest.mark.parametrize("refresh", [False, True])
def test_plugin_publication_is_journaled_and_recoverable(tmp_path, after, refresh):
    _, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    if refresh:
        assert run(request, env).returncode == 0
        (profile / "plugins/tts/vox").unlink()
        request["charter"]["purpose"] = "Refreshed"
    result = run(request, {**env, "FLUME_TEST_SPECIALIST_INTERRUPT_AFTER": after})
    assert result.returncode == 91, result.stderr
    pending = json.loads((profile / ".specialist-pending.json").read_text())
    assert pending["links"] == {"plugins/tts/vox": str(plugin)}
    assert pending["before_links"] == {"plugins/tts/vox": None}
    link = profile / "plugins/tts/vox"
    assert link.is_symlink() == (after != ".specialist-pending.json")
    before = snapshot(profile)
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert snapshot(profile) == before
    assert run(request, env).returncode == 0
    assert os.readlink(link) == str(plugin)
    assert not (profile / ".specialist-pending.json").exists()
    assert json.loads(run(request, env).stdout)["changed"] == []


def test_pending_link_conflict_refuses_recovery(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    assert run(request, {**env, "FLUME_TEST_SPECIALIST_INTERRUPT_AFTER": "plugins/tts/vox"}).returncode == 91
    link = profile / "plugins/tts/vox"
    link.unlink()
    link.symlink_to(tmp_path / "foreign")
    before = snapshot(profile)
    for args in [("--check",), ()]:
        result = run(request, env, *args)
        assert result.returncode != 0 and "ownership conflict" in result.stderr
        assert snapshot(profile) == before
        assert os.readlink(link) == str(tmp_path / "foreign")


@pytest.mark.parametrize("leaves", [("voice",), ("vox",), ("voice", "vox")])
def test_profile_voice_override_survives_refresh(tmp_path, leaves):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    assert run(request, env).returncode == 0
    path = profile / "config.delta.yaml"
    delta = yaml.safe_load(path.read_text())
    for leaf in leaves:
        if leaf == "vox":
            delta["tts"]["vox"]["voice"] = "walken"
        else:
            delta["tts"]["voice"] = "walken"
    path.write_text(yaml.safe_dump(delta))
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    for name in ["config.delta.yaml", "config.yaml"]:
        tts = yaml.safe_load((profile / name).read_text())["tts"]
        assert tts["voice"] == tts["vox"]["voice"] == "walken"
    assert json.loads((profile / "specialist-projection.json").read_text())["voice"]["voice"] == "walken"
    assert json.loads(run(request, env).stdout)["changed"] == []


def test_definition_voice_updates_managed_pin_but_refuses_foreign_override(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    request["voice"] = "carlin"
    assert run(request, env).returncode == 0
    request["voice"] = "walken"
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    path = profile / "config.delta.yaml"
    delta = yaml.safe_load(path.read_text())
    delta["tts"]["voice"] = delta["tts"]["vox"]["voice"] = "rick"
    path.write_text(yaml.safe_dump(delta))
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0 and "ownership conflict" in result.stderr
    assert snapshot(profile) == before


@pytest.mark.parametrize("kind", ["malformed", "symlink", "directory", "bad-fleet"])
def test_invalid_template_config_fails_closed(tmp_path, kind):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    config = Path(env["HERMES_TEMPLATE_CONFIG"])
    config.unlink()
    if kind == "symlink":
        target = tmp_path / "toml"
        target.write_text('[fleet]\nvox_voice="walken"')
        config.symlink_to(target)
    elif kind == "directory":
        config.mkdir()
    else:
        config.write_text("[fleet" if kind == "malformed" else "fleet = 17")
    result = run(request, env)
    assert result.returncode != 0
    assert not profile.exists()


def test_missing_template_config_uses_fleet_base_voice_and_endpoint(tmp_path):
    fleet, profile, request, env = fixture(tmp_path)
    base_path = fleet / "config.yaml"
    base = yaml.safe_load(base_path.read_text())
    base["tts"] = {"provider": "vox", "voice": "walken", "vox": {"voice": "walken", "base_url": "https://vox-base.test"}}
    base_path.write_text(yaml.safe_dump(base))
    result = run(request, env)
    assert result.returncode == 0, result.stderr
    tts = yaml.safe_load((profile / "config.yaml").read_text())["tts"]
    assert tts["voice"] == "walken" and tts["vox"]["base_url"] == "https://vox-base.test"


def test_explicit_environment_overrides_template_defaults(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, plugin = voice_env(env, tmp_path)
    other = tmp_path / "override-plugin"
    other.mkdir()
    result = run(request, {**env, "VOX_PLUGIN_DIR": str(other), "VOX_VOICE": "walken", "VOX_URL": "https://vox-override.test"})
    assert result.returncode == 0, result.stderr
    tts = yaml.safe_load((profile / "config.yaml").read_text())["tts"]
    assert tts["voice"] == "walken" and tts["vox"]["base_url"] == "https://vox-override.test"
    assert os.readlink(profile / "plugins/tts/vox") == str(other)


@pytest.mark.parametrize("voice", ["walken", ""])
def test_pre_parity_receipt_preserves_voice_or_refuses_invalid_override(tmp_path, voice):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    assert run(request, env).returncode == 0
    receipt_path = profile / "specialist-projection.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.pop("voice")
    receipt["config"] = {k: v for k, v in receipt["config"].items() if not k.startswith("tts.")}
    receipt_path.write_text(json.dumps(receipt))
    path = profile / "config.delta.yaml"
    delta = yaml.safe_load(path.read_text())
    delta["tts"]["voice"] = delta["tts"]["vox"]["voice"] = voice
    path.write_text(yaml.safe_dump(delta))
    before = snapshot(profile)
    result = run(request, env)
    if voice:
        assert result.returncode == 0, result.stderr
        assert yaml.safe_load((profile / "config.yaml").read_text())["tts"]["voice"] == voice
    else:
        assert result.returncode != 0 and "invalid TTS voice" in result.stderr
        assert snapshot(profile) == before


def test_ambiguous_legacy_plugins_snapshot_refuses_without_effects(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    assert run(request, env).returncode == 0
    path = profile / "config.delta.yaml"
    delta = yaml.safe_load(path.read_text())
    delta["plugins"] = {"enabled": ["bloodbank-platform", "tts/vox"]}
    path.write_text(yaml.safe_dump(delta))
    before = snapshot(profile)
    result = run(request, env)
    assert result.returncode != 0
    assert "partially matches sealed 52d history" in result.stderr
    assert snapshot(profile) == before


def test_credential_url_refuses_before_link_or_journal(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    result = run(request, {**env, "VOX_URL": "https://user:fixture-secret@vox.test"})
    assert result.returncode != 0 and "without credentials" in result.stderr
    assert "fixture-secret" not in result.stderr
    assert not profile.exists()


def test_definition_voice_beats_environment_default(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    env, _ = voice_env(env, tmp_path)
    request["voice"] = "walken"
    result = run(request, {**env, "VOX_VOICE": "carlin"})
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load((profile / "config.yaml").read_text())["tts"]["voice"] == "walken"
