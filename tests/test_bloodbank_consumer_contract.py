from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "template" / ".scripts"


def _make_role(tmp_path: Path) -> tuple[Path, Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    for name in ("_lib.sh", "60-bloodbank.sh", "70-systemd.sh", "80-registry.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "heartbeat.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "checkpoint.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
telegram:
  bot_username: "demo_pm_bot"
slack:
  provisioning_status: "deferred"
  team_id: ""
  team_name: ""
  bot_user_id: ""
  bot_id: ""
  bot_username: ""
bloodbank:
  gateway_scope: fleet
  target_agent_id: "demo-pm"
  producer: "hermes-agent:demo-pm"
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
    return role, registry


def _environment(tmp_path: Path, registry: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )
    return env


def _run(role: Path, name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(role / ".scripts" / name)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_future_runtime_scaffolds_have_no_profile_consumer_or_inbox() -> None:
    for scaffold in (ROOT / "runtime-scaffold", ROOT / "template" / ".runtime-scaffold"):
        assert not (scaffold / "bloodbank-consumer.py").exists()
        assert "bloodbank-inbox" not in (scaffold / ".gitignore").read_text(encoding="utf-8")
        readme = (scaffold / "README.md").read_text(encoding="utf-8")
        assert "fleet-shared" in readme
        assert "no consumer process or inbox bridge" in readme


@pytest.mark.parametrize("skip", ["0", "1"])
def test_step_60_is_a_harmless_compatibility_noop(tmp_path: Path, skip: str) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    env["SKIP_BLOODBANK"] = skip

    result = _run(role, "60-bloodbank.sh", env)

    assert result.returncode == 0, result.stderr
    assert (role / ".scripts" / ".done-60-bloodbank").is_file()
    assert not (role / "runtime" / "bloodbank-consumer.py").exists()
    assert "NATS" not in result.stdout + result.stderr


def test_systemd_installs_only_profile_gateway_and_heartbeat(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    assert (unit_dir / "hermes-demo-pm-gateway.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.timer").is_file()
    assert not (unit_dir / "hermes-demo-pm-consumer.service").exists()
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in unit_dir.iterdir())
    assert "bloodbank-consumer.py" not in rendered
    assert "consumer.log" not in rendered


def test_registry_records_fleet_gateway_contract_without_consumer_unit(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "demo-pm": {
                        "systemd": {
                            "consumer_unit": "hermes-demo-pm-consumer.service"
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(role, "80-registry.sh", env)

    assert result.returncode == 0, result.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert entry["bloodbank"] == {
        "gateway_scope": "fleet",
        "target_agent_id": "demo-pm",
    }
    assert entry["systemd"] == {
        "gateway_unit": "hermes-demo-pm-gateway.service",
        "heartbeat_timer": "hermes-demo-pm-heartbeat.timer",
    }
    assert "consumer_unit" not in entry["systemd"]


def test_template_declares_fleet_scope_and_retains_compatibility_step() -> None:
    role = (ROOT / "template" / "role.yaml.jinja").read_text(encoding="utf-8")
    copier = (ROOT / "copier.yml").read_text(encoding="utf-8")
    step = (SCRIPTS / "60-bloodbank.sh").read_text(encoding="utf-8")

    assert "gateway_scope: fleet" in role
    assert 'target_agent_id: "{{ agent_id }}"' in role
    assert './.scripts/60-bloodbank.sh' in copier
    assert "SKIP_BLOODBANK accepted as a compatibility no-op" in step
    for legacy in ("/dev/tcp", "uv pip install", "bloodbank-consumer.py"):
        assert legacy not in step
