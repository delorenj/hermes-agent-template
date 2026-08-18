from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "template"
SCRIPTS = ROOT / "scripts"
FLEET_LIBRARY = TEMPLATE / ".scripts" / "lib" / "fleet-env.sh"
FLEET_PARSER = TEMPLATE / ".scripts" / "lib" / "parse-fleet-env.py"

# This intentionally matches shell snippets embedded inside Python strings too.
# Every full fleet import must cross the shared parser/atomic-apply boundary;
# executable sourcing is forbidden regardless of quoting or dot/source spelling.
EXECUTABLE_FLEET_SOURCE = re.compile(
    r"(?im)(?:^|[;{]\s*)[ \t]*(?:builtin\s+)?(?:source|\.)\s+[^\n#]*"
    r"(?:\$\{?(?:HERMES_)?FLEET_ENV\}?(?![A-Za-z0-9_])|fleet\\?\.env)"
)


def _clean_env(tmp_path: Path, fleet: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "HERMES_BIN",
            "HERMES_FLEET_BIN",
            "HERMES_FLEET_HOME",
            "HERMES_FLEET_REGISTRY_FILE",
            "HERMES_FLEET_REPO",
        }
    }
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(fleet),
            "HERMES_TEMPLATE_CONFIG": str(tmp_path / "missing-config.toml"),
        }
    )
    return env


def _malicious_fleet(marker: Path) -> str:
    return f"PJAN67_EXECUTED=$(touch {marker})\n"


def _parsed_fleet(fleet: Path, env: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(
        ["python3", "-I", str(FLEET_PARSER), str(fleet)],
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    records = result.stdout.split(b"\0")
    assert records[0] == b"PJANGLER_FLEET_ENV_V1"
    assert records[-3:] == [b"PJANGLER_FLEET_ENV_END", b"", b""]
    return {
        key.decode(): value.decode()
        for record in records[1:-3]
        for key, value in [record.split(b"=", 1)]
    }


def test_shipped_sources_forbid_every_executable_fleet_env_spelling() -> None:
    production = [
        path
        for root in (TEMPLATE, SCRIPTS)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {"", ".sh", ".py", ".jinja"}
    ]
    violations = {
        str(path.relative_to(ROOT)): match.group(0)
        for path in production
        if (match := EXECUTABLE_FLEET_SOURCE.search(path.read_text(encoding="utf-8")))
    }

    assert violations == {}
    for spelling in (
        'source "$FLEET_ENV"',
        ". '${HERMES_FLEET_ENV}'",
        'builtin source -- "$HOME/.hermes/fleet.env"',
        '{ . "${FLEET_ENV}"; }',
    ):
        assert EXECUTABLE_FLEET_SOURCE.search(spelling), spelling


@pytest.mark.parametrize(
    "script",
    [SCRIPTS / "fleet-sync.sh", SCRIPTS / "migrate-unify.sh"],
    ids=["fleet-sync", "migrate-unify"],
)
def test_maintenance_consumers_reject_shell_code_without_executing_it(
    tmp_path: Path, script: Path
) -> None:
    marker = tmp_path / "fleet-code-executed"
    fleet = tmp_path / "fleet.env"
    fleet.write_text(_malicious_fleet(marker), encoding="utf-8")
    before = fleet.read_bytes()
    result = subprocess.run(
        ["bash", str(script), "--help"],
        env=_clean_env(tmp_path, fleet),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "fleet environment" in result.stderr
    assert not marker.exists()
    assert fleet.read_bytes() == before


def test_backfill_rejects_shell_code_before_any_write(tmp_path: Path) -> None:
    marker = tmp_path / "fleet-code-executed"
    fleet = tmp_path / "fleet.env"
    fleet.write_text(_malicious_fleet(marker), encoding="utf-8")
    before = fleet.read_bytes()
    env = _clean_env(tmp_path, fleet)
    env["HERMES_FLEET_REGISTRY_FILE"] = str(tmp_path / "missing-registry.yaml")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "fleet environment" in result.stderr
    assert not marker.exists()
    assert fleet.read_bytes() == before
    assert not (Path(env["HOME"]) / ".config").exists()


@pytest.mark.parametrize("kind", ["duplicate", "symlink"])
def test_backfill_rejects_invalid_existing_fleet_without_mutation(
    tmp_path: Path, kind: str
) -> None:
    fleet = tmp_path / "fleet.env"
    if kind == "duplicate":
        fleet.write_text("SAFE=one\n  export SAFE='two'\n", encoding="utf-8")
        protected = fleet
    else:
        protected = tmp_path / "real-fleet.env"
        protected.write_text("SAFE=unchanged\n", encoding="utf-8")
        fleet.symlink_to(protected)
    before = protected.read_bytes()
    env = _clean_env(tmp_path, fleet)
    env["HERMES_FLEET_REGISTRY_FILE"] = str(tmp_path / "missing-registry.yaml")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "fleet environment" in result.stderr
    assert protected.read_bytes() == before
    if kind == "symlink":
        assert fleet.is_symlink()


def _launcher_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    (role / "runtime").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "consumer-pm").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    launcher = role / "hermes"
    launcher.write_text(
        (TEMPLATE / "hermes.jinja").read_text(encoding="utf-8").replace(
            "{{ agent_id }}", "consumer-pm"
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    shutil.copytree(TEMPLATE / ".scripts" / "lib", role / ".scripts" / "lib")
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True, exist_ok=True)
    env = _clean_env(tmp_path, fleet)
    return launcher, fleet, env, role


def test_real_launcher_rejects_shell_code_before_hermes_child(tmp_path: Path) -> None:
    launcher, fleet, env, _ = _launcher_fixture(tmp_path)
    marker = tmp_path / "fleet-code-executed"
    child = tmp_path / "hermes-child-executed"
    fake_hermes = tmp_path / "fake-hermes"
    fake_hermes.write_text(f"#!/usr/bin/env bash\ntouch {child}\n", encoding="utf-8")
    fake_hermes.chmod(0o755)
    env["HERMES_BIN"] = str(fake_hermes)
    fleet.write_text(_malicious_fleet(marker), encoding="utf-8")

    result = subprocess.run(
        [str(launcher), "gateway", "run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "fleet environment" in result.stderr
    assert not marker.exists()
    assert not child.exists()


def test_real_launcher_preserves_literal_values_and_caller_precedence(
    tmp_path: Path,
) -> None:
    launcher, fleet, env, role = _launcher_fixture(tmp_path)
    observed = tmp_path / "observed.json"
    fake_hermes = tmp_path / "fake hermes"
    fake_hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(observed)!r}, 'w').write(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    fleet.write_text(
        "HERMES_FLEET_BIN=$'" + str(fake_hermes).replace("'", "\\'") + "'\n"
        "PJAN67_SPACES=$'alpha beta = gamma'\n"
        "PJAN67_MULTILINE=$'first line\\nsecond line\\n'\n"
        "PATH=$'/fleet/path/must/not/win'\n",
        encoding="utf-8",
    )
    controlled_path = env["PATH"]

    result = subprocess.run(
        [str(launcher), "status"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(observed.read_text(encoding="utf-8"))
    assert child_env["PJAN67_SPACES"] == "alpha beta = gamma"
    assert child_env["PJAN67_MULTILINE"] == "first line\nsecond line\n"
    assert child_env["PATH"] == controlled_path


def test_maintenance_scripts_accept_canonical_data_only_fleet(tmp_path: Path) -> None:
    fleet = tmp_path / "fleet state" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("PJAN67_SAFE=$'spaces = and\\nnewlines\\n'\n", encoding="utf-8")
    for script in (SCRIPTS / "fleet-sync.sh", SCRIPTS / "migrate-unify.sh"):
        result = subprocess.run(
            ["bash", str(script), "--help"],
            env=_clean_env(tmp_path / script.stem, fleet),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_backfill_installs_canonical_loader_and_generated_launcher(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    (role / ".scripts").mkdir(parents=True)
    (role / "runtime").mkdir()
    (home / ".hermes" / "profiles" / "backfill-pm").mkdir(parents=True)
    (role / "hermes").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    (role / ".scripts" / "_lib.sh").write_text("# legacy library\n", encoding="utf-8")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "backfill-pm": {
                        "role": "pm",
                        "role_dir": str(role),
                        "project_path": str(project),
                        "profile_name": "backfill-pm",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    fleet = home / ".hermes" / "fleet.env"
    env = _clean_env(tmp_path, fleet)
    env["HERMES_FLEET_REGISTRY_FILE"] = str(registry)
    env["HERMES_FLEET_BIN"] = "/fixture/hermes with spaces/$literal;'\"\\tail"
    env["HERMES_FLEET_REPO"] = "/fixture/repo\nsecond line\n"
    env["HERMES_FLEET_OAUTH_FILE"] = "/fixture/auth = $literal ' quote"
    env["HERMES_FLEET_CODEX_HOME"] = "/fixture/codex\\path\nnext\n"

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (role / ".scripts" / "lib" / "fleet-env.sh").read_bytes() == FLEET_LIBRARY.read_bytes()
    assert (role / ".scripts" / "lib" / "parse-fleet-env.py").read_bytes() == FLEET_PARSER.read_bytes()
    assert (role / ".scripts" / "_lib.sh").read_bytes() == (TEMPLATE / ".scripts" / "_lib.sh").read_bytes()
    wrapper = (role / "hermes").read_text(encoding="utf-8")
    assert "load_fleet_environment" in wrapper
    assert not EXECUTABLE_FLEET_SOURCE.search(wrapper)
    records = _parsed_fleet(fleet, env)
    assert records == {
        "HERMES_FLEET_BIN": env["HERMES_FLEET_BIN"],
        "HERMES_FLEET_REPO": env["HERMES_FLEET_REPO"],
        "HERMES_FLEET_REGISTRY_FILE": str(registry),
        "HERMES_FLEET_OAUTH_FILE": env["HERMES_FLEET_OAUTH_FILE"],
        "HERMES_FLEET_CODEX_HOME": env["HERMES_FLEET_CODEX_HOME"],
    }
