from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
PROFILE_SCRIPT = ROOT / "template" / ".scripts" / "10-hermes-profile.sh"
RUNTIME_SCRIPT = ROOT / "template" / ".scripts" / "20-runtime-repo.sh"
LIB_SCRIPT = ROOT / "template" / ".scripts" / "_lib.sh"
SECRET_SCAN = ROOT / "template" / ".scripts" / "secret-scan.py"


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scaffold = role / ".runtime-scaffold"
    scripts.mkdir(parents=True)
    scaffold.mkdir()
    shutil.copytree(ROOT / "template" / ".scripts" / "lib", scripts / "lib")
    shutil.copy2(RUNTIME_SCRIPT, scripts / RUNTIME_SCRIPT.name)
    shutil.copy2(LIB_SCRIPT, scripts / LIB_SCRIPT.name)
    shutil.copy2(SECRET_SCAN, scripts / SECRET_SCAN.name)
    (scaffold / "MEMORY.md").write_text("agent={{agent_id}}\n", encoding="utf-8")
    (role / "SOUL.md").write_text("local soul\n", encoding="utf-8")
    (role / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
telegram:
  bot_username: demo_pm_bot
plane:
  workspace: test
runtime:
  github_owner: legacy
  github_repo: agent-hm-demo-pm
  local_path: ./runtime
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)

    home = tmp_path / "home"
    profiles = home / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - fleet/core-one\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: rick\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes = fake_bin / "hermes"
    hermes.write_text(
        """#!/usr/bin/env bash
printf '%s|%s\n' "${HERMES_HOME:-}" "$*" >> "$HERMES_LOG"
[[ "${FAIL_HERMES_CONFIG:-0}" != "1" ]]
""",
        encoding="utf-8",
    )
    hermes.chmod(0o755)
    pj = fake_bin / "pj"
    pj.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PJANGLER_LOG"
case " $* " in
  *" --dry-run "*) exit 0 ;;
esac
mkdir -p "$HOME/.hermes/profiles/demo-pm"
exit 0
""",
        encoding="utf-8",
    )
    pj.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_BIN": str(hermes),
            "HERMES_LOG": str(tmp_path / "hermes.log"),
            "PJANGLER_BIN": str(pj),
            "PJANGLER_LOG": str(tmp_path / "pjangler.log"),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "RUNTIME_SCAFFOLD_DIR": str(scaffold),
            "VOX_PLUGIN_DIR": str(tmp_path / "missing-vox"),
        }
    )
    return project, role, env


def _run(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(role / ".scripts" / "20-runtime-repo.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_provisioner_creates_ignored_local_runtime_without_git(tmp_path: Path) -> None:
    project, role, env = _fixture(tmp_path)

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    runtime = role / "runtime"
    assert (runtime / "MEMORY.md").read_text(encoding="utf-8") == "agent=demo-pm\n"
    assert (runtime / "SOUL.md").read_text(encoding="utf-8") == "local soul\n"
    assert not (runtime / ".git").exists()
    profile = Path(env["HOME"]) / ".hermes" / "profiles" / "demo-pm"
    assert profile.is_dir()
    assert not profile.is_symlink()
    calls = Path(env["PJANGLER_LOG"]).read_text(encoding="utf-8")
    assert "migrate hermes.runtime-singleton" in calls
    assert "--dry-run --json" in calls
    assert not Path(env["HERMES_LOG"]).exists()
    assert subprocess.run(
        ["git", "check-ignore", "-q", "agents/hermes/pm/runtime/"],
        cwd=project,
        check=False,
    ).returncode == 0


def test_provisioner_preserves_existing_runtime_and_refuses_stale_mapping(tmp_path: Path) -> None:
    project, role, env = _fixture(tmp_path)
    runtime = role / "runtime"
    runtime.mkdir()
    private_state = runtime / "private-state.txt"
    private_state.write_text("preserve exactly\n", encoding="utf-8")
    (project / ".gitmodules").write_text(
        """[submodule "legacy-runtime"]
\tpath = agents/hermes/pm/runtime
\turl = git@github.com:example/legacy.git
""",
        encoding="utf-8",
    )

    result = _run(role, env)

    assert result.returncode != 0
    assert "stale .gitmodules mapping" in result.stderr
    assert private_state.read_text(encoding="utf-8") == "preserve exactly\n"
    assert not (runtime / ".git").exists()


def test_active_provisioner_has_no_runtime_repo_or_submodule_mutation() -> None:
    script = RUNTIME_SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "gh repo create",
        "gh repo view",
        "git submodule add",
        "git submodule update",
        'rm -rf "$RUNTIME_LOCAL"',
        "git push",
        "git init",
    ):
        assert forbidden not in script
    assert "stale .gitmodules mapping" in script
    assert 'cp -an "$TMP/." "$RUNTIME_LOCAL/"' not in script
    assert "os.path.lexists(destination)" in script
    assert "migrate hermes.runtime-singleton" in script
    assert 'ln -sfn "$RUNTIME_LOCAL" "$PROFILE_HOME"' not in script
    assert "config set terminal.cwd" not in script
    assert "config set terminal.cwd" not in PROFILE_SCRIPT.read_text(encoding="utf-8")


def test_provisioner_never_replaces_existing_named_profile(tmp_path: Path) -> None:
    _project, role, env = _fixture(tmp_path)
    profile = Path(env["HOME"]) / ".hermes" / "profiles" / "demo-pm"
    profile.mkdir(parents=True)
    sentinel = profile / "owned-marker"
    sentinel.write_text("preserve\n", encoding="utf-8")

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert profile.is_dir() and not profile.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_runtime_provisioner_never_mutates_generated_profile_config_with_hermes(
    tmp_path: Path,
) -> None:
    _project, role, env = _fixture(tmp_path)
    env["FAIL_HERMES_CONFIG"] = "1"

    result = _run(role, env)

    assert result.returncode == 0, result.stderr
    assert (role / ".scripts" / ".done-20-runtime-repo").exists()
    assert not Path(env["HERMES_LOG"]).exists()


def test_pm_voice_claim_matches_effective_vox_carlin_config(tmp_path: Path) -> None:
    _project, role, env = _fixture(tmp_path)
    plugin = tmp_path / "vox-plugin"
    plugin.mkdir()
    env["VOX_PLUGIN_DIR"] = str(plugin)
    profile = Path(env["HOME"]) / ".hermes" / "profiles" / "demo-pm"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: rick\n",
        encoding="utf-8",
    )

    result = _run(role, env)

    assert result.returncode == 0, result.stderr
    assert "PM voice verified: provider=vox voice=carlin" in result.stderr
    assert (profile / "plugins" / "tts" / "vox").resolve() == plugin.resolve()
    generated = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "tts/voxxy" not in generated
    assert "fleet/core-one" in generated
    assert "provider: vox\n" in generated
    assert "voice: carlin\n" in generated
    assert not Path(env["HERMES_LOG"]).exists()

    delta_path = profile / "config.delta.yaml"
    delta = yaml.safe_load(delta_path.read_text(encoding="utf-8"))
    assert "plugins" not in delta
    assert delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"] == {
        "add": ["tts/vox"],
        "remove": ["tts/voxxy"],
    }

    fleet_base = Path(env["HOME"]) / ".hermes" / "config.yaml"
    fleet_base.write_text(
        "plugins:\n  enabled:\n    - fleet/core-two\n    - tts/voxxy\n"
        "tts:\n  provider: voxxy\n  voice: rick\n",
        encoding="utf-8",
    )
    rerun = _run(role, env)
    assert rerun.returncode == 0, rerun.stderr
    rerendered = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "fleet/core-two" in rerendered
    assert "fleet/core-one" not in rerendered
    assert "tts/voxxy" not in rerendered

    delta["plugins"] = {"enabled": ["operator/only"]}
    delta_path.write_text(
        "# operator exclusion must survive\n"
        + yaml.safe_dump(delta, sort_keys=False),
        encoding="utf-8",
    )
    excluded = _run(role, env)
    assert excluded.returncode == 0, excluded.stderr
    excluded_generated = yaml.safe_load(
        (profile / "config.yaml").read_text(encoding="utf-8")
    )
    assert excluded_generated["plugins"]["enabled"] == ["operator/only", "tts/vox"]
    assert "# operator exclusion must survive" in delta_path.read_text(encoding="utf-8")


def test_step20_preserves_shared_config_behind_profile_symlink(tmp_path: Path) -> None:
    _project, role, env = _fixture(tmp_path)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace("role: pm", "role: director"),
        encoding="utf-8",
    )
    home = Path(env["HOME"])
    shared_config = home / ".hermes" / "config.yaml"
    shared_config.write_bytes(b"fleet: shared\nterminal:\n  cwd: /fleet/default\n")
    before = shared_config.read_bytes()
    profile_config = home / ".hermes" / "profiles" / "demo-pm" / "config.yaml"
    profile_config.parent.mkdir(parents=True)
    profile_config.symlink_to(shared_config)

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert profile_config.is_symlink()
    assert shared_config.read_bytes() == before
    assert not Path(env["HERMES_LOG"]).exists()


def test_deferred_runtime_step_clears_only_its_marker_and_later_reconciles(
    tmp_path: Path,
) -> None:
    _project, role, env = _fixture(tmp_path)
    marker = role / ".scripts" / ".done-20-runtime-repo"
    unrelated = role / ".scripts" / ".done-10-hermes-profile"
    marker.touch()
    unrelated.touch()
    env["SKIP_RUNTIME_REPO"] = "1"

    deferred = _run(role, env)

    assert deferred.returncode == 0, deferred.stderr
    assert not marker.exists()
    assert unrelated.exists()

    env.pop("SKIP_RUNTIME_REPO")
    activated = _run(role, env)

    assert activated.returncode == 0, activated.stderr
    assert marker.exists()
    assert unrelated.exists()
