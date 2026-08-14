from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
LAUNCHER_TEMPLATE = ROOT / "template" / "hermes.jinja"


def test_launcher_drops_shared_identity_credentials_but_keeps_policy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    role = tmp_path / "role"
    runtime = role / "runtime"
    runtime.mkdir(parents=True)
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    # Singleton-runtime contract: the launcher resolves HERMES_HOME to the named
    # profile dir and refuses to start without it, so the fixture must provision
    # one the same way a real agent has one.
    profile = home / ".hermes" / "profiles" / "demo-pm"
    profile.mkdir(parents=True)
    fake_hermes = tmp_path / "fake-hermes"
    fake_hermes.write_text(
        """#!/usr/bin/env python3
import json
import os

keys = (
    "TELEGRAM_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "HERMES_HOME",
)
print(json.dumps({key: os.environ.get(key) for key in keys}))
""",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    fleet.write_text(
        f"export HERMES_FLEET_BIN={fake_hermes}\n"
        "export TELEGRAM_BOT_TOKEN=123456:shared-secret\n"
        "export SLACK_BOT_TOKEN=xoxb-shared-secret\n"
        "export SLACK_APP_TOKEN=xapp-shared-secret\n"
        "export TELEGRAM_ALLOWED_USERS=111\n"
        "export SLACK_ALLOWED_USERS=U111\n",
        encoding="utf-8",
    )
    launcher = role / "hermes"
    launcher.write_text(
        LAUNCHER_TEMPLATE.read_text(encoding="utf-8").replace("{{ agent_id }}", "demo-pm"),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "TELEGRAM_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "TELEGRAM_ALLOWED_USERS",
            "SLACK_ALLOWED_USERS",
            "HERMES_BIN",
            "HERMES_FLEET_BIN",
        }
    }
    env.update({"HOME": str(home), "HERMES_FLEET_ENV": str(fleet)})

    result = subprocess.run(
        [str(launcher), "gateway", "run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["TELEGRAM_BOT_TOKEN"] is None
    assert observed["SLACK_BOT_TOKEN"] is None
    assert observed["SLACK_APP_TOKEN"] is None
    assert observed["TELEGRAM_ALLOWED_USERS"] == "111"
    assert observed["SLACK_ALLOWED_USERS"] == "U111"
    # Singleton-runtime contract: HERMES_HOME is the NAMED PROFILE dir, never the
    # raw runtime path. Hermes derives profile identity and shared fleet auth
    # from the unresolved HERMES_HOME, so pointing it at the runtime makes
    # get_active_profile_name() report "default" and disables shared auth.
    assert observed["HERMES_HOME"] == str(profile)
