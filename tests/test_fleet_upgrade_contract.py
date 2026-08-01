from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
FLEET_SYNC = ROOT / "scripts" / "fleet-sync.sh"


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    role = tmp_path / "role"
    runtime = role / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "profile.yaml").write_text(
        "config:\n  inherit_from: default\n  save_mode: delta\n", encoding="utf-8"
    )
    (role / "role.yaml").write_text("profile: demo-pm\n", encoding="utf-8")
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "demo-pm": {
                        "role_dir": str(role),
                        "profile_name": "demo-pm",
                        "systemd": {
                            "gateway_unit": "hermes-demo-pm-gateway.service",
                            "consumer_unit": "hermes-demo-pm-consumer.service",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    consumer = unit_dir / "hermes-demo-pm-consumer.service"
    consumer.write_text("legacy\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    retired = tmp_path / "retired"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"disable --now hermes-demo-pm-consumer.service"*) touch "$SYSTEMCTL_RETIRED"; exit 0 ;;
  *"is-active --quiet hermes-demo-pm-consumer.service"*|*"is-enabled --quiet hermes-demo-pm-consumer.service"*)
    [[ -f "$SYSTEMCTL_RETIRED" ]] && exit 1 || exit 0 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HERMES_FLEET_REGISTRY_FILE": str(registry),
            "HERMES_FLEET_HOME": str(home / ".hermes"),
            "SYSTEMCTL_RETIRED": str(retired),
        }
    )
    return env, registry, consumer, role


def test_fleet_audit_reports_and_apply_retires_legacy_consumer(tmp_path: Path) -> None:
    env, registry, consumer, _ = _fixture(tmp_path)

    audit = subprocess.run(
        ["bash", str(FLEET_SYNC), "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert audit.returncode == 1
    assert "legacy per-profile Bloodbank consumer remains" in audit.stdout

    applied = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert not consumer.exists()
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert "consumer_unit" not in entry["systemd"]
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600


def test_pinned_fork_publication_replaces_upstream_clean_install_path() -> None:
    installer = (ROOT / "install-local.sh").read_text(encoding="utf-8")
    config = (ROOT / "template" / ".scripts" / "config.example.toml").read_text(
        encoding="utf-8"
    )
    expected_sha = "113e1b182b6d72a7dd02a191f134a41668ceaf0e"

    assert "raw.githubusercontent.com/NousResearch/hermes-agent" not in installer
    assert 'HERMES_RUNTIME_GIT_URL="https://github.com/delorenj/hermes-agent.git"' in installer
    assert 'HERMES_RUNTIME_GIT_REF="feature/PJAN-19-routing-publication"' in installer
    assert f'HERMES_RUNTIME_GIT_SHA="{expected_sha}"' in installer
    assert "merge-base --is-ancestor" in installer
    assert f'hermes_git_sha = "{expected_sha}"' in config
