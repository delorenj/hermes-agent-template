from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _copy(target: Path, *, overwrite: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "copier",
        "copy",
        "--skip-tasks",
        "--defaults",
        "--trust",
        "-d",
        "target_repo=demo",
    ]
    if overwrite:
        command.append("--overwrite")
    command.extend([str(ROOT), str(target)])
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_copier_render_contains_no_python_cache_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "rendered"

    rendered = _copy(target)

    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    offenders = [
        str(path.relative_to(target))
        for path in target.rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    ]
    assert offenders == []


def test_forced_rerender_preserves_durable_state_and_refreshes_assets(
    tmp_path: Path,
) -> None:
    target = tmp_path / "rendered"
    first = _copy(target)
    assert first.returncode == 0, first.stdout + first.stderr

    role = target / "role.yaml"
    role.write_text(
        role.read_text(encoding="utf-8")
        .replace("enabled: true", "enabled: false", 1)
        .replace('provisioning_status: "deferred"', 'provisioning_status: "verified"', 1),
        encoding="utf-8",
    )
    role_before = role.read_bytes()
    runtime_sentinel = target / "runtime" / "private-state"
    runtime_sentinel.parent.mkdir()
    runtime_sentinel.write_text("preserve\n", encoding="utf-8")
    (target / "hermes").write_text("stale launcher\n", encoding="utf-8")

    rerendered = _copy(target, overwrite=True)

    assert rerendered.returncode == 0, rerendered.stdout + rerendered.stderr
    assert role.read_bytes() == role_before
    assert runtime_sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert (target / "hermes").read_text(encoding="utf-8") != "stale launcher\n"
    assert (target / ".scripts" / "store-onepassword-secret.py").is_file()


def test_missing_required_skillex_projection_fails_before_profile_mutation(
    tmp_path: Path,
) -> None:
    role = tmp_path / "project" / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "template" / ".scripts" / "10-hermes-profile.sh", scripts)
    shutil.copy2(ROOT / "template" / ".scripts" / "_lib.sh", scripts)
    shutil.copytree(ROOT / "template" / ".scripts" / "lib", scripts / "lib")
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: Demo PM
profile: demo-pm
telegram:
  bot_username: demo_pm_bot
plane:
  workspace: test
runtime:
  github_repo: demo-runtime
""",
        encoding="utf-8",
    )
    marker = scripts / ".done-10-hermes-profile"
    marker.touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes_log = tmp_path / "hermes.log"
    hermes = fake_bin / "hermes"
    hermes.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$HERMES_LOG"\n',
        encoding="utf-8",
    )
    hermes.chmod(0o755)
    home = tmp_path / "home"
    missing = tmp_path / "missing-skills"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_BIN": str(hermes),
            "HERMES_LOG": str(hermes_log),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "CANONICAL_SKILLS_DIR": str(missing),
        }
    )

    result = subprocess.run(
        ["bash", str(scripts / "10-hermes-profile.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    for name in (
        "delonet-conventions",
        "delonet-dotenv",
        "hermes-pm-template-maintenance",
        "hindsight",
        "subagent-driven-development",
    ):
        assert name in result.stderr
    assert not marker.exists()
    assert not hermes_log.exists()
    assert not (home / ".hermes" / "profiles" / "demo-pm").exists()


def test_reconciliation_default_and_heartbeat_guidance_agree() -> None:
    copier = (ROOT / "copier.yml").read_text(encoding="utf-8")
    role = (ROOT / "template" / "role.yaml.jinja").read_text(encoding="utf-8")
    heartbeat = (ROOT / "template" / ".scripts" / "heartbeat.sh").read_text(
        encoding="utf-8"
    )

    assert "reconcile_enabled:\n  type: bool" in copier
    assert "reconcile_enabled | tojson" in role
    assert "explicit_opt_out:" in role
    assert "Deployed PMs default on" in heartbeat
    assert "Default off" not in heartbeat


def test_summary_reports_operational_heartbeat_and_deferred_gateway(
    tmp_path: Path,
) -> None:
    target = tmp_path / "rendered"
    rendered = _copy(target)
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    role = target / "role.yaml"
    role.write_text(
        role.read_text(encoding="utf-8")
        .replace('gateway: "pending"', 'gateway: "deferred"')
        .replace('heartbeat: "pending"', 'heartbeat: "active"'),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "SKIP_PLANE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(target / ".scripts" / "99-summary.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Mode:           OPERATIONAL_WITH_GATEWAY_DEFERRED" in result.stderr
    assert "Heartbeat:      active" in result.stderr
    assert "Gateway:        deferred" in result.stderr
    assert "do not start hermes-demo-pm-gateway.service manually" in result.stderr
    assert "Provisioned:" not in result.stderr
