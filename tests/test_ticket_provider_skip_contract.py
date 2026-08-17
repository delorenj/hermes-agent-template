from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SCRIPTS = ROOT / "template" / ".scripts"


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TEMPLATE_SCRIPTS / "42-ticket-provider.sh", scripts)
    shutil.copy2(TEMPLATE_SCRIPTS / "_lib.sh", scripts)
    shutil.copytree(TEMPLATE_SCRIPTS / "lib", scripts / "lib")
    shutil.copytree(TEMPLATE_SCRIPTS / "providers", scripts / "providers")

    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
telegram:
  bot_username: demo_pm_bot
plane:
  workspace: test-space
runtime:
  github_owner: test
  github_repo: agent-hm-demo-pm
ticket_provider:
  name: plane
  board_id: ""
""",
        encoding="utf-8",
    )
    (project / ".project.json").write_text(
        json.dumps(
            {
                "project_name": "Demo",
                "project_slug": "demo",
                "repo_path": str(project),
                "ticket_provider": {
                    "type": "plane",
                    "workspace": "test-space",
                    "identifier": "DEMO",
                    "board_id": "",
                    "state": "planned",
                },
                "agents": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "provider-calls.log"
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROVIDER_CALL_LOG"
case "$*" in
  *'/projects/?per_page=200'*) printf '%s\n' '[{"id":"granted-board","name":"Demo"}]' ;;
  *) printf '%s\n' '{}' ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PLANE_", "TRELLO_", "LINEAR_"))
        and key not in {"SKIP_PLANE", "TICKET_PROVIDER"}
    }
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HERMES_FLEET_ENV": str(home / ".hermes" / "missing-fleet.env"),
            "HERMES_TEMPLATE_CONFIG": str(home / ".config" / "missing-config.toml"),
            "PROVIDER_CALL_LOG": str(call_log),
            # Deliberately ambient: SKIP_PLANE must dominate available credentials.
            "PLANE_API_KEY": "ambient-plane-test-key",
            "TRELLO_KEY": "ambient-trello-test-key",
            "TRELLO_TOKEN": "ambient-trello-test-token",
        }
    )
    return project, role, env, call_log


def _run(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(role / ".scripts" / "42-ticket-provider.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "caller_state",
    [
        {"SKIP_PLANE": "1"},
        {"SKIP_PLANE": "1", "MCP_LIVE": "1"},
    ],
    ids=["live-false-no-board-grant", "live-true-skip-plane"],
)
def test_skip_plane_exits_before_provider_or_binding_effects(
    tmp_path: Path, caller_state: dict[str, str]
) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    env.update(caller_state)
    project_before = (project / ".project.json").read_bytes()
    role_before = (role / "role.yaml").read_bytes()

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not call_log.exists(), "SKIP_PLANE must prevent every provider/curl call"
    assert (project / ".project.json").read_bytes() == project_before
    assert (role / "role.yaml").read_bytes() == role_before
    assert not (role / ".scripts" / ".done-42-ticket-provider").exists()
    assert not (role / ".scripts" / ".provision.log").exists()


def test_explicit_board_grant_reaches_real_provider_adapter(tmp_path: Path) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    env["SKIP_PLANE"] = "0"

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").strip(), "positive grant must reach fake curl"
    manifest = json.loads((project / ".project.json").read_text(encoding="utf-8"))
    assert manifest["ticket_provider"]["board_id"] == "granted-board"
    assert manifest["agents"]["demo-pm"] == {
        "role": "pm",
        "role_dir": "agents/hermes/pm",
        "provisioning_state": "provisioned",
    }
    assert (role / ".scripts" / ".done-42-ticket-provider").exists()


@pytest.mark.parametrize(
    "script",
    ["01-config.sh", "05-fleet-env.sh", "10-hermes-profile.sh", "80-registry.sh"],
)
def test_host_state_guard_exits_before_library_or_host_writes(tmp_path: Path, script: str) -> None:
    role = tmp_path / "project" / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TEMPLATE_SCRIPTS / script, scripts / script)
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".config" / "hermes-agent-template" / "config.toml"
    registry = home / ".hermes" / "agents-registry.yaml"
    env = {
        **os.environ,
        "HOME": str(home),
        "SKIP_HOST_STATE": "1",
        "HERMES_TEMPLATE_CONFIG": str(config),
        "HERMES_FLEET_REGISTRY_FILE": str(registry),
    }

    result = subprocess.run(
        ["bash", str(scripts / script)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not config.exists()
    assert not registry.exists()
    assert not (home / ".hermes" / "profiles").exists()
    assert not (scripts / ".provision.log").exists()


def test_explicit_project_root_cannot_climb_into_enclosing_checkout(tmp_path: Path) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    shutil.rmtree(project / ".git")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    outer_manifest = tmp_path / ".project.json"
    outer_before = b'{"agents": {}}\n'
    outer_manifest.write_bytes(outer_before)
    env.update({"SKIP_PLANE": "0", "PJANGLER_PROJECT_ROOT": str(project)})

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.exists(), "the granted provider still runs inside the explicit target"
    assert outer_manifest.read_bytes() == outer_before
    manifest = json.loads((project / ".project.json").read_text(encoding="utf-8"))
    assert manifest["agents"]["demo-pm"]["role_dir"] == "agents/hermes/pm"
