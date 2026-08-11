from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
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
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hermes = fake_bin / "hermes"
    hermes.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_BIN": str(hermes),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "RUNTIME_SCAFFOLD_DIR": str(scaffold),
            "VOXXY_PLUGIN_DIR": str(tmp_path / "missing-voxxy"),
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
    assert (Path(env["HOME"]) / ".hermes" / "profiles" / "demo-pm").resolve() == runtime
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
    assert 'cp -an "$TMP/." "$RUNTIME_LOCAL/"' in script
