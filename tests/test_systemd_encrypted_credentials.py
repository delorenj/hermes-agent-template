from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "template" / ".scripts"


def _role(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    role = tmp_path / "project" / "agents" / "hermes" / "director"
    project = tmp_path / "project"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    for name in ("_lib.sh", "credential-launch.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    # credential-launch.sh reads role.yaml through the shared block-scoped
    # walker in lib/role-yaml.py.
    shutil.copytree(SCRIPTS / "lib", scripts / "lib")
    (role / "role.yaml").write_text(
        """repo: demo
role: director
agent_id: demo-director
display_name: "Demo Director"
profile: demo-director
model:
  provider: "custom"
  name: "hermes"
  base_url: "https://gateway.example.test/v1"
  api_mode: "chat_completions"
  key_env: "DIRECTOR_LITELLM_KEY"
telegram:
  bot_username: demo_director_bot
plane:
  workspace: test
runtime:
  github_owner: ""
  github_repo: legacy
""",
        encoding="utf-8",
    )
    (home / ".hermes" / "profiles" / "demo-director").mkdir(parents=True)
    capture = tmp_path / "capture.json"
    fake_hermes = tmp_path / "hermes"
    fake_hermes.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["CAPTURE_PATH"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "home": os.environ.get("HERMES_HOME"),
    "telegram": os.environ.get("TELEGRAM_BOT_TOKEN"),
    "model_key": os.environ.get("DIRECTOR_LITELLM_KEY"),
    "terminal_cwd": os.environ.get("TERMINAL_CWD"),
}))
""",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    # Heartbeat mode execs the role's heartbeat.sh; stand in a capture script with
    # the same observation surface (gateway mode is retired and never execs).
    fake_heartbeat = scripts / "heartbeat.sh"
    fake_heartbeat.write_text(fake_hermes.read_text(encoding="utf-8"), encoding="utf-8")
    fake_heartbeat.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_BIN": str(fake_hermes),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "CAPTURE_PATH": str(capture),
        }
    )
    return role, env


def test_launcher_reads_volatile_credentials_and_forwards_only_key_name(
    tmp_path: Path,
) -> None:
    role, env = _role(tmp_path)
    credentials = tmp_path / "run-credentials"
    credentials.mkdir()
    (credentials / "telegram_bot_token").write_text(
        "telegram-runtime-secret", encoding="utf-8"
    )
    (credentials / "model_api_key").write_text(
        "model-runtime-secret", encoding="utf-8"
    )
    env["CREDENTIALS_DIRECTORY"] = str(credentials)

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "heartbeat"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "runtime-secret" not in result.stdout + result.stderr
    observed = json.loads(Path(env["CAPTURE_PATH"]).read_text(encoding="utf-8"))
    assert observed["telegram"] is None
    assert observed["model_key"] == "model-runtime-secret"
    assert observed["home"].endswith("/.hermes/profiles/demo-director")
    assert observed["terminal_cwd"] == str(role.parents[2])
    assert observed["argv"] == []


def test_launcher_gateway_mode_is_retired_and_refuses_to_take_over_the_host(
    tmp_path: Path,
) -> None:
    """A per-agent unit pointing at `credential-launch.sh gateway` would
    `gateway run --replace` the one host gateway (2026-10-20, 2026-11-02)."""
    role, env = _role(tmp_path)

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "gateway"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "gateway mode is retired" in result.stderr
    assert "hermes-gateway.service" in result.stderr
    assert "hermes-gateway-restart" in result.stderr
    assert not Path(env["CAPTURE_PATH"]).exists(), "refusal must not exec anything"


def test_launcher_rejects_key_values_in_role_manifest(tmp_path: Path) -> None:
    role, env = _role(tmp_path)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            'key_env: "DIRECTOR_LITELLM_KEY"', 'key_env: "not-a-variable-name"'
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "heartbeat"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid credential environment name" in result.stderr


def test_launcher_scrubs_channel_env_but_preserves_model_env_fallback(tmp_path: Path) -> None:
    role, env = _role(tmp_path)
    env["TELEGRAM_BOT_TOKEN"] = "runtime-env-telegram"
    env["DIRECTOR_LITELLM_KEY"] = "runtime-env-model"

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "heartbeat"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(Path(env["CAPTURE_PATH"]).read_text(encoding="utf-8"))
    assert observed["telegram"] is None
    assert observed["model_key"] == "runtime-env-model"


def test_unit_entrypoints_ship_executable() -> None:
    # Older templates wrote `ExecStart=<role>/.scripts/credential-launch.sh` and
    # systemd exec'd it directly (a 0644 copy failed with status=203/EXEC); it is
    # still the manual `heartbeat` entrypoint, so it ships executable.
    # `flume remediate` copies a template file's executable bit, so the bit
    # has to be on the template file itself -- chmod at provisioning time does
    # not survive the next scaffold refresh.
    for name in ("credential-launch.sh", "heartbeat.sh"):
        path = SCRIPTS / name
        assert os.access(path, os.X_OK), f"{name} must be executable"
        mode = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-s", f"template/.scripts/{name}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split()[0]
        assert mode == "100755", f"{name} is tracked as {mode}"
