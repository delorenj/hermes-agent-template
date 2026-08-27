from __future__ import annotations

import fcntl
import os
import select
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]
TRANSACTION_SOURCE = ROOT / "template" / ".scripts" / "channel-transaction.py"
LOCK_SOURCE = ROOT / "template" / ".scripts" / "lib" / "profile-config-lock.py"


def _fixture(tmp_path: Path) -> dict[str, Path]:
    fleet = tmp_path / "home" / ".hermes"
    profile = fleet / "profiles" / "demo-pm"
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    helper = scripts / "channel-transaction.py"
    profile.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(TRANSACTION_SOURCE, helper)
    shutil.copy2(LOCK_SOURCE, scripts / "lib" / LOCK_SOURCE.name)
    helper.chmod(0o755)

    base = {
        "plugins": {"enabled": ["core-one"]},
        "platforms": {"slack": {"enabled": True}},
        "operator": {"fleet": "preserve"},
    }
    delta = {
        "secrets": {
            "onepassword": {
                "enabled": True,
                "env": {
                    "SLACK_BOT_TOKEN": "op://DeLoSecrets/old/slack_bot_token",
                    "SLACK_APP_TOKEN": "op://DeLoSecrets/old/slack_app_token",
                },
            }
        },
        "platforms": {"slack": {"enabled": True}},
        "operator": {"profile": "original"},
    }
    (fleet / "config.yaml").write_text(
        yaml.safe_dump(base, sort_keys=False), encoding="utf-8"
    )
    delta_path = profile / "config.delta.yaml"
    delta_path.write_text(yaml.safe_dump(delta, sort_keys=False), encoding="utf-8")
    generated = dict(base)
    generated.update(delta)
    generated_path = profile / "config.yaml"
    generated_path.write_text(
        yaml.safe_dump(generated, sort_keys=False), encoding="utf-8"
    )
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        """repo: demo
role: pm
agent_id: demo-pm
profile: demo-pm
slack:
  provisioning_status: verified
  team_id: TOLD
  team_name: Old Workspace
  bot_user_id: UOLDBOT
  bot_id: BOLDBOT
  bot_username: old-pm
""",
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
                        "operator_extension": "original",
                        "slack": {
                            "provisioning_status": "verified",
                            "team_id": "TOLD",
                            "team_name": "Old Workspace",
                            "bot_user_id": "UOLDBOT",
                            "bot_id": "BOLDBOT",
                            "bot_username": "old-pm",
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runtime_env = runtime / ".env"
    runtime_env.write_text('SLACK_ALLOWED_USERS="UOLD"\n', encoding="utf-8")
    marker = scripts / ".done-31-slack"
    marker.write_bytes(b"original-marker\n")
    return {
        "fleet": fleet,
        "profile": profile,
        "role": role,
        "helper": helper,
        "delta": delta_path,
        "generated": generated_path,
        "role_yaml": role_yaml,
        "registry": registry,
        "runtime_env": runtime_env,
        "marker": marker,
        "other_marker": scripts / ".done-30-telegram",
        "journal": profile.parent / ".demo-pm.channel-transaction" / "journal.json",
    }


def _command(paths: dict[str, Path], *, prepare: bool = False) -> list[str]:
    common = [
        sys.executable,
        "-I",
        str(paths["helper"]),
        "--channel",
        "slack",
        "--profile",
        str(paths["profile"]),
        "--role-yaml",
        str(paths["role_yaml"]),
        "--registry",
        str(paths["registry"]),
        "--runtime-env",
        str(paths["runtime_env"]),
        "--done-marker",
        str(paths["marker"]),
        "--agent-id",
        "demo-pm",
        "--role-dir",
        str(paths["role"]),
        "--profile-name",
        "demo-pm",
    ]
    if prepare:
        return [*common, "--prepare-unconfigured"]
    return [
        *common,
        "--allowed-value",
        "UNEW",
        "--reference",
        "SLACK_BOT_TOKEN",
        "op://DeLoSecrets/new/slack_bot_token",
        "--reference",
        "SLACK_APP_TOKEN",
        "op://DeLoSecrets/new/slack_app_token",
        "--metadata",
        "provisioning_status",
        "verified",
        "--metadata",
        "team_id",
        "TNEW",
        "--metadata",
        "team_name",
        "New Workspace",
        "--metadata",
        "bot_user_id",
        "UNEWBOT",
        "--metadata",
        "bot_id",
        "BNEWBOT",
        "--metadata",
        "bot_username",
        "new-pm",
    ]


def _instrument(paths: dict[str, Path]) -> None:
    source = paths["helper"].read_text(encoding="utf-8")
    needle = "    # TEST_FIXTURE_FAULT_BOUNDARY\n"
    assert source.count(needle) == 1
    harness = """\
    external_source = os.environ.get("PJANGLER_TEST_EXTERNAL_SOURCE")
    external_target = os.environ.get("PJANGLER_TEST_EXTERNAL_TARGET")
    if _label == "prepared" and external_source and external_target:
        os.replace(external_source, external_target)
    if os.environ.get("PJANGLER_TEST_RAISE_LABEL") == _label:
        raise RuntimeError("injected transaction failure")
    selected = os.environ.get("PJANGLER_TEST_FAULT_LABEL")
    if selected == _label:
        mode = os.environ.get("PJANGLER_TEST_FAULT_MODE")
        if mode == "kill":
            os.kill(os.getpid(), 9)
        if mode == "pause":
            ready_fd = int(os.environ["PJANGLER_TEST_READY_FD"])
            resume_fd = int(os.environ["PJANGLER_TEST_RESUME_FD"])
            os.write(ready_fd, (_label + "\\n").encode("utf-8"))
            if os.read(resume_fd, 1) != b"1":
                raise RuntimeError("test harness resume protocol failed")
"""
    paths["helper"].write_text(source.replace(needle, harness), encoding="utf-8")


def _start_paused(
    paths: dict[str, Path], label: str
) -> tuple[subprocess.Popen[str], int, int, int, int]:
    ready_read, ready_write = os.pipe()
    resume_read, resume_write = os.pipe()
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_FAULT_LABEL": label,
            "PJANGLER_TEST_FAULT_MODE": "pause",
            "PJANGLER_TEST_READY_FD": str(ready_write),
            "PJANGLER_TEST_RESUME_FD": str(resume_read),
        }
    )
    process = subprocess.Popen(
        _command(paths),
        env=env,
        pass_fds=(ready_write, resume_read),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(resume_read)
    readable, _, _ = select.select([ready_read], [], [], 8)
    if not readable:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(f"process did not reach {label}: {stdout}\n{stderr}")
    observed = os.read(ready_read, 4096).decode("utf-8").strip()
    assert observed == label
    return process, ready_read, resume_write, ready_write, resume_read


def _finish_paused(
    process: subprocess.Popen[str], ready_read: int, resume_write: int
) -> tuple[str, str]:
    os.write(resume_write, b"1")
    os.close(resume_write)
    os.close(ready_read)
    return process.communicate(timeout=10)


def _target_state(paths: dict[str, Path]) -> dict[str, tuple[bool, bytes, int, int]]:
    result: dict[str, tuple[bool, bytes, int, int]] = {}
    for key in (
        "delta",
        "generated",
        "role_yaml",
        "registry",
        "runtime_env",
        "marker",
        "other_marker",
    ):
        path = paths[key]
        if path.exists():
            info = os.lstat(path)
            result[key] = (
                True,
                path.read_bytes(),
                stat.S_IMODE(info.st_mode),
                info.st_ino,
            )
        else:
            result[key] = (False, b"", 0, 0)
    return result


def _assert_state_exact(
    paths: dict[str, Path], expected: dict[str, tuple[bool, bytes, int, int]]
) -> None:
    assert _target_state(paths) == expected


def test_out_of_band_edit_after_snapshot_is_preserved_with_conflict_evidence(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    process, ready_read, resume_write, _unused_one, _unused_two = _start_paused(
        paths, "prepared"
    )
    operator = b"operator: newer\nplatforms:\n  slack:\n    enabled: false\n"
    replacement = paths["delta"].with_name("operator-delta.yaml")
    replacement.write_bytes(operator)
    operator_inode = replacement.stat().st_ino
    os.replace(replacement, paths["delta"])
    stdout, stderr = _finish_paused(process, ready_read, resume_write)

    assert process.returncode != 0, stdout + stderr
    assert paths["delta"].read_bytes() == operator
    assert paths["delta"].stat().st_ino == operator_inode
    for key in ("generated", "role_yaml", "registry", "runtime_env", "marker"):
        assert _target_state(paths)[key] == before[key]
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "conflict"
    assert "delta:external-capture-reversed" in journal["conflicts"]
    assert "external state preserved" in stderr


def test_rollback_compare_and_swap_cannot_erase_newer_edit(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    process, ready_read, resume_write, _unused_one, _unused_two = _start_paused(
        paths, "write:0:disabled-delta"
    )
    operator = b"operator: arrived-after-transaction-write\n"
    replacement = paths["delta"].with_name("operator-delta.yaml")
    replacement.write_bytes(operator)
    operator_inode = replacement.stat().st_ino
    os.replace(replacement, paths["delta"])
    stdout, stderr = _finish_paused(process, ready_read, resume_write)

    assert process.returncode != 0, stdout + stderr
    assert paths["delta"].read_bytes() == operator
    assert paths["delta"].stat().st_ino == operator_inode
    for key in ("generated", "role_yaml", "registry", "runtime_env", "marker"):
        assert _target_state(paths)[key] == before[key]
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "conflict"
    assert "delta:ambiguous-inflight-topology" in journal["conflicts"]


def test_external_replace_between_rollback_check_and_syscall_is_reversed(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    ready_read, ready_write = os.pipe()
    resume_read, resume_write = os.pipe()
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_RAISE_LABEL": "write:0:disabled-delta",
            "PJANGLER_TEST_FAULT_LABEL": "rollback-intent:0:delta",
            "PJANGLER_TEST_FAULT_MODE": "pause",
            "PJANGLER_TEST_READY_FD": str(ready_write),
            "PJANGLER_TEST_RESUME_FD": str(resume_read),
        }
    )
    process = subprocess.Popen(
        _command(paths),
        env=env,
        pass_fds=(ready_write, resume_read),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(resume_read)
    readable, _, _ = select.select([ready_read], [], [], 8)
    assert readable, process.communicate(timeout=1)
    assert os.read(ready_read, 4096).decode().strip() == "rollback-intent:0:delta"

    operator = b"operator: raced-the-rollback-cas\n"
    replacement = paths["delta"].with_name("operator-delta.yaml")
    replacement.write_bytes(operator)
    operator_inode = replacement.stat().st_ino
    os.replace(replacement, paths["delta"])
    stdout, stderr = _finish_paused(process, ready_read, resume_write)

    assert process.returncode != 0, stdout + stderr
    assert paths["delta"].read_bytes() == operator
    assert paths["delta"].stat().st_ino == operator_inode
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "conflict"
    assert "delta:rollback-cas-reversed" in journal["conflicts"]


@pytest.mark.parametrize(
    ("index", "label"),
    [
        (0, "disabled-delta"),
        (1, "disabled-generated"),
        (2, "runtime-policy"),
        (3, "role-identity"),
        (4, "registry-identity"),
        (5, "enabled-delta"),
        (6, "enabled-generated"),
        (7, "completion-marker"),
    ],
)
def test_sigkill_after_each_write_recovers_exact_original_generation(
    tmp_path: Path, index: int, label: str
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_FAULT_LABEL": f"write:{index}:{label}",
            "PJANGLER_TEST_FAULT_MODE": "kill",
        }
    )
    crashed = subprocess.run(
        _command(paths), env=env, text=True, capture_output=True, check=False
    )
    assert crashed.returncode == -9
    assert paths["journal"].is_file()

    recovered = subprocess.run(
        _command(paths, prepare=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert recovered.returncode == 3, recovered.stdout + recovered.stderr
    _assert_state_exact(paths, before)
    assert not paths["journal"].parent.exists()


@pytest.mark.parametrize("boundary", ["intent", "capture", "commit"])
@pytest.mark.parametrize(
    ("index", "label"),
    [
        (0, "disabled-delta"),
        (1, "disabled-generated"),
        (2, "runtime-policy"),
        (3, "role-identity"),
        (4, "registry-identity"),
        (5, "enabled-delta"),
        (6, "enabled-generated"),
        (7, "completion-marker"),
    ],
)
def test_sigkill_at_every_forward_atomic_boundary_recovers_original_generation(
    tmp_path: Path, boundary: str, index: int, label: str
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_FAULT_LABEL": f"{boundary}:{index}:{label}",
            "PJANGLER_TEST_FAULT_MODE": "kill",
        }
    )
    crashed = subprocess.run(
        _command(paths), env=env, text=True, capture_output=True, check=False
    )
    assert crashed.returncode == -9

    recovered = subprocess.run(
        _command(paths, prepare=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert recovered.returncode == 3, recovered.stdout + recovered.stderr
    _assert_state_exact(paths, before)
    assert not paths["journal"].parent.exists()


@pytest.mark.parametrize(
    "boundary", ["reverse-intent:0:disabled-delta", "reverse:0:disabled-delta"]
)
def test_sigkill_at_external_capture_reversal_preserves_external_inode(
    tmp_path: Path, boundary: str
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    external = paths["delta"].with_name("operator-delta.yaml")
    external.write_bytes(b"operator: atomic-external-generation\n")
    external_inode = external.stat().st_ino
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_EXTERNAL_SOURCE": str(external),
            "PJANGLER_TEST_EXTERNAL_TARGET": str(paths["delta"]),
            "PJANGLER_TEST_FAULT_LABEL": boundary,
            "PJANGLER_TEST_FAULT_MODE": "kill",
        }
    )
    crashed = subprocess.run(
        _command(paths), env=env, text=True, capture_output=True, check=False
    )
    assert crashed.returncode == -9

    recovered = subprocess.run(
        _command(paths, prepare=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert recovered.returncode != 0
    assert paths["delta"].read_bytes() == b"operator: atomic-external-generation\n"
    assert paths["delta"].stat().st_ino == external_inode
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "conflict"
    assert any("external-capture" in item for item in journal["conflicts"])


@pytest.mark.parametrize(
    "boundary",
    [
        "rollback-intent:0:delta",
        "rollback-capture:0:delta",
        "rollback-commit:0:delta",
    ],
)
def test_sigkill_at_rollback_atomic_boundaries_recovers_original_generation(
    tmp_path: Path, boundary: str
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_RAISE_LABEL": "write:0:disabled-delta",
            "PJANGLER_TEST_FAULT_LABEL": boundary,
            "PJANGLER_TEST_FAULT_MODE": "kill",
        }
    )
    crashed = subprocess.run(
        _command(paths), env=env, text=True, capture_output=True, check=False
    )
    assert crashed.returncode == -9

    recovered = subprocess.run(
        _command(paths, prepare=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert recovered.returncode == 3, recovered.stdout + recovered.stderr
    _assert_state_exact(paths, before)
    assert not paths["journal"].parent.exists()


def test_cleanup_atomic_capture_retains_new_unknown_artifact(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    process, ready_read, resume_write, _unused_one, _unused_two = _start_paused(
        paths, "cleanup-before-exchange"
    )
    candidate = paths["journal"].parent / "candidate-00"
    assert candidate.is_file()
    replacement = candidate.with_name("external-cleanup-artifact")
    replacement.write_bytes(b"external: arrived-during-cleanup\n")
    external_inode = replacement.stat().st_ino
    os.replace(replacement, candidate)
    stdout, stderr = _finish_paused(process, ready_read, resume_write)

    assert process.returncode != 0, stdout + stderr
    assert candidate.read_bytes() == b"external: arrived-during-cleanup\n"
    assert candidate.stat().st_ino == external_inode
    assert paths["journal"].is_file()
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "committed"
    assert "retained" in stderr


def test_preexisting_hardlink_alias_fails_before_mutation(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    before = _target_state(paths)
    alias = paths["delta"].with_name("operator-alias.yaml")
    os.link(paths["delta"], alias)
    result = subprocess.run(
        _command(paths), text=True, capture_output=True, check=False, timeout=10
    )

    assert result.returncode != 0
    assert "already has a hard-link alias" in result.stderr
    _assert_state_exact(paths, before)
    assert alias.read_bytes() == paths["delta"].read_bytes()
    assert alias.stat().st_ino == paths["delta"].stat().st_ino
    assert not paths["journal"].parent.exists()


def test_preparing_recovery_never_deletes_unknown_recovery_inode(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    _instrument(paths)
    before = _target_state(paths)
    env = os.environ.copy()
    env.update(
        {
            "PJANGLER_TEST_FAULT_LABEL": "recovery-link:delta",
            "PJANGLER_TEST_FAULT_MODE": "kill",
        }
    )
    crashed = subprocess.run(
        _command(paths), env=env, text=True, capture_output=True, check=False
    )
    assert crashed.returncode == -9
    recovery = paths["journal"].parent / "recovery-delta"
    assert recovery.is_file()
    replacement = recovery.with_name("external-recovery-inode")
    replacement.write_bytes(b"external: must-not-be-discarded\n")
    external_inode = replacement.stat().st_ino
    os.replace(replacement, recovery)

    result = subprocess.run(
        _command(paths, prepare=True),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    _assert_state_exact(paths, before)
    assert recovery.read_bytes() == b"external: must-not-be-discarded\n"
    assert recovery.stat().st_ino == external_inode
    journal = yaml.safe_load(paths["journal"].read_text(encoding="utf-8"))
    assert journal["status"] == "conflict"
    assert "preparing:unsafe-recovery-artifact" in journal["conflicts"]


def test_direct_helper_times_out_on_registry_before_creating_profile_lock(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    lock_path = Path(f"{paths['registry']}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    env = os.environ.copy()
    env["HERMES_REGISTRY_LOCK_TIMEOUT_SECONDS"] = "0.1"
    try:
        blocked = subprocess.run(
            _command(paths), env=env, text=True, capture_output=True, check=False, timeout=3
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert blocked.returncode != 0
    assert "timed out waiting for registry lock" in blocked.stderr
    assert not (paths["profile"].parent / ".demo-pm.config.lock").exists()


def test_direct_helper_holds_registry_while_waiting_for_profile_lock(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    profile_lock = paths["profile"].parent / ".demo-pm.config.lock"
    profile_descriptor = os.open(profile_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(profile_descriptor, fcntl.LOCK_EX)
    env = os.environ.copy()
    env["HERMES_REGISTRY_LOCK_TIMEOUT_SECONDS"] = "2"
    env["HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS"] = "2"
    process = subprocess.Popen(
        _command(paths), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    registry_lock = Path(f"{paths['registry']}.lock")
    deadline = time.monotonic() + 2
    observed_order = False
    while time.monotonic() < deadline and process.poll() is None:
        if registry_lock.exists():
            probe = os.open(registry_lock, os.O_RDWR)
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    observed_order = True
                    break
                else:
                    fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)
        time.sleep(0.01)
    assert observed_order, "helper never held registry while blocked on profile lock"
    assert process.poll() is None
    fcntl.flock(profile_descriptor, fcntl.LOCK_UN)
    os.close(profile_descriptor)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr
