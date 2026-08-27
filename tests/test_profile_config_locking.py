from __future__ import annotations

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
