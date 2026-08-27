from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
FLEET_SYNC = ROOT / "scripts" / "fleet-sync.sh"
HEARTBEAT = ROOT / "template" / ".scripts" / "heartbeat.sh"


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    fleet_home = home / ".hermes"
    profile = fleet_home / "profiles" / "demo-pm"
    profile.mkdir(parents=True)
    (fleet_home / "config.yaml").write_text("fleet: true\n", encoding="utf-8")
    (profile / "config.delta.yaml").write_text("{}\n", encoding="utf-8")
    (profile / "config.yaml").write_text("fleet: true\n", encoding="utf-8")
    role = tmp_path / "role"
    runtime = role / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "profile.yaml").write_text(
        "config:\n  inherit_from: default\n  save_mode: delta\n", encoding="utf-8"
    )
    (role / "role.yaml").write_text("profile: demo-pm\n", encoding="utf-8")
    (role / ".scripts").mkdir()
    (role / ".scripts" / "heartbeat.sh").write_text(
        "#!/bin/sh\n# legacy fleet-bypassing heartbeat\n",
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
  *"disable --now hermes-demo-pm-consumer.service"*)
    [[ "$SYSTEMCTL_MODE" == "disable-fail" ]] && exit 1
    touch "$SYSTEMCTL_RETIRED"; exit 0 ;;
  *"is-active hermes-demo-pm-consumer.service"*)
    [[ "$SYSTEMCTL_MODE" == "active-query-error" ]] && { echo "Failed to connect to bus" >&2; exit 1; }
    if [[ ! -e "$SYSTEMCTL_CONSUMER" ]]; then echo inactive; exit 4;
    elif [[ -f "$SYSTEMCTL_RETIRED" ]]; then echo inactive; exit 3; else echo active; exit 0; fi ;;
  *"is-enabled hermes-demo-pm-consumer.service"*)
    [[ "$SYSTEMCTL_MODE" == "enabled-query-error" ]] && { echo "Failed to connect to bus" >&2; exit 1; }
    if [[ ! -e "$SYSTEMCTL_CONSUMER" ]]; then echo not-found; exit 4;
    elif [[ -f "$SYSTEMCTL_RETIRED" ]]; then echo disabled; exit 1; else echo enabled; exit 0; fi ;;
  *"daemon-reload"*) exit 0 ;;
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
            "SYSTEMCTL_CONSUMER": str(consumer),
            "SYSTEMCTL_MODE": "success",
        }
    )
    return env, registry, consumer, role


def test_fleet_audit_reports_and_apply_retires_legacy_consumer(tmp_path: Path) -> None:
    env, registry, consumer, role = _fixture(tmp_path)

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
    assert (role / ".scripts" / "heartbeat.sh").read_bytes() == HEARTBEAT.read_bytes()

    converged = subprocess.run(
        ["bash", str(FLEET_SYNC), "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert converged.returncode == 0, converged.stdout + converged.stderr
    assert "config.yaml" not in converged.stdout
    profile = Path(env["HERMES_FLEET_HOME"]) / "profiles" / "demo-pm"
    assert not (profile / "config.yaml").is_symlink()


def test_fleet_vox_delta_uses_list_patch_and_preserves_base_flow_and_exclusions(
    tmp_path: Path,
) -> None:
    env, registry, _consumer, role = _fixture(tmp_path)
    pm_role = tmp_path / "pm"
    role.rename(pm_role)
    registry_data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    registry_data["agents"]["demo-pm"]["role_dir"] = str(pm_role)
    registry.write_text(yaml.safe_dump(registry_data, sort_keys=False), encoding="utf-8")
    fleet_home = Path(env["HERMES_FLEET_HOME"])
    base = fleet_home / "config.yaml"
    base.write_text(
        "plugins:\n  enabled:\n    - fleet/core-one\n    - tts/voxxy\n"
        # All three old substring probes matched this invalid config: vox in
        # voxxy, tts/vox in tts/voxxy, and the already-correct voice. Parsed
        # exact checks must still reconcile the provider and plugin key.
        "tts:\n  provider: voxxy\n  voice: carlin\n",
        encoding="utf-8",
    )
    profile = fleet_home / "profiles" / "demo-pm"
    delta_path = profile / "config.delta.yaml"
    delta_path.write_text("# operator comment survives\n{}\n", encoding="utf-8")
    plugin = tmp_path / "vox-plugin"
    plugin.mkdir()
    env["VOX_PLUGIN_DIR"] = str(plugin)

    first = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0, first.stdout + first.stderr
    delta = yaml.safe_load(delta_path.read_text(encoding="utf-8"))
    assert "plugins" not in delta
    assert delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"] == {
        "add": ["tts/vox"],
        "remove": ["tts/voxxy"],
    }
    assert "# operator comment survives" in delta_path.read_text(encoding="utf-8")
    generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert generated["plugins"]["enabled"] == ["fleet/core-one", "tts/vox"]
    assert "x-pjangler-merge" not in generated

    base.write_text(
        "plugins:\n  enabled:\n    - fleet/core-two\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: newer\n",
        encoding="utf-8",
    )
    changed_base = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert changed_base.returncode == 0, changed_base.stdout + changed_base.stderr
    generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert generated["plugins"]["enabled"] == ["fleet/core-two", "tts/vox"]

    # An explicit list replacement is operator intent.  Fleet reconciliation
    # may append its role-owned plugin through the directive, but must not copy
    # excluded fleet entries back into the replacement list.
    delta_path.write_text(
        "# explicit operator exclusion\nplugins:\n  enabled:\n    - operator/only\n    - tts/vox\n",
        encoding="utf-8",
    )
    excluded = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert excluded.returncode == 0, excluded.stdout + excluded.stderr
    delta = yaml.safe_load(delta_path.read_text(encoding="utf-8"))
    assert delta["plugins"]["enabled"] == ["operator/only", "tts/vox"]
    assert delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"]["add"] == [
        "tts/vox"
    ]
    generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert generated["plugins"]["enabled"] == ["operator/only", "tts/vox"]
    assert "# explicit operator exclusion" in delta_path.read_text(encoding="utf-8")


def test_fleet_thaws_only_provenance_backed_snapshot_after_base_already_changed(
    tmp_path: Path,
) -> None:
    env, registry, _consumer, role = _fixture(tmp_path)
    pm_role = tmp_path / "pm"
    role.rename(pm_role)
    registry_data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    registry_data["agents"]["demo-pm"]["role_dir"] = str(pm_role)
    registry.write_text(yaml.safe_dump(registry_data, sort_keys=False), encoding="utf-8")
    fleet_home = Path(env["HERMES_FLEET_HOME"])
    base = fleet_home / "config.yaml"
    # The current base no longer contains core-one before the first migration.
    # The historical list below is the only authority for thawing inherited
    # values, preventing a retired plugin from becoming an additive override.
    base.write_text(
        "plugins:\n  enabled:\n    - fleet/core-two\n    - fleet/new-default\n    - fleet/excluded\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: old\n",
        encoding="utf-8",
    )
    profile = fleet_home / "profiles" / "demo-pm"
    delta_path = profile / "config.delta.yaml"
    delta_path.write_text(
        "# legacy operator comment survives thaw\n"
        "plugins:\n"
        "  enabled:\n"
        "    - fleet/core-one\n"
        "    - fleet/excluded\n"
        "    - operator/extra\n"
        "    - tts/vox\n"
        "  operator_metadata: preserve-me\n"
        "x-pjangler-merge:\n"
        "  list_patches:\n"
        "    plugins.enabled:\n"
        "      add:\n"
        "        - tts/vox\n"
        "      remove:\n"
        "        - tts/voxxy\n"
        "        - fleet/excluded\n"
        "  migrations:\n"
        "    plugins_enabled_snapshot:\n"
        "      source: pjangler-52d9445\n"
        "      state: pending\n"
        "      inherited:\n"
        "        - fleet/core-one\n"
        "        - fleet/excluded\n"
        "        - tts/voxxy\n"
        "  operator_extension: keep-me\n"
        "tts:\n"
        "  provider: vox\n"
        "  voice: carlin\n"
        "  vox:\n"
        "    voice: carlin\n",
        encoding="utf-8",
    )
    plugin = tmp_path / "vox-plugin"
    plugin.mkdir()
    env["VOX_PLUGIN_DIR"] = str(plugin)

    first = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    delta_text = delta_path.read_text(encoding="utf-8")
    delta = yaml.safe_load(delta_text)
    assert delta["plugins"] == {"operator_metadata": "preserve-me"}
    directive = delta["x-pjangler-merge"]
    assert directive["operator_extension"] == "keep-me"
    assert directive["migrations"]["plugins_enabled_snapshot"] == {
        "source": "pjangler-52d9445",
        "state": "completed",
        "inherited": ["fleet/core-one", "fleet/excluded", "tts/voxxy"],
    }
    assert directive["list_patches"]["plugins.enabled"] == {
        "add": ["operator/extra", "tts/vox"],
        "remove": ["tts/voxxy", "fleet/excluded"],
    }
    assert "# legacy operator comment survives thaw" in delta_text
    first_generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert first_generated["plugins"]["enabled"] == [
        "fleet/core-two",
        "fleet/new-default",
        "operator/extra",
        "tts/vox",
    ]
    assert "fleet/core-one" not in first_generated["plugins"]["enabled"]

    base.write_text(
        "plugins:\n  enabled:\n    - fleet/core-three\n    - fleet/later-default\n"
        "    - fleet/excluded\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: newer\n",
        encoding="utf-8",
    )
    second = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    generated = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert generated["plugins"]["enabled"] == [
        "fleet/core-three",
        "fleet/later-default",
        "operator/extra",
        "tts/vox",
    ]
    assert "fleet/core-one" not in generated["plugins"]["enabled"]
    assert "fleet/excluded" not in generated["plugins"]["enabled"]

    converged = subprocess.run(
        ["bash", str(FLEET_SYNC), "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert converged.returncode == 0, converged.stdout + converged.stderr


def test_fleet_apply_preserves_unit_and_metadata_when_disable_fails(tmp_path: Path) -> None:
    env, registry, consumer, _ = _fixture(tmp_path)
    env["SYSTEMCTL_MODE"] = "disable-fail"

    result = subprocess.run(
        ["bash", str(FLEET_SYNC), "--apply", "--no-restart", "--agent", "demo-pm"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "disable failed; unit and metadata preserved" in result.stdout
    assert consumer.read_text(encoding="utf-8") == "legacy\n"
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert entry["systemd"]["consumer_unit"] == "hermes-demo-pm-consumer.service"
    assert not Path(env["SYSTEMCTL_RETIRED"]).exists()


def test_fleet_audit_and_apply_fail_closed_on_state_query_errors(tmp_path: Path) -> None:
    for mode in ("active-query-error", "enabled-query-error"):
        case_dir = tmp_path / mode
        case_dir.mkdir()
        env, registry, consumer, _ = _fixture(case_dir)
        env["SYSTEMCTL_MODE"] = mode

        for apply_args in ([], ["--apply", "--no-restart"]):
            result = subprocess.run(
                ["bash", str(FLEET_SYNC), *apply_args, "--agent", "demo-pm"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode != 0
            assert "state query failed; unit and metadata preserved" in result.stdout
            assert consumer.read_text(encoding="utf-8") == "legacy\n"
            entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
            assert entry["systemd"]["consumer_unit"] == "hermes-demo-pm-consumer.service"
            assert not Path(env["SYSTEMCTL_RETIRED"]).exists()


def test_pinned_fork_publication_replaces_upstream_clean_install_path() -> None:
    installer = (ROOT / "install-local.sh").read_text(encoding="utf-8")
    config = (ROOT / "template" / ".scripts" / "config.example.toml").read_text(
        encoding="utf-8"
    )
    expected_sha = "0408fec7a153e6c32c064acd2b8053917f1525f1"
    expected_release = f".local/share/hermes-agent/releases/{expected_sha}"

    assert "raw.githubusercontent.com/NousResearch/hermes-agent" not in installer
    assert 'HERMES_RUNTIME_GIT_URL="https://github.com/delorenj/hermes-agent.git"' in installer
    assert 'HERMES_RUNTIME_GIT_REF="main"' in installer
    assert f'HERMES_RUNTIME_GIT_SHA="{expected_sha}"' in installer
    assert '$HOME/.local/share/hermes-agent/releases/$HERMES_RUNTIME_GIT_SHA' in installer
    assert 'HERMES_BIN="$HERMES_INSTALL_DIR/.venv/bin/hermes"' in installer
    assert 'HERMES_BIN="$(command -v hermes)"' not in installer
    assert "merge-base --is-ancestor" in installer
    assert f'hermes_git_sha = "{expected_sha}"' in config
    assert f'hermes_repo = "~/{expected_release}"' in config
    assert f'hermes_bin = "~/{expected_release}/.venv/bin/hermes"' in config


def test_runtime_templatizer_pins_canonical_bmad_next31_pack() -> None:
    templatizer = (ROOT / "scripts" / "hermes-runtime-templatize.py").read_text(
        encoding="utf-8"
    )

    assert "/packs/bmad/6.10.1-next.31" in templatizer
    assert "/packs/bmad/6.10.2" not in templatizer
