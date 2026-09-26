from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
VOICE_HELPER = ROOT / "template" / ".scripts" / "lib" / "voice-config.py"
CHANNEL_HELPER = ROOT / "template" / ".scripts" / "channel-transaction.py"
PROFILE_RENDERER = ROOT / "scripts" / "hermes-profile-config.py"
PROFILE_SEEDER = ROOT / "template" / ".scripts" / "lib" / "profile-config-seed.py"


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _fixture(tmp_path: Path) -> dict[str, Path]:
    fleet = tmp_path / "home" / ".hermes"
    profile = fleet / "profiles" / "demo-pm"
    role = tmp_path / "role"
    runtime = role / "runtime"
    profile.mkdir(parents=True)
    runtime.mkdir(parents=True)
    base = {
        "plugins": {"enabled": ["core-one"]},
        "platforms": {"slack": {"enabled": True}},
        "operator": {"fleet": "preserve"},
    }
    delta = {
        "secrets": {
            "onepassword": {
                "enabled": True,
                "env": {
                    "SLACK_BOT_TOKEN": "op://DeLoSecrets/old/slack_bot_token",
                    "SLACK_APP_TOKEN": "op://DeLoSecrets/old/slack_app_token",
                },
            }
        },
        "platforms": {"slack": {"enabled": True}},
        "operator": {"profile": "preserve"},
    }
    (fleet / "config.yaml").write_text(
        yaml.safe_dump(base, sort_keys=False), encoding="utf-8"
    )
    (profile / "config.delta.yaml").write_text(
        yaml.safe_dump(delta, sort_keys=False), encoding="utf-8"
    )
    (profile / "config.yaml").write_text(
        yaml.safe_dump(_deep_merge(base, delta), sort_keys=False), encoding="utf-8"
    )
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        """repo: demo
role: pm
agent_id: demo-pm
profile: demo-pm
slack:
  provisioning_status: verified
  team_id: TOLD
  team_name: Old Workspace
  bot_user_id: UOLDBOT
  bot_id: BOLDBOT
  bot_username: old-pm
""",
        encoding="utf-8",
    )
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "demo-pm": {
                        "role_dir": str(role),
                        "profile_name": "demo-pm",
                        "operator_extension": "preserve",
                        "slack": {
                            "provisioning_status": "verified",
                            "team_id": "TOLD",
                            "team_name": "Old Workspace",
                            "bot_user_id": "UOLDBOT",
                            "bot_id": "BOLDBOT",
                            "bot_username": "old-pm",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runtime_env = runtime / ".env"
    runtime_env.write_text('SLACK_ALLOWED_USERS="UOLD"\n', encoding="utf-8")
    marker = role / ".scripts" / ".done-31-slack"
    marker.parent.mkdir()
    marker.write_bytes(b"")
    return {
        "fleet": fleet,
        "profile": profile,
        "role": role,
        "role_yaml": role_yaml,
        "registry": registry,
        "runtime_env": runtime_env,
        "marker": marker,
    }


def _voice_command(paths: dict[str, Path]) -> list[str]:
    profile = paths["profile"]
    return [
        sys.executable,
        "-I",
        str(VOICE_HELPER),
        "reconcile",
        "--base",
        str(paths["fleet"] / "config.yaml"),
        "--delta",
        str(profile / "config.delta.yaml"),
        "--generated",
        str(profile / "config.yaml"),
        "--plugin",
        "vox",
        "--voice",
        "carlin",
    ]


def _channel_command(paths: dict[str, Path]) -> list[str]:
    flock = shutil.which("flock")
    assert flock is not None
    registry = paths["registry"]
    return [
        flock,
        "-w",
        "5",
        f"{registry}.lock",
        sys.executable,
        "-I",
        str(CHANNEL_HELPER),
        "--channel",
        "slack",
        "--profile",
        str(paths["profile"]),
        "--role-yaml",
        str(paths["role_yaml"]),
        "--registry",
        str(registry),
        "--runtime-env",
        str(paths["runtime_env"]),
        "--done-marker",
        str(paths["marker"]),
        "--agent-id",
        "demo-pm",
        "--role-dir",
        str(paths["role"]),
        "--profile-name",
        "demo-pm",
        "--allowed-value",
        "U456",
        "--reference",
        "SLACK_BOT_TOKEN",
        "op://DeLoSecrets/new/slack_bot_token",
        "--reference",
        "SLACK_APP_TOKEN",
        "op://DeLoSecrets/new/slack_app_token",
        "--metadata",
        "provisioning_status",
        "verified",
        "--metadata",
        "team_id",
        "T456",
        "--metadata",
        "team_name",
        "New Workspace",
        "--metadata",
        "bot_user_id",
        "U456BOT",
        "--metadata",
        "bot_id",
        "B456BOT",
        "--metadata",
        "bot_username",
        "new-pm",
    ]


def _barrier_env(barrier: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PJANGLER_TEST_PROFILE_CONFIG_BARRIER"] = str(barrier)
    env["PJANGLER_TEST_PROFILE_CONFIG_BARRIER_TIMEOUT_SECONDS"] = "10"
    return env


def _attempt_env(attempt: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PJANGLER_TEST_PROFILE_CONFIG_LOCK_ATTEMPT"] = str(attempt)
    return env


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"process exited before {path.name}: {process.returncode}\n{stdout}\n{stderr}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"timed out waiting for {path}: {stdout}\n{stderr}")
        time.sleep(0.01)


def _wait_until_locked(path: Path, process: subprocess.Popen[str], timeout: float = 5) -> None:
    flock = shutil.which("flock")
    assert flock is not None
    deadline = time.monotonic() + timeout
    while True:
        probe = subprocess.run(
            [flock, "-n", str(path), "true"], capture_output=True, check=False
        )
        if probe.returncode == 1:
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"process exited before taking {path}: {process.returncode}\n{stdout}\n{stderr}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for lock ownership: {path}")
        time.sleep(0.01)


def _assert_converged(paths: dict[str, Path]) -> None:
    profile = paths["profile"]
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text(encoding="utf-8"))
    generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    references = delta["secrets"]["onepassword"]["env"]
    assert references == {
        "SLACK_BOT_TOKEN": "op://DeLoSecrets/new/slack_bot_token",
        "SLACK_APP_TOKEN": "op://DeLoSecrets/new/slack_app_token",
    }
    assert delta["platforms"]["slack"]["enabled"] is True
    assert delta["tts"]["provider"] == "vox"
    assert delta["tts"]["voice"] == "carlin"
    patch = delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"]
    assert patch == {"add": ["tts/vox"], "remove": ["tts/voxxy"]}
    assert generated["secrets"]["onepassword"]["env"] == references
    assert generated["platforms"]["slack"]["enabled"] is True
    assert generated["tts"]["provider"] == "vox"
    assert generated["plugins"]["enabled"] == ["core-one", "tts/vox"]
    assert generated["operator"] == {"fleet": "preserve", "profile": "preserve"}
    registry = yaml.safe_load(paths["registry"].read_text(encoding="utf-8"))
    entry = registry["agents"]["demo-pm"]
    assert entry["operator_extension"] == "preserve"
    assert entry["slack"]["team_id"] == "T456"
    assert entry["slack"]["bot_id"] == "B456BOT"
    role = yaml.safe_load(paths["role_yaml"].read_text(encoding="utf-8"))
    assert role["slack"]["team_id"] == "T456"
    assert role["slack"]["bot_id"] == "B456BOT"


def test_voice_snapshot_then_slack_rotation_cannot_restore_old_refs(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    barrier = tmp_path / "voice-first"
    voice = subprocess.Popen(
        _voice_command(paths),
        env=_barrier_env(barrier),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(Path(f"{barrier}.ready"), voice)
    assert Path(f"{barrier}.ready").read_text(encoding="utf-8") == "voice\n"

    channel_attempt = tmp_path / "channel-lock-attempt"
    channel = subprocess.Popen(
        _channel_command(paths),
        env=_attempt_env(channel_attempt),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(channel_attempt, channel)
    _wait_until_locked(Path(f"{paths['registry']}.lock"), channel)
    assert channel.poll() is None

    Path(f"{barrier}.resume").touch()
    voice_stdout, voice_stderr = voice.communicate(timeout=5)
    channel_stdout, channel_stderr = channel.communicate(timeout=5)
    assert voice.returncode == 0, voice_stdout + voice_stderr
    assert channel.returncode == 0, channel_stdout + channel_stderr
    _assert_converged(paths)


def test_slack_snapshot_then_voice_reconcile_cannot_restore_prevoice_config(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    barrier = tmp_path / "channel-first"
    channel = subprocess.Popen(
        _channel_command(paths),
        env=_barrier_env(barrier),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(Path(f"{barrier}.ready"), channel)
    assert Path(f"{barrier}.ready").read_text(encoding="utf-8") == "channel:slack\n"

    voice_attempt = tmp_path / "voice-lock-attempt"
    voice = subprocess.Popen(
        _voice_command(paths),
        env=_attempt_env(voice_attempt),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(voice_attempt, voice)
    profile_lock = paths["profile"].parent / ".demo-pm.config.lock"
    _wait_until_locked(profile_lock, channel)
    assert voice.poll() is None

    Path(f"{barrier}.resume").touch()
    channel_stdout, channel_stderr = channel.communicate(timeout=5)
    voice_stdout, voice_stderr = voice.communicate(timeout=5)
    assert channel.returncode == 0, channel_stdout + channel_stderr
    assert voice.returncode == 0, voice_stdout + voice_stderr
    _assert_converged(paths)


def test_profile_lock_timeout_is_truthful_and_process_crash_releases_lock(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    barrier = tmp_path / "crash-holder"
    holder = subprocess.Popen(
        _voice_command(paths),
        env=_barrier_env(barrier),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(Path(f"{barrier}.ready"), holder)

    timeout_env = os.environ.copy()
    timeout_env["HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS"] = "0.1"
    blocked = subprocess.run(
        _voice_command(paths),
        env=timeout_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert blocked.returncode != 0
    assert "timed out waiting for profile config lock" in blocked.stderr

    renderer_env = timeout_env.copy()
    renderer_env["HERMES_FLEET_HOME"] = str(paths["fleet"])
    renderer = subprocess.run(
        [sys.executable, str(PROFILE_RENDERER), "check", "--profile", "demo-pm"],
        env=renderer_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert renderer.returncode != 0
    assert "FATAL: timed out waiting for profile config lock" in renderer.stderr

    holder.kill()
    holder.communicate(timeout=3)
    assert holder.returncode is not None and holder.returncode < 0
    recovered = subprocess.run(
        _voice_command(paths),
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    profile_lock = paths["profile"].parent / ".demo-pm.config.lock"
    assert profile_lock.is_file()
    assert (
        subprocess.run(
            [shutil.which("flock") or "flock", "-n", str(profile_lock), "true"],
            check=False,
        ).returncode
        == 0
    )


def test_initial_delta_seed_uses_shared_lock_and_recovers_after_crash(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "home" / ".hermes" / "profiles" / "seed-pm"
    profile.mkdir(parents=True)
    barrier = tmp_path / "seed-holder"
    holder_env = _barrier_env(barrier)
    holder_env["PJANGLER_TEST_PROFILE_CONFIG_BARRIER_LABEL"] = "seed"
    holder = subprocess.Popen(
        [sys.executable, "-I", str(PROFILE_SEEDER), "--profile", str(profile)],
        env=holder_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for(Path(f"{barrier}.ready"), holder)
    assert Path(f"{barrier}.ready").read_text(encoding="utf-8") == "seed\n"
    assert not (profile / "config.delta.yaml").exists()

    blocked_env = os.environ.copy()
    blocked_env["HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS"] = "0.1"
    blocked = subprocess.run(
        [sys.executable, "-I", str(PROFILE_SEEDER), "--profile", str(profile)],
        env=blocked_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert blocked.returncode != 0
    assert "timed out waiting for profile config lock" in blocked.stderr
    assert not (profile / "config.delta.yaml").exists()

    holder.kill()
    holder.communicate(timeout=3)
    assert holder.returncode is not None and holder.returncode < 0
    seeded = subprocess.run(
        [sys.executable, "-I", str(PROFILE_SEEDER), "--profile", str(profile)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert seeded.returncode == 0, seeded.stderr
    assert seeded.stdout == "seeded\n"
    delta = profile / "config.delta.yaml"
    assert delta.read_text(encoding="utf-8").endswith("{}\n")
    assert delta.stat().st_mode & 0o777 == 0o600
    before = delta.read_bytes()

    converged = subprocess.run(
        [sys.executable, "-I", str(PROFILE_SEEDER), "--profile", str(profile)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert converged.returncode == 0, converged.stderr
    assert converged.stdout == "exists\n"
    assert delta.read_bytes() == before

    profile_step = (ROOT / "template" / ".scripts" / "10-hermes-profile.sh").read_text(
        encoding="utf-8"
    )
    assert "profile-config-seed.py" in profile_step
    assert 'cat > "$PROFILE_DELTA"' not in profile_step


def test_memory_pin_keeps_named_agent_bank_across_profile_change(tmp_path: Path) -> None:
    fleet = tmp_path / "home" / ".hermes"
    first = fleet / "profiles" / "infra-director"
    first.mkdir(parents=True)
    (fleet / "config.yaml").write_text("operator: {}\n", encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_FLEET_HOME"] = str(fleet)
    pinned = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROFILE_RENDERER),
            "memory-pin",
            "--profile",
            "infra-director",
            "--bank-id",
            "agent-grolf",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert pinned.returncode == 0, pinned.stdout + pinned.stderr
    assert yaml.safe_load(
        (first / "hindsight" / "config.json").read_text(encoding="utf-8")
    )["bank_id"] == "agent-grolf"

    moved = fleet / "profiles" / "cto"
    first.rename(moved)
    repinned = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROFILE_RENDERER),
            "memory-pin",
            "--profile",
            "cto",
            "--bank-id",
            "agent-grolf",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert repinned.returncode == 0, repinned.stdout + repinned.stderr
    assert yaml.safe_load(
        (moved / "hindsight" / "config.json").read_text(encoding="utf-8")
    )["bank_id"] == "agent-grolf"


def test_memory_pin_keeps_profile_template_as_legacy_default(tmp_path: Path) -> None:
    fleet = tmp_path / "home" / ".hermes"
    profile = fleet / "profiles" / "demo-pm"
    profile.mkdir(parents=True)
    (fleet / "config.yaml").write_text("operator: {}\n", encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_FLEET_HOME"] = str(fleet)
    result = subprocess.run(
        [sys.executable, "-I", str(PROFILE_RENDERER), "memory-pin", "--profile", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load(
        (profile / "hindsight" / "config.json").read_text(encoding="utf-8")
    )["bank_id"] == "agent-demo-pm"


def _memory_template_env(tmp_path: Path, template: str | None) -> tuple[Path, dict]:
    fleet = tmp_path / "home" / ".hermes"
    (fleet / "profiles").mkdir(parents=True)
    (fleet / "config.yaml").write_text("operator: {}\n", encoding="utf-8")
    config_toml = tmp_path / "template-config.toml"
    if template is not None:
        config_toml.write_text(
            f'[hindsight]\nagent_bank_template = "{template}"\n', encoding="utf-8"
        )
    env = os.environ.copy()
    env["HERMES_FLEET_HOME"] = str(fleet)
    env["HERMES_TEMPLATE_CONFIG"] = str(config_toml)
    return fleet, env


def _pinned_profile(fleet: Path, name: str, bank_id: str) -> Path:
    profile = fleet / "profiles" / name
    (profile / "hindsight").mkdir(parents=True)
    pin = profile / "hindsight" / "config.json"
    pin.write_text(json.dumps({"bank_id": bank_id}) + "\n", encoding="utf-8")
    return pin


def _manifest(tmp_path: Path, bank: dict) -> Path:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"version": "1", "bank": bank}), encoding="utf-8")
    return path


def _memory_template(env: dict, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(PROFILE_RENDERER), "memory-template", *extra],
        env=env, text=True, capture_output=True, check=False,
    )


def test_memory_template_records_configured_template_and_keeps_the_pin(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"retain_mission": "Remember who this agent is."})
    fleet, env = _memory_template_env(tmp_path, str(manifest))
    pin = _pinned_profile(fleet, "33god-pm", "agent-grolf")

    first = _memory_template(env, "--profile", "33god-pm")
    assert first.returncode == 0, first.stdout + first.stderr
    payload = json.loads(pin.read_text(encoding="utf-8"))
    assert payload == {"bank_id": "agent-grolf", "bank_template": str(manifest.resolve())}
    assert pin.stat().st_mode & 0o777 == 0o600

    before = pin.read_bytes()
    again = _memory_template(env, "--profile", "33god-pm")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "changed 0" in again.stdout
    assert pin.read_bytes() == before


def test_memory_template_expands_home_in_config(tmp_path: Path) -> None:
    home = tmp_path / "userhome"
    manifest_dir = home / "templates"
    manifest_dir.mkdir(parents=True)
    manifest = manifest_dir / "identity.json"
    manifest.write_text(json.dumps({"bank": {"reflect_mission": "x"}}), encoding="utf-8")
    fleet, env = _memory_template_env(tmp_path, "~/templates/identity.json")
    env["HOME"] = str(home)
    pin = _pinned_profile(fleet, "demo-pm", "agent-demo-pm")
    result = _memory_template(env, "--profile", "demo-pm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(pin.read_text(encoding="utf-8"))["bank_template"] == str(manifest.resolve())


def test_memory_template_unconfigured_is_a_noop(tmp_path: Path) -> None:
    fleet, env = _memory_template_env(tmp_path, None)
    pin = _pinned_profile(fleet, "demo-pm", "agent-demo-pm")
    before = pin.read_bytes()
    result = _memory_template(env, "--profile", "demo-pm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to record" in result.stdout
    assert pin.read_bytes() == before


def test_memory_template_refuses_a_manifest_without_a_mission(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"disposition_empathy": 2})
    fleet, env = _memory_template_env(tmp_path, str(manifest))
    pin = _pinned_profile(fleet, "demo-pm", "agent-demo-pm")
    before = pin.read_bytes()
    result = _memory_template(env, "--profile", "demo-pm")
    assert result.returncode != 0
    assert "sets no mission" in result.stderr
    assert pin.read_bytes() == before


def test_memory_template_refuses_a_missing_manifest(tmp_path: Path) -> None:
    fleet, env = _memory_template_env(tmp_path, str(tmp_path / "nope.json"))
    _pinned_profile(fleet, "demo-pm", "agent-demo-pm")
    result = _memory_template(env, "--profile", "demo-pm")
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_memory_template_explicit_empty_removes_it(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"retain_mission": "x"})
    fleet, env = _memory_template_env(tmp_path, str(manifest))
    pin = _pinned_profile(fleet, "demo-pm", "agent-demo-pm")
    assert _memory_template(env, "--profile", "demo-pm").returncode == 0
    removed = _memory_template(env, "--profile", "demo-pm", "--bank-template", "")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert json.loads(pin.read_text(encoding="utf-8")) == {"bank_id": "agent-demo-pm"}


def test_memory_template_skips_a_desk_without_a_pin(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"retain_mission": "x"})
    fleet, env = _memory_template_env(tmp_path, str(manifest))
    (fleet / "profiles" / "fresh-pm").mkdir()
    result = _memory_template(env, "--profile", "fresh-pm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run memory-pin first" in result.stdout
    assert not (fleet / "profiles" / "fresh-pm" / "hindsight" / "config.json").exists()


def test_hire_step_records_the_identity_bank_template() -> None:
    profile_step = (ROOT / "template" / ".scripts" / "10-hermes-profile.sh").read_text(
        encoding="utf-8"
    )
    assert 'memory-template --profile "$PROFILE_NAME"' in profile_step
    example = (ROOT / "template" / ".scripts" / "config.example.toml").read_text(encoding="utf-8")
    assert "[hindsight]" in example and "agent_bank_template" in example
