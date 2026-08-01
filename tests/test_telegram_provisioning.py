from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
TELEGRAM_SCRIPT = ROOT / "template" / ".scripts" / "30-telegram.sh"
LIB_SCRIPT = ROOT / "template" / ".scripts" / "_lib.sh"
REGISTRY_SCRIPT = ROOT / "template" / ".scripts" / "80-registry.sh"

BOT_TOKEN = "123456:profile-only-secret"
OTHER_TOKEN = "654321:different-profile-secret"


def _make_role(tmp_path: Path) -> tuple[Path, Path, Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    shutil.copy2(TELEGRAM_SCRIPT, scripts / TELEGRAM_SCRIPT.name)
    shutil.copy2(LIB_SCRIPT, scripts / LIB_SCRIPT.name)
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
telegram:
  provisioning_status: "deferred"
  bot_username: "demo_pm_bot"
  bot_id: ""
slack:
  provisioning_status: "deferred"
  team_id: ""
  team_name: ""
  bot_user_id: ""
  bot_id: ""
  bot_username: ""
bloodbank:
  gateway_scope: fleet
  target_agent_id: demo-pm
plane:
  workspace: "test"
  identifier: ""
runtime:
  github_owner: "test"
  github_repo: "agent-hm-demo-pm"
""",
        encoding="utf-8",
    )
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    return role, runtime, registry


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

url = sys.argv[-1]
expected = os.environ.get("EXPECTED_TELEGRAM_TOKEN", "")
assert expected and url == f"https://api.telegram.org/bot{expected}/getMe"
print(json.dumps({"ok": True, "result": {"id": 424242, "username": "verified_demo_bot"}}))
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    hermes = bindir / "hermes"
    hermes.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)
    return bindir


def _run(
    role: Path,
    registry: Path,
    home: Path,
    bindir: Path,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELEGRAM_") and key != "SKIP_TELEGRAM"
    }
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bindir}:{env['PATH']}",
            "HERMES_BIN": str(bindir / "hermes"),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )
    env.update(overrides or {})
    return subprocess.run(
        ["bash", str(role / ".scripts" / "30-telegram.sh")],
        env=env,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )


def test_telegram_ignores_shared_fleet_token_and_defers_noninteractive(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_USERS=111\n",
        encoding="utf-8",
    )

    result = _run(role, registry, home, _fake_bin(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "deferred" in result.stderr
    assert not (runtime / ".env").exists()
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert BOT_TOKEN in fleet.read_text(encoding="utf-8")


def test_explicit_token_writes_only_private_runtime_env_and_identity(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("TELEGRAM_ALLOWED_USERS=111,222\n", encoding="utf-8")
    shared = home / ".hermes" / ".env"
    shared.write_text("PROVIDER_KEY=keep-me\n", encoding="utf-8")

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "EXPECTED_TELEGRAM_TOKEN": BOT_TOKEN},
    )

    assert result.returncode == 0, result.stderr
    env_file = runtime / ".env"
    env_text = env_file.read_text(encoding="utf-8")
    assert f'TELEGRAM_BOT_TOKEN="{BOT_TOKEN}"' in env_text
    assert 'TELEGRAM_ALLOWED_USERS="111,222"' in env_text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"
    assert fleet.read_text(encoding="utf-8") == "TELEGRAM_ALLOWED_USERS=111,222\n"
    assert BOT_TOKEN not in result.stdout + result.stderr

    telegram = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]
    assert telegram == {
        "provisioning_status": "verified",
        "bot_username": "verified_demo_bot",
        "bot_id": "424242",
    }


def test_rejects_token_parked_in_shared_fleet_env(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n", encoding="utf-8")

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
            "EXPECTED_TELEGRAM_TOKEN": BOT_TOKEN,
        },
    )

    assert result.returncode != 0
    assert "already assigned to shared fleet environment" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_rejects_token_reused_by_another_profile(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    other_role = tmp_path / "other-role"
    other_runtime = other_role / "runtime"
    other_runtime.mkdir(parents=True)
    (other_runtime / ".env").write_text(
        f'TELEGRAM_BOT_TOKEN="{BOT_TOKEN}"\n', encoding="utf-8"
    )
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "other-pm": {
                        "role_dir": str(other_role),
                        "telegram": {"bot_id": "999999"},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
            "EXPECTED_TELEGRAM_TOKEN": BOT_TOKEN,
        },
    )

    assert result.returncode != 0
    assert "already assigned to agent other-pm" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_rejects_verified_bot_identity_owned_by_another_agent(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "other-pm": {
                        "telegram": {
                            "provisioning_status": "verified",
                            "bot_username": "verified_demo_bot",
                            "bot_id": "424242",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": OTHER_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
            "EXPECTED_TELEGRAM_TOKEN": OTHER_TOKEN,
        },
    )

    assert result.returncode != 0
    assert "bot identity is already assigned to agent other-pm" in result.stderr
    assert OTHER_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_refuses_runtime_env_symlink_before_get_me(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    shared = home / ".hermes" / ".env"
    shared.parent.mkdir(parents=True)
    shared.write_text("PROVIDER_KEY=keep-me\n", encoding="utf-8")
    (runtime / ".env").symlink_to(shared)

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
            "EXPECTED_TELEGRAM_TOKEN": BOT_TOKEN,
        },
    )

    assert result.returncode != 0
    assert "refusing to write Telegram credentials through symlink" in result.stderr
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"


def test_registry_persists_telegram_identity_without_token(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy2(REGISTRY_SCRIPT, role / ".scripts" / REGISTRY_SCRIPT.name)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8")
        .replace('provisioning_status: "deferred"', 'provisioning_status: "verified"', 1)
        .replace('bot_username: "demo_pm_bot"', 'bot_username: "verified_demo_bot"')
        .replace('bot_id: ""', 'bot_id: "424242"', 1),
        encoding="utf-8",
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")
    }
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "80-registry.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    serialized = registry.read_text(encoding="utf-8")
    entry = yaml.safe_load(serialized)["agents"]["demo-pm"]
    assert entry["telegram"] == {
        "provisioning_status": "verified",
        "bot_username": "verified_demo_bot",
        "bot_id": "424242",
    }
    assert "TELEGRAM_BOT_TOKEN" not in serialized
