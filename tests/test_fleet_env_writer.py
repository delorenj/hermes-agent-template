from __future__ import annotations

import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
TEMPLATE_SCRIPTS = ROOT / "template" / ".scripts"
HEADER = b"PJANGLER_FLEET_ENV_V1"
FOOTER = b"PJANGLER_FLEET_ENV_END"


def _render_scripts(tmp_path: Path) -> tuple[Path, Path]:
    role_dir = tmp_path / "project" / "agents" / "hermes" / "pm"
    scripts = role_dir / ".scripts"
    shutil.copytree(TEMPLATE_SCRIPTS, scripts)
    (role_dir / "role.yaml").write_text(
        """role: pm
repo: fleet-writer-fixture
agent_id: fleet-writer-fixture-pm
display_name: Fleet Writer Fixture
profile: fleet-writer-fixture-pm
telegram:
  bot_username: ""
plane:
  workspace: ""
runtime:
  github_owner: fixture
  github_repo: fleet-writer-fixture
""",
        encoding="utf-8",
    )
    return role_dir, scripts


def _run_writer(scripts: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(scripts / "05-fleet-env.sh")],
        cwd=scripts.parent,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_records(parser: Path, fleet: Path, environment: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(
        ["python3", "-I", str(parser), str(fleet)],
        env=environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    records = result.stdout.split(b"\0")
    assert records[0] == HEADER
    assert records[-3:] == [FOOTER, b"", b""]
    return {
        record.split(b"=", 1)[0].decode(): record.split(b"=", 1)[1].decode()
        for record in records[1:-3]
    }


def _upsert(
    parser: Path,
    fleet: Path,
    key: str,
    value: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["python3", "-I", str(parser), "--upsert", str(fleet), key, value],
        capture_output=True,
        check=False,
    )


def test_systemd_environment_serializer_is_lossless_and_rejects_record_injection() -> None:
    namespace = runpy.run_path(str(TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"))
    serialize = namespace["serialize_systemd_environment"]
    value = '/path with spaces/"quote"/back\\slash/%token/$dollar\tend'

    rendered = serialize("CODEX_HOME", value)

    assert rendered == (
        'Environment="CODEX_HOME=/path with spaces/\\"quote\\"/'
        'back\\\\slash/%%token/$dollar\\tend"'
    )
    for unsafe in ("line one\nline two", "carriage\rreturn", "nul\0byte"):
        with pytest.raises(ValueError, match="control|newline|NUL"):
            serialize("CODEX_HOME", unsafe)
    with pytest.raises(ValueError, match="name"):
        serialize("NOT-AN-ENV-NAME", "value")

    serialize_exec = namespace["serialize_systemd_exec_value"]
    assert serialize_exec(
        '/space x/${EXPAND}/"quote"/back\\slash/%token/$plain'
    ) == '"/space x/$${EXPAND}/\\"quote\\"/back\\\\slash/%%token/$$plain"'


def test_writer_round_trips_every_value_through_canonical_literal_format(
    tmp_path: Path,
) -> None:
    role_dir, scripts = _render_scripts(tmp_path)
    fleet = tmp_path / "fleet state" / "fleet.env"
    initial = {
        "HERMES_FLEET_BIN": "/tools/hermes with spaces/$literal;and=equals",
        "HERMES_FLEET_REPO": "/repo/'single'/\"double\"/back\\slash\nline two\n",
        "HERMES_FLEET_REGISTRY_FILE": "/registry/[glob]*? & pipe|semi;",
        "HERMES_FLEET_OAUTH_FILE": "/auth/${HOME}/literal",
        "HERMES_FLEET_CODEX_HOME": "first line\nsecond line\n",
    }
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "HERMES_FLEET_ENV": str(fleet),
        "HERMES_TEMPLATE_CONFIG": str(tmp_path / "config.toml"),
        "HERMES_BIN": initial["HERMES_FLEET_BIN"],
        "HERMES_AGENT_REPO": initial["HERMES_FLEET_REPO"],
        "REGISTRY_FILE": initial["HERMES_FLEET_REGISTRY_FILE"],
        "HERMES_OAUTH_FILE": initial["HERMES_FLEET_OAUTH_FILE"],
        "CODEX_HOME": initial["HERMES_FLEET_CODEX_HOME"],
        "PJANGLER_BIN": "/fixture/bin/pj",
        "SKIP_PLANE": "1",
    }

    created = _run_writer(scripts, environment)
    assert created.returncode == 0, created.stderr
    assert _parse_records(scripts / "lib" / "parse-fleet-env.py", fleet, environment) == initial

    assignment_lines = [
        line for line in fleet.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(assignment_lines) == len(initial)
    for key in initial:
        assert any(line.startswith(f"{key}=$'") for line in assignment_lines)

    refreshed = {
        "HERMES_FLEET_OAUTH_FILE": "/new auth/with 'quotes' and $dollar\\tail",
        "HERMES_FLEET_CODEX_HOME": "new\nmultiline\nvalue\n",
    }
    environment["HERMES_OAUTH_FILE"] = refreshed["HERMES_FLEET_OAUTH_FILE"]
    environment["CODEX_HOME"] = refreshed["HERMES_FLEET_CODEX_HOME"]
    rerun = _run_writer(scripts, environment)
    assert rerun.returncode == 0, rerun.stderr

    expected = {**initial, **refreshed}
    assert _parse_records(scripts / "lib" / "parse-fleet-env.py", fleet, environment) == expected
    text = fleet.read_text(encoding="utf-8")
    for key in expected:
        assert text.count(f"{key}=") == 1
    assert (role_dir / ".scripts" / ".done-05-fleet-env").is_file()


def test_upsert_recognizes_all_accepted_assignment_prefixes_and_preserves_mode(
    tmp_path: Path,
) -> None:
    fleet = tmp_path / "fleet.env"
    fleet.write_text(
        "# operator comment\n"
        "  export HERMES_FLEET_OAUTH_FILE='old value' # legacy spelling\n"
        "SAFE_KEY=keep\n",
        encoding="utf-8",
    )
    fleet.chmod(0o600)
    before = fleet.stat()

    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    result = _upsert(
        parser,
        fleet,
        "HERMES_FLEET_OAUTH_FILE",
        "new value $'\\\n",
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    parsed = _parse_records(parser, fleet, os.environ.copy())
    assert parsed == {
        "HERMES_FLEET_OAUTH_FILE": "new value $'\\\n",
        "SAFE_KEY": "keep",
    }
    text = fleet.read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^[ \t]*(?:export[ \t]+)?HERMES_FLEET_OAUTH_FILE=", text)) == 1
    after = fleet.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_upsert_applies_ownership_before_restoring_exact_setid_mode(
    tmp_path: Path,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    fleet.chmod(0o4600)
    before = fleet.stat()

    result = _upsert(parser, fleet, "KEY", "replacement")

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    after = fleet.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o4600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert _parse_records(parser, fleet, os.environ.copy()) == {"KEY": "replacement"}


def test_upsert_ownership_failure_is_zero_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    fleet.chmod(0o600)
    before_bytes = fleet.read_bytes()
    before = fleet.stat()
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]

    def reject_ownership(_descriptor: int, _uid: int, _gid: int) -> None:
        raise PermissionError("synthetic ownership denial")

    monkeypatch.setattr(os, "fchown", reject_ownership)
    with pytest.raises(PermissionError, match="synthetic ownership denial"):
        atomic_upsert(fleet, "KEY", "replacement")

    after = fleet.stat()
    assert fleet.read_bytes() == before_bytes
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert list(tmp_path.iterdir()) == [fleet]


def test_upsert_rejects_duplicates_and_symlinks_without_corruption(
    tmp_path: Path,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    duplicate = tmp_path / "duplicate.env"
    duplicate.write_text("KEY=one\n  export KEY='two'\n", encoding="utf-8")
    duplicate_before = duplicate.read_bytes()
    duplicate_result = _upsert(parser, duplicate, "KEY", "replacement")
    assert duplicate_result.returncode == 2
    assert duplicate_result.stdout == b""
    assert duplicate.read_bytes() == duplicate_before

    real = tmp_path / "real.env"
    real.write_text("KEY=original\n", encoding="utf-8")
    link = tmp_path / "linked.env"
    link.symlink_to(real)
    link_result = _upsert(parser, link, "KEY", "replacement")
    assert link_result.returncode == 2
    assert link_result.stdout == b""
    assert real.read_text(encoding="utf-8") == "KEY=original\n"
    assert link.is_symlink()


def test_upsert_failed_replace_leaves_original_and_no_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    before = fleet.read_bytes()
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]

    def reject_exchange(_source: object, _destination: object) -> None:
        raise OSError("synthetic exchange failure")

    monkeypatch.setitem(atomic_upsert.__globals__, "_exchange_paths", reject_exchange)
    with pytest.raises(OSError, match="synthetic exchange failure"):
        atomic_upsert(fleet, "KEY", "replacement")

    assert fleet.read_bytes() == before
    assert list(tmp_path.iterdir()) == [fleet]


def test_upsert_preserves_a_replacement_racing_the_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    replacement = tmp_path / "replacement.env"
    replacement_bytes = b"KEY=concurrent-replacement\n"
    replacement.write_bytes(replacement_bytes)
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]
    real_exchange = namespace["_exchange_paths"]
    calls = 0

    def replace_then_exchange(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.replace(replacement, fleet)
        real_exchange(source, destination)

    monkeypatch.setitem(
        atomic_upsert.__globals__, "_exchange_paths", replace_then_exchange
    )
    with pytest.raises(OSError, match="destination changed"):
        atomic_upsert(fleet, "KEY", "our-update")

    assert calls == 2, "the atomic exchange must be reversed after mismatch"
    assert fleet.read_bytes() == replacement_bytes
    assert list(tmp_path.iterdir()) == [fleet]


def test_upsert_preserves_same_inode_content_mutation_racing_commit_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    original_inode = fleet.stat().st_ino
    concurrent_bytes = b"KEY=concurrent-same-inode\n"
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]
    real_exchange = namespace["_exchange_paths"]
    calls = 0

    def modify_same_inode_then_exchange(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            with fleet.open("wb") as stream:
                stream.write(concurrent_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            assert fleet.stat().st_ino == original_inode
        real_exchange(source, destination)

    monkeypatch.setitem(
        atomic_upsert.__globals__, "_exchange_paths", modify_same_inode_then_exchange
    )
    with pytest.raises(OSError, match="destination changed"):
        atomic_upsert(fleet, "KEY", "our-update")

    assert calls == 2, "a same-inode content mismatch must reverse the atomic exchange"
    assert fleet.read_bytes() == concurrent_bytes
    assert fleet.stat().st_ino == original_inode
    assert list(tmp_path.iterdir()) == [fleet]


def test_upsert_reverse_exchange_failure_preserves_displaced_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    replacement = tmp_path / "replacement.env"
    replacement_bytes = b"KEY=concurrent-replacement\n"
    replacement.write_bytes(replacement_bytes)
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]
    real_exchange = namespace["_exchange_paths"]
    calls = 0

    def replace_then_block_recovery(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.replace(replacement, fleet)
            real_exchange(source, destination)
            return
        raise OSError("synthetic reverse exchange failure")

    monkeypatch.setitem(
        atomic_upsert.__globals__, "_exchange_paths", replace_then_block_recovery
    )
    with pytest.raises(
        OSError,
        match="^fleet environment destination changed and recovery failed; concurrent data preserved$",
    ) as captured:
        atomic_upsert(fleet, "KEY", "our-update")

    recovery_path = getattr(captured.value, "recovery_path", None)
    assert isinstance(recovery_path, Path), "operator recovery metadata must identify the preserved inode"
    assert recovery_path.parent == tmp_path
    assert recovery_path.name.startswith(".fleet.env.pjangler-recovery-")
    assert recovery_path.read_bytes() == replacement_bytes
    assert stat.S_IMODE(recovery_path.stat().st_mode) == 0o600
    assert fleet.read_text(encoding="utf-8") == "KEY=$'our-update'\n"


def test_upsert_recovery_cleanup_failure_never_deletes_displaced_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = TEMPLATE_SCRIPTS / "lib" / "parse-fleet-env.py"
    fleet = tmp_path / "fleet.env"
    fleet.write_text("KEY=original\n", encoding="utf-8")
    replacement = tmp_path / "replacement.env"
    replacement_bytes = b"KEY=concurrent-replacement\n"
    replacement.write_bytes(replacement_bytes)
    namespace = runpy.run_path(str(parser))
    atomic_upsert = namespace["atomic_upsert"]
    real_exchange = namespace["_exchange_paths"]
    calls = 0

    def replace_then_block_recovery(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.replace(replacement, fleet)
            real_exchange(source, destination)
            return
        raise OSError("synthetic reverse exchange failure")

    monkeypatch.setitem(
        atomic_upsert.__globals__, "_exchange_paths", replace_then_block_recovery
    )
    monkeypatch.setitem(atomic_upsert.__globals__, "_remove_recovery_temporary", lambda _path: False)
    with pytest.raises(OSError) as captured:
        atomic_upsert(fleet, "KEY", "our-update")

    recovery_path = getattr(captured.value, "recovery_path", None)
    assert isinstance(recovery_path, Path)
    assert recovery_path.read_bytes() == replacement_bytes
    preserved = [path for path in tmp_path.iterdir() if path.read_bytes() == replacement_bytes]
    assert preserved, "cleanup failure must retain at least one name for the displaced bytes"
