from __future__ import annotations

import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
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
    r"(?im)(?:^|[;{]\s*)[ \t]*(?:(?:builtin|command)\s+)*(?:source|\.)\s+[^\n#]*"
    r"(?:\$\{?(?:HERMES_)?FLEET_ENV\}?(?![A-Za-z0-9_])|fleet\\?\.env)"
)
FLEET_REFERENCE = re.compile(
    r"(?i)(?:HERMES_)?FLEET_ENV|fleet\.env|load_fleet_environment|import_fleet_environment"
)
DYNAMIC_EXECUTION = (
    re.compile(r"(?m)\beval\b"),
    re.compile(r"(?m)\b(?:bash|sh|zsh|dash)\b[^\n]*(?:^|[ \t])(?:-c|--command)(?:[ \t]|$)"),
    re.compile(r"(?m)^[ \t]*[A-Za-z_][A-Za-z0-9_]*=[\"']?(?:source|\.)[\"']?(?:[; \t]|$)"),
)
FLEET_REFERENCE_ALLOWLIST = {
    "scripts/backfill-fleet-sot.py",
    "scripts/backfill-fleet-sot.sh",
    "scripts/fleet-sync.sh",
    "scripts/migrate-unify.sh",
    "template/.scripts/01-config.sh",
    "template/.scripts/05-fleet-env.sh",
    "template/.scripts/30-telegram.sh",
    "template/.scripts/31-slack.sh",
    "template/.scripts/70-systemd.sh",
    "template/.scripts/80-registry.sh",
    "template/.scripts/99-summary.sh",
    "template/.scripts/_lib.sh",
    "template/.scripts/heartbeat.sh",
    "template/.scripts/lib/fleet-env.sh",
    "template/.scripts/lib/parse-fleet-env.py",
    "template/.scripts/providers/plane.sh",
    "template/hermes.jinja",
}


def executable_sources(*roots: Path) -> list[Path]:
    return [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.suffix in {"", ".sh", ".py", ".jinja", ".command"}
            or stat.S_IMODE(path.stat().st_mode) & 0o111
        )
    ]


def executable_fleet_violation(text: str) -> re.Match[str] | None:
    direct = EXECUTABLE_FLEET_SOURCE.search(text)
    if direct is not None:
        return direct
    if not FLEET_REFERENCE.search(text):
        return None
    for pattern in DYNAMIC_EXECUTION:
        match = pattern.search(text)
        if match is not None:
            return match
    return None


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


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    if not root.exists() and not root.is_symlink():
        return {}
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else str(path.relative_to(root))
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path), mode)
        elif path.is_dir():
            snapshot[relative] = ("directory", mode)
        else:
            snapshot[relative] = ("file", path.read_bytes(), mode)
    return snapshot


def _backfill_fixture(tmp_path: Path, *, invalid_last: bool = False) -> tuple[dict[str, str], dict[str, Path]]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    first_role = project / "agents" / "hermes" / "first"
    last_role = project / "agents" / "hermes" / "last"
    for role in (first_role, last_role):
        (role / ".scripts").mkdir(parents=True)
        (role / "runtime").mkdir()
        (role / "hermes").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (role / ".scripts" / "_lib.sh").write_text("# legacy library\n", encoding="utf-8")
    if invalid_last:
        shutil.rmtree(last_role / ".scripts")
        outside = tmp_path / "outside-scripts"
        outside.mkdir()
        (last_role / ".scripts").symlink_to(outside, target_is_directory=True)

    systemd = home / ".config" / "systemd" / "user"
    systemd.mkdir(parents=True)
    unit = systemd / "hermes-first-pm-gateway.service"
    unit.write_text("[Service]\nEnvironment=HERMES_HOME=/old\n", encoding="utf-8")

    registry = tmp_path / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "first-pm": {
                        "role": "pm",
                        "role_dir": str(first_role),
                        "project_path": str(project),
                        "profile_name": "first-pm",
                    },
                    "last-pm": {
                        "role": "pm",
                        "role_dir": str(last_role),
                        "project_path": str(project),
                        "profile_name": "last-pm",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("SAFE_KEY=$'unchanged'\n", encoding="utf-8")

    marker = tmp_path / "external-process.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PJAN67_EXTERNAL_PROCESS_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    env = _clean_env(tmp_path, fleet)
    env.update(
        {
            "HERMES_FLEET_REGISTRY_FILE": str(registry),
            "HERMES_FLEET_BIN": "/fixture/hermes",
            "HERMES_FLEET_REPO": "/fixture/repo",
            "HERMES_FLEET_OAUTH_FILE": "/fixture/auth.json",
            "HERMES_FLEET_CODEX_HOME": "/fixture/codex",
            "HERMES_FLEET_SYSTEMD_DIR": str(systemd),
            "PJAN67_EXTERNAL_PROCESS_LOG": str(marker),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    return env, {
        "fleet": fleet,
        "registry": registry,
        "project": project,
        "systemd": systemd,
        "marker": marker,
    }


def test_shipped_sources_forbid_every_executable_fleet_env_spelling() -> None:
    production = executable_sources(TEMPLATE, SCRIPTS)
    references = {
        str(path.relative_to(ROOT))
        for path in production
        if FLEET_REFERENCE.search(path.read_text(encoding="utf-8"))
    }
    assert references == FLEET_REFERENCE_ALLOWLIST
    violations = {
        str(path.relative_to(ROOT)): match.group(0)
        for path in production
        if (match := executable_fleet_violation(path.read_text(encoding="utf-8")))
    }

    assert violations == {}
    for spelling in (
        'source "$FLEET_ENV"',
        ". '${HERMES_FLEET_ENV}'",
        'builtin source -- "$HOME/.hermes/fleet.env"',
        '{ . "${FLEET_ENV}"; }',
        'eval "$(cat "$FLEET_ENV")"',
        'fleet_loader=source; builtin "$fleet_loader" "$FLEET_ENV"',
        'command source "$FLEET_ENV"',
    ):
        assert executable_fleet_violation(spelling), spelling


def test_executable_command_suffix_is_inside_fleet_reference_inventory(
    tmp_path: Path,
) -> None:
    command = tmp_path / "fleet-escape.command"
    command.write_text('source "$HERMES_FLEET_ENV"\n', encoding="utf-8")
    command.chmod(0o755)

    discovered = executable_sources(tmp_path)

    assert discovered == [command]
    assert executable_fleet_violation(discovered[0].read_text(encoding="utf-8"))


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


def test_backfill_missing_registry_preflight_preserves_valid_fleet(tmp_path: Path) -> None:
    fleet = tmp_path / "fleet.env"
    fleet.write_text("SAFE_KEY=$'unchanged'\n", encoding="utf-8")
    before = fleet.read_bytes()
    env = _clean_env(tmp_path, fleet)
    env.update(
        {
            "HERMES_FLEET_REGISTRY_FILE": str(tmp_path / "missing-registry.yaml"),
            "HERMES_FLEET_OAUTH_FILE": "/would-have-been-written",
            "HERMES_FLEET_CODEX_HOME": "/would-have-been-written",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "registry" in result.stderr.lower()
    assert fleet.read_bytes() == before


def test_backfill_late_invalid_last_target_is_zero_effect(tmp_path: Path) -> None:
    env, paths = _backfill_fixture(tmp_path, invalid_last=True)
    before = {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"}

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert re.search(r"symlink|real directory", result.stderr, re.IGNORECASE)
    assert {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"} == before
    assert not paths["marker"].exists(), "preflight failure must not invoke external processes"


def test_backfill_rejects_role_path_with_symlinked_escaping_ancestor(
    tmp_path: Path,
) -> None:
    env, paths = _backfill_fixture(tmp_path)
    agents = paths["project"] / "agents"
    escaped_agents = tmp_path / "escaped-agents"
    agents.rename(escaped_agents)
    agents.symlink_to(escaped_agents, target_is_directory=True)
    watched = {**paths, "escaped_agents": escaped_agents}
    before = {key: _tree_snapshot(path) for key, path in watched.items() if key != "marker"}

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert re.search(r"escape|contain|symlink", result.stderr, re.IGNORECASE)
    assert {key: _tree_snapshot(path) for key, path in watched.items() if key != "marker"} == before
    assert not paths["marker"].exists()


def test_backfill_batch_preserves_setid_mode_and_exact_ownership(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(SCRIPTS / "backfill-fleet-sot.py"))
    batch_plan = namespace["BatchPlan"]
    destination = tmp_path / "owned.env"
    destination.write_bytes(b"before\n")
    destination.chmod(0o4600)
    before = destination.stat()
    plan = batch_plan(namespace["load_parser"](FLEET_PARSER)._exchange_paths)
    plan.plan_file(destination, b"after\n")

    plan.apply()

    after = destination.stat()
    assert destination.read_bytes() == b"after\n"
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o4600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_backfill_batch_fchown_denial_is_zero_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPTS / "backfill-fleet-sot.py"))
    batch_plan = namespace["BatchPlan"]
    destination = tmp_path / "owned.env"
    destination.write_bytes(b"before\n")
    destination.chmod(0o4600)
    before_bytes = destination.read_bytes()
    before = destination.stat()
    plan = batch_plan(namespace["load_parser"](FLEET_PARSER)._exchange_paths)
    plan.plan_file(destination, b"after\n")

    def reject_ownership(_descriptor: int, _uid: int, _gid: int) -> None:
        raise PermissionError("synthetic backfill ownership denial")

    monkeypatch.setattr(os, "fchown", reject_ownership)
    with pytest.raises(PermissionError, match="synthetic backfill ownership denial"):
        plan.apply()

    after = destination.stat()
    assert destination.read_bytes() == before_bytes
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o4600
    assert list(tmp_path.iterdir()) == [destination]


def test_backfill_loads_the_attested_parser_bytes_without_reopening_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPTS / "backfill-fleet-sot.py"))
    parser_copy = tmp_path / "parse-fleet-env.py"
    parser_copy.write_bytes(FLEET_PARSER.read_bytes())
    original_read = namespace["read_regular_file"]
    load_parser = namespace["load_parser"]

    def snapshot_then_replace(path: Path, label: str):
        snapshot = original_read(path, label)
        path.write_text("raise RuntimeError('mutable parser path reopened')\n", encoding="utf-8")
        return snapshot

    monkeypatch.setitem(load_parser.__globals__, "read_regular_file", snapshot_then_replace)

    loaded = load_parser(parser_copy)

    assert loaded.parse("SAFE=$'snapshot bytes'\n") == [("SAFE", "snapshot bytes")]


def test_backfill_stable_reader_rejects_same_inode_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(SCRIPTS / "backfill-fleet-sot.py"))
    source = tmp_path / "large-source.bin"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    inode = source.stat().st_ino
    real_read = os.read
    mutated = False

    def mutate_after_first_chunk(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            with source.open("r+b") as stream:
                stream.seek(1024 * 1024)
                stream.write(b"b" * 4096)
                stream.flush()
                os.fsync(stream.fileno())
            assert source.stat().st_ino == inode
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_first_chunk)
    with pytest.raises(namespace["BackfillError"], match="changed while it was read"):
        namespace["read_regular_file"](source, "concurrent source")


def test_backfill_repairs_gateway_and_heartbeat_units_transactionally(
    tmp_path: Path,
) -> None:
    env, paths = _backfill_fixture(tmp_path)
    heartbeat = paths["systemd"] / "hermes-first-pm-heartbeat.service"
    timer = paths["systemd"] / "hermes-first-pm-heartbeat.timer"
    heartbeat.write_text("[Service]\nEnvironment=HERMES_HOME=/old-heartbeat\n", encoding="utf-8")
    timer_bytes = b"[Timer]\nUnit=hermes-first-pm-heartbeat.service\n"
    timer.write_bytes(timer_bytes)

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    gateway_text = (paths["systemd"] / "hermes-first-pm-gateway.service").read_text(encoding="utf-8")
    heartbeat_text = heartbeat.read_text(encoding="utf-8")
    for rendered in (gateway_text, heartbeat_text):
        assert 'Environment="HERMES_OAUTH_FILE=/fixture/auth.json"' in rendered
        assert 'Environment="CODEX_HOME=/fixture/codex"' in rendered
    assert timer.read_bytes() == timer_bytes


def test_backfill_rejects_systemd_newline_injection_before_batch_mutation(
    tmp_path: Path,
) -> None:
    env, paths = _backfill_fixture(tmp_path)
    heartbeat = paths["systemd"] / "hermes-first-pm-heartbeat.service"
    heartbeat.write_text("[Service]\nEnvironment=HERMES_HOME=/old-heartbeat\n", encoding="utf-8")
    env["HERMES_FLEET_CODEX_HOME"] = "/safe\nEnvironment=PJAN67_INJECTED=yes"
    before = {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"}

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "systemd" in result.stderr.lower()
    assert {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"} == before
    assert not paths["marker"].exists()


def test_backfill_dry_run_plans_all_targets_without_mutation(tmp_path: Path) -> None:
    env, paths = _backfill_fixture(tmp_path)
    before = {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"}

    result = subprocess.run(
        ["bash", str(SCRIPTS / "backfill-fleet-sot.sh"), "--dry-run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout.lower()
    assert {key: _tree_snapshot(path) for key, path in paths.items() if key != "marker"} == before
    assert not paths["marker"].exists(), "dry-run must not invoke external processes"


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


def test_real_rendered_heartbeat_uses_fleet_selected_hermes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    runtime = role / "runtime"
    runtime.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    shutil.copytree(TEMPLATE / ".scripts", role / ".scripts")
    (role / ".scripts" / "sentinel.prompt.md").write_text("fixture prompt\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        "repo: heartbeat-fixture\n"
        "role: pm\n"
        "agent_id: heartbeat-fixture-pm\n"
        "reconcile:\n  enabled: true\n"
        "ticket_provider:\n  name: plane\n",
        encoding="utf-8",
    )
    marker = tmp_path / "heartbeat-hermes.json"
    fake_hermes = tmp_path / "fleet selected hermes"
    fake_hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['PJAN67_HEARTBEAT_MARKER'], 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(
        "HERMES_FLEET_BIN=$'" + str(fake_hermes).replace("'", "\\'") + "'\n",
        encoding="utf-8",
    )
    env = _clean_env(tmp_path, fleet)
    env["PJAN67_HEARTBEAT_MARKER"] = str(marker)
    env["HEARTBEAT_CHECKPOINT_MIN_INTERVAL_SECONDS"] = "999999"

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "heartbeat.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text(encoding="utf-8"))[0] == "chat"


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
    (role / ".scripts" / "heartbeat.sh").write_text("#!/bin/sh\n# legacy heartbeat\n", encoding="utf-8")
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
    assert (role / ".scripts" / "heartbeat.sh").read_bytes() == (TEMPLATE / ".scripts" / "heartbeat.sh").read_bytes()
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
