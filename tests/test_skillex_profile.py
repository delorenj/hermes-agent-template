"""Installed Node profile projection through the generated provisioning step."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1"


def run(command, *, cwd, env):
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)


def succeeded(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def snapshot(root):
    if not root.exists():
        return None
    result = {"": (root.lstat().st_ino, root.lstat().st_mode, None)}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            stat = path.lstat()
            value = (
                os.readlink(path)
                if path.is_symlink()
                else path.read_bytes()
                if path.is_file()
                else None
            )
            result[str(path.relative_to(root))] = (stat.st_ino, stat.st_mode, value)
    return result


MIN_NODE_MAJOR = 24


def _node_major(node: Path) -> int:
    result = subprocess.run([str(node), "--version"], text=True, capture_output=True)
    try:
        return int(result.stdout.strip().lstrip("v").split(".")[0])
    except ValueError:
        return 0


def _node_with_npm() -> Path:
    """The Node this suite installs Skillex with.

    SKILLEX_TEST_NODE_BIN wins. Otherwise the first candidate that is new
    enough for Skillex AND ships npm beside it: the ambient `node`, then the one
    mise resolves. The ambient one is often not it: a shell without mise
    activation finds the distro's /usr/bin/node (Node 20), and a mise install
    that was pruned or half-installed can leave a node with no npm.
    """
    override = os.environ.get("SKILLEX_TEST_NODE_BIN")
    if override:
        return Path(override).resolve()
    candidates = []
    ambient = shutil.which("node")
    if ambient:
        candidates.append(Path(ambient).resolve())
    mise = shutil.which("mise")
    if mise:
        resolved = subprocess.run([mise, "which", "node"], text=True, capture_output=True)
        if resolved.returncode == 0 and resolved.stdout.strip():
            candidates.append(Path(resolved.stdout.strip()).resolve())
    for node in candidates:
        if (node.parent / "npm").exists() and _node_major(node) >= MIN_NODE_MAJOR:
            return node
    pytest.fail(
        f"no Node >= {MIN_NODE_MAJOR} with npm beside it among {[str(c) for c in candidates]}; "
        "set SKILLEX_TEST_NODE_BIN"
    )


@pytest.fixture(scope="module")
def installed():
    with tempfile.TemporaryDirectory(
        prefix="hermes-template-skillex-", dir="/tmp"
    ) as tmp:
        root = Path(tmp).resolve()
        node = _node_with_npm()
        mise = Path(shutil.which("mise")).resolve()
        prefix = root / "package"
        env = {
            "HOME": str(root / "install-home"),
            "npm_config_cache": str(root / "npm-cache"),
            "PATH": f"{node.parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
        Path(env["HOME"]).mkdir()
        package = os.environ.get("SKILLEX_TEST_TARBALL", f"@delorenj/skillex@{VERSION}")
        succeeded(
            run(
                [
                    str(node.parent / "npm"),
                    "install",
                    "--global",
                    "--prefix",
                    str(prefix),
                    "--ignore-scripts",
                    "--omit=dev",
                    "--no-audit",
                    "--no-fund",
                    package,
                ],
                cwd=root,
                env=env,
            )
        )
        assert (
            succeeded(
                run([str(prefix / "bin/skillex"), "--version"], cwd=root, env=env)
            ).stdout.strip()
            == VERSION
        )
        yield root, node, mise, prefix


@pytest.fixture
def fixture(installed):
    base, node, mise, prefix = installed
    with tempfile.TemporaryDirectory(prefix="case-", dir=base) as tmp:
        root = Path(tmp).resolve()
        project = root / "selected project"
        role = project / "agents/hermes/pm"
        scripts = role / ".scripts"
        scripts.mkdir(parents=True)
        for name in ("10-hermes-profile.sh", "_lib.sh"):
            shutil.copy2(ROOT / "template/.scripts" / name, scripts)
        shutil.copytree(
            ROOT / "template/.scripts/lib",
            scripts / "lib",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (role / "role.yaml").write_text(
            "repo: fixture\nrole: pm\nagent_id: fixture-pm\ndisplay_name: Fixture PM\n"
            "profile: fixture-pm\ntelegram:\n  bot_username: fixture_pm_bot\n"
            "plane:\n  workspace: test\nruntime:\n  github_repo: ignored-runtime\n"
        )
        (role / "SOUL.md").write_text("Profile-specific identity.\n")
        home = root / "home"
        (home / ".agents").mkdir(parents=True)
        global_manifest = home / ".agents/skills.json"
        global_manifest.write_text(
            json.dumps({"skills": [{"name": "alpha"}, {"name": "gamma"}]})
        )
        (project / ".agents").mkdir()
        manifest = project / ".agents/skills.json"
        manifest.write_text(
            json.dumps(
                {
                    "inherit_global": False,
                    "exclude": ["alpha"],
                    "skills": [{"name": "beta"}],
                }
            )
        )
        registry = root / "catalog"
        for name in ("alpha", "beta", "gamma", "delta"):
            skill = registry / "all-skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Fixture {name}\n---\n"
            )
        profile = home / ".hermes/profiles/fixture-pm"
        profile.mkdir(parents=True)
        # This is deliberately not the selected project or a descendant of it.
        cwd = root / "ambient project"
        (cwd / ".agents").mkdir(parents=True)
        (cwd / ".agents/skills.json").write_text(
            '{"skills":[{"name":"not-selected"}]}\n'
        )
        tools = root / "tools"
        tools.mkdir()
        for name, target in {"node": node, "mise": mise, "sh": Path("/bin/sh")}.items():
            (tools / name).symlink_to(target)
        hermes = root / "hermes"
        hermes.write_text(
            '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$HERMES_LOG"\n'
            "if [[ \"$1 $2\" == 'profile create' ]]; then\n"
            '  mkdir -p "$HOME/.hermes/profiles/$3"\nfi\n'
        )
        hermes.chmod(0o755)
        env = {
            "HOME": str(home),
            "PATH": os.pathsep.join(
                [
                    str(tools),
                    str(node.parent),
                    str(Path(sys.executable).parent),
                    "/usr/bin",
                    "/bin",
                ]
            ),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "MISE_DATA_DIR": str(root / "mise-data"),
            "MISE_CACHE_DIR": str(root / "mise-cache"),
            "MISE_CONFIG_DIR": str(root / "mise-config"),
            "HERMES_FLEET_ENV": str(home / ".hermes/fleet.env"),
            "HERMES_BIN": str(hermes),
            "HERMES_LOG": str(root / "hermes.log"),
            "PROFILE_RENDERER": str(root / "no-renderer"),
            "PJANGLER_PROJECT_ROOT": str(project),
            "PJ_SKILLS_REGISTRY_ROOT": str(registry),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        succeeded(
            run(
                [str(mise), "link", f"npm:@delorenj/skillex@{VERSION}", str(prefix)],
                cwd=root,
                env=env,
            )
        )
        yield {
            "root": root,
            "project": project,
            "profile": profile,
            "home": home,
            "role": role,
            "scripts": scripts,
            "registry": registry,
            "env": env,
            "cwd": cwd,
            "tools": tools,
            "manifest": manifest,
            "global_manifest": global_manifest,
        }


def provision(fixture):
    return run(
        ["/bin/bash", str(fixture["scripts"] / "10-hermes-profile.sh")],
        cwd=fixture["cwd"],
        env=fixture["env"],
    )


def cli(fixture, *args):
    env = dict(fixture["env"], PATH=str(fixture["tools"]))
    assert shutil.which("python3", path=env["PATH"]) is None
    assert shutil.which("uv", path=env["PATH"]) is None
    return run(
        [
            str(fixture["tools"] / "mise"),
            "exec",
            f"npm:@delorenj/skillex@{VERSION}",
            "--",
            "skillex",
            "profile",
            "sync",
            "fixture-pm",
            "--hermes-root",
            str(fixture["home"] / ".hermes"),
            "--project",
            str(fixture["project"]),
            *args,
        ],
        cwd=fixture["cwd"],
        env=env,
    )


def test_step_uses_explicit_union_and_preserves_profile_local_overrides(fixture):
    skills = fixture["profile"] / "skills"
    local = skills / "alpha"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("Profile-owned alpha wins.\n")
    (local / "helper.sh").write_text("#!/bin/sh\nexit 0\n")
    (local / "helper.sh").chmod(0o755)
    gamma = skills / "gamma"
    gamma.symlink_to(fixture["registry"] / "all-skills/gamma")
    (skills / "runtime-notes.txt").write_text("Hermes-owned file.\n")
    local_before = snapshot(local)
    profile_inode, skills_inode = fixture["profile"].stat().st_ino, skills.stat().st_ino
    source_before = snapshot(fixture["registry"])
    manifest_bytes = (
        fixture["manifest"].read_bytes(),
        fixture["global_manifest"].read_bytes(),
    )
    succeeded(provision(fixture))
    assert (skills / "beta").resolve() == fixture["registry"] / "all-skills/beta"
    assert snapshot(local) == local_before
    assert (skills / "gamma").is_symlink()
    assert fixture["profile"].stat().st_ino == profile_inode
    assert skills.stat().st_ino == skills_inode
    assert snapshot(fixture["registry"]) == source_before
    assert (
        fixture["manifest"].read_bytes(),
        fixture["global_manifest"].read_bytes(),
    ) == manifest_bytes
    assert not (fixture["project"] / ".agents/skills").exists()
    assert not (fixture["home"] / ".agents/skills").exists()
    assert not (skills / "software-development").exists(), "no copied PM fallback"
    assert (
        fixture["profile"] / "SOUL.md"
    ).read_text() == "Profile-specific identity.\n"
    before = snapshot(skills), snapshot(fixture["root"] / "state")
    succeeded(provision(fixture))
    assert (snapshot(skills), snapshot(fixture["root"] / "state")) == before


def test_new_profile_receives_declared_global_and_project_skills(fixture):
    fixture["profile"].rmdir()
    succeeded(provision(fixture))
    skills = fixture["profile"] / "skills"
    assert skills.is_dir() and not skills.is_symlink()
    assert {path.name for path in skills.iterdir()} == {"alpha", "beta", "gamma"}
    assert (fixture["root"] / "hermes.log").read_text().splitlines() == [
        "profile create fixture-pm --no-alias"
    ]
    # A project exclusion does not erase the independently selected global alpha.
    assert (skills / "alpha").resolve() == fixture["registry"] / "all-skills/alpha"
    assert not (fixture["cwd"] / ".agents/skills").exists()


def test_node_only_preview_then_owned_prune_preserves_foreign_and_released_entries(
    fixture,
):
    before = snapshot(fixture["profile"])
    preview = succeeded(cli(fixture, "--dry-run", "--json"))
    assert json.loads(preview.stdout)["data"]["changes"]
    assert snapshot(fixture["profile"]) == before
    assert not (fixture["root"] / "state").exists()
    succeeded(cli(fixture, "--json"))
    skills = fixture["profile"] / "skills"
    beta = skills / "beta"
    beta.unlink()
    beta.mkdir()
    (beta / "SKILL.md").write_text("Runtime took ownership.\n")
    foreign = skills / "foreign"
    foreign.symlink_to(fixture["registry"] / "all-skills/delta")
    before = snapshot(beta), foreign.lstat().st_ino
    fixture["manifest"].write_text('{"inherit_global":false,"skills":[]}\n')
    fixture["global_manifest"].write_text('{"skills":[{"name":"alpha"}]}\n')
    succeeded(cli(fixture, "--json"))
    assert not (skills / "gamma").exists(), (
        "only an unchanged receipt-owned child is pruned"
    )
    assert (snapshot(beta), foreign.lstat().st_ino) == before
    assert (skills / "alpha").is_symlink()


def test_invalid_selected_manifest_refuses_before_existing_profile_cleanup(fixture):
    (fixture["profile"] / "gateway.pid").write_text("runtime evidence\n")
    (fixture["profile"] / "state.db").write_bytes(b"runtime-owned")
    fixture["manifest"].write_text("{malformed\n")
    before = snapshot(fixture["profile"]), snapshot(fixture["registry"])
    refused = provision(fixture)
    assert refused.returncode != 0
    assert "E_MANIFEST_PARSE" in refused.stdout + refused.stderr
    assert (snapshot(fixture["profile"]), snapshot(fixture["registry"])) == before


def test_missing_project_manifest_is_an_init_refusal_not_an_ambient_fallback(fixture):
    fixture["manifest"].unlink()
    fixture["profile"].rmdir()
    refused = provision(fixture)
    assert refused.returncode != 0
    assert "skillex init --project" in refused.stderr
    assert not fixture["profile"].exists()
    assert not (fixture["root"] / "hermes.log").exists()


def test_whole_skills_link_is_refused_without_following_it(fixture):
    target = fixture["root"] / "legacy-shared-skills"
    target.mkdir()
    (target / "runtime-owned.txt").write_text("Do not follow or replace.\n")
    (fixture["profile"] / "skills").symlink_to(target)
    before = snapshot(fixture["profile"]), snapshot(target)
    refused = provision(fixture)
    assert refused.returncode != 0
    assert "E_PROFILE_SKILLS_ROOT" in refused.stdout + refused.stderr
    assert (snapshot(fixture["profile"]), snapshot(target)) == before


def test_root_task_has_no_automatic_skill_writer():
    config = tomllib.loads((ROOT / "mise.toml").read_text())
    tasks = config["tasks"]
    assert [name for name in tasks if name.startswith("skills:")] == ["skills:sync"]
    assert tasks["skills:sync"]["tools"] == {"npm:@delorenj/skillex": VERSION}
    assert (
        tasks["skills:sync"]["run"]
        == "skillex sync --scope project --project '{{config_root}}'"
    )
    for hook in config["hooks"]["enter"]:
        assert (
            "sync-skills" not in hook["script"]
            and "provision-packs" not in hook["script"]
        )
    assert all(watch["task"] != "skills:sync" for watch in config["watch_files"])
    for name in ("sync-skills.py", "provision-packs.py"):
        assert not (ROOT / ".mise/scripts" / name).exists()
