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
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    for name in ("_lib.sh", "credential-launch.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
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
}))
""",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
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
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "gateway"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "runtime-secret" not in result.stdout + result.stderr
    observed = json.loads(Path(env["CAPTURE_PATH"]).read_text(encoding="utf-8"))
    assert observed["telegram"] == "telegram-runtime-secret"
    assert observed["model_key"] == "model-runtime-secret"
    assert observed["home"].endswith("/.hermes/profiles/demo-director")
    assert observed["argv"] == [
        "gateway",
        "run",
        "--replace",
        "--model",
        "hermes",
        "--provider",
        "custom",
        "--base-url",
        "https://gateway.example.test/v1",
        "--api-mode",
        "chat_completions",
        "--key-env",
        "DIRECTOR_LITELLM_KEY",
    ]


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
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "gateway"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid credential environment name" in result.stderr


def test_launcher_keeps_ignored_environment_file_fallback(tmp_path: Path) -> None:
    role, env = _role(tmp_path)
    env["TELEGRAM_BOT_TOKEN"] = "runtime-env-telegram"
    env["DIRECTOR_LITELLM_KEY"] = "runtime-env-model"

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "credential-launch.sh"), "gateway"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(Path(env["CAPTURE_PATH"]).read_text(encoding="utf-8"))
    assert observed["telegram"] == "runtime-env-telegram"
    assert observed["model_key"] == "runtime-env-model"
