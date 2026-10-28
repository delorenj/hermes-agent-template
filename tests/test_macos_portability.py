"""macOS (Darwin) portability of the provisioning scaffold, proven on Linux.

macOS has no flock(1), no sha256sum, bash 3.2 at /bin/bash, a BSD realpath/sed,
no /proc, no renameat2 and launchd instead of systemd. Each primitive that used
to assume one of those now has a portable path; these tests exercise the
portable path on this host (forcing it where the Linux tool would be chosen) so
a regression shows up here and not on somebody's Mac.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "template" / ".scripts"
LIB = SCRIPTS / "lib"
FLOCK = shutil.which("flock")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _bash(script: str, env: dict[str, str] | None = None, *args: str):
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def _functions(source: Path, *names: str) -> str:
    """The text of named shell functions from a script (single-line closing brace form)."""
    lines = source.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for name in names:
        start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
        end = next(i for i in range(start, len(lines)) if lines[i] == "}")
        out.extend(lines[start : end + 1])
    return "\n".join(out)


# --------------------------------------------------------------------------- lock


def _lock_role(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copytree(LIB, scripts / "lib")
    shutil.copy2(SCRIPTS / "_lib.sh", scripts / "_lib.sh")
    (role / "role.yaml").write_text("repo: demo\nrole: pm\nagent_id: demo-pm\nprofile: demo-pm\n")
    home = tmp_path / "home"
    home.mkdir()
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text("schema_version: 1\nagents: {}\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
        "REGISTRY_FILE": str(registry),
    }
    return role, env


@pytest.mark.skipif(FLOCK is None, reason="needs flock(1) to prove interoperability")
def test_python_fleet_lock_excludes_flock1_on_the_same_lock_file(tmp_path: Path) -> None:
    role, env = _lock_role(tmp_path)
    env["HERMES_FD_LOCK"] = "python"
    script = textwrap.dedent(
        """
        source "$1/.scripts/_lib.sh"
        fleet_lock_acquire
        flock -n "${REGISTRY_FILE}.lock" true && echo FREE_WHILE_HELD || echo HELD_WHILE_HELD
        fleet_lock_release
        flock -n "${REGISTRY_FILE}.lock" true && echo FREE_AFTER || echo HELD_AFTER
        """
    )
    result = _bash(script, env, str(role))
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["HELD_WHILE_HELD", "FREE_AFTER"]


@pytest.mark.skipif(FLOCK is None, reason="needs flock(1) to hold the competing lock")
def test_python_fleet_lock_waits_for_and_times_out_on_a_flock1_holder(tmp_path: Path) -> None:
    role, env = _lock_role(tmp_path)
    env["HERMES_FD_LOCK"] = "python"
    lock = Path(env["REGISTRY_FILE"] + ".lock")
    lock.touch()
    holder = subprocess.Popen([FLOCK, str(lock), "sleep", "2"])
    try:
        time.sleep(0.3)
        script = 'source "$1/.scripts/_lib.sh"; fleet_lock_acquire; echo ACQUIRED'
        started = time.monotonic()
        timed_out = _bash(script, {**env, "FLEET_LOCK_TIMEOUT_SECONDS": "1"}, str(role))
        assert timed_out.returncode != 0 and "timed out waiting for fleet registry lock" in timed_out.stderr
        assert time.monotonic() - started < 1.9  # gave up on its own deadline, not the holder's
        waited = _bash(script, {**env, "FLEET_LOCK_TIMEOUT_SECONDS": "10"}, str(role))
        assert waited.returncode == 0, waited.stderr
        assert "ACQUIRED" in waited.stdout
    finally:
        holder.kill()
        holder.wait()


def test_fleet_lock_works_with_no_flock_binary_at_all(tmp_path: Path) -> None:
    """The macOS shape: flock(1) is not on PATH, the helper is the only lock."""
    role, env = _lock_role(tmp_path)
    tools = tmp_path / "bin"
    tools.mkdir()
    # Every tool _lib.sh needs, except flock.
    for name in ("python3", "dirname", "mkdir", "chmod", "date", "git", "cat", "sleep", "uname", "env", "rm", "mktemp", "touch", "grep", "sed", "id", "tr", "head"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    env["PATH"] = str(tools)
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-c",
         'source "$1/.scripts/_lib.sh"; command -v flock >/dev/null && echo HAS_FLOCK; fleet_lock_acquire; echo LOCKED; fleet_lock_release; echo RELEASED',
         "bash", str(role)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["LOCKED", "RELEASED"]


def test_fd_lock_helper_rejects_bad_input(tmp_path: Path) -> None:
    helper = LIB / "fd-lock.py"
    for argv in (["lock"], ["lock", "x", "1"], ["lock", "9", "nan"], ["lock", "9", "-1"], ["frob", "1"]):
        result = subprocess.run([sys.executable, "-I", str(helper), *argv], capture_output=True, text=True)
        assert result.returncode == 2, argv
    # a descriptor that is not a regular file is refused (never lock a pipe or tty by accident)
    result = subprocess.run([sys.executable, "-I", str(helper), "lock", "0", "1"], stdin=subprocess.PIPE, capture_output=True, text=True)
    assert result.returncode == 2 and "regular file" in result.stderr


# ------------------------------------------------------------------ small helpers


def test_sha256_hex_matches_hashlib_with_every_fallback(tmp_path: Path) -> None:
    body = _functions(SCRIPTS / "_lib.sh", "sha256_hex")
    payload = "/tmp/some project/.project.json"
    expected = hashlib.sha256(payload.encode()).hexdigest()
    default = _bash(f'{body}\nprintf %s "$1" | sha256_hex', None, payload)
    assert default.stdout.strip() == expected

    # shasum only (macOS): no sha256sum on PATH
    only_shasum = tmp_path / "shasum-bin"
    only_shasum.mkdir()
    shasum = only_shasum / "shasum"
    shasum.write_text('#!/bin/sh\n[ "$1 $2" = "-a 256" ] || exit 9\nexec python3 -c \'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+"  -")\'\n')
    shasum.chmod(0o755)
    (only_shasum / "python3").symlink_to(shutil.which("python3"))
    via_shasum = subprocess.run(
        [shutil.which("bash"), "-c", f'{body}\nprintf %s "$1" | sha256_hex', "bash", payload],
        env={"PATH": str(only_shasum)}, text=True, capture_output=True,
    )
    assert via_shasum.stdout.strip() == expected, via_shasum.stderr

    # neither: python fallback
    neither = tmp_path / "neither-bin"
    neither.mkdir()
    (neither / "python3").symlink_to(shutil.which("python3"))
    via_python = subprocess.run(
        [shutil.which("bash"), "-c", f'{body}\nprintf %s "$1" | sha256_hex', "bash", payload],
        env={"PATH": str(neither)}, text=True, capture_output=True,
    )
    assert via_python.stdout.strip() == expected, via_python.stderr


def test_path_relative_to_replaces_gnu_realpath_relative_to(tmp_path: Path) -> None:
    body = _functions(SCRIPTS / "_lib.sh", "path_relative_to")
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    role.mkdir(parents=True)
    link = tmp_path / "link-to-project"
    link.symlink_to(project)
    result = _bash(f'{body}\npath_relative_to "$1" "$2"', None, str(link), str(role))
    assert result.stdout.strip() == "agents/hermes/pm"
    gnu = shutil.which("realpath")
    if gnu:
        expected = subprocess.run([gnu, f"--relative-to={link}", str(role)], text=True, capture_output=True)
        if expected.returncode == 0:
            assert result.stdout.strip() == expected.stdout.strip()


# ------------------------------------------------------------------------ bash guard


def _simulate_old_bash(guard: Path, destination: Path, *, reject_all_candidates: bool) -> Path:
    """The guard with its version thresholds raised so THIS bash counts as too old."""
    text = guard.read_text(encoding="utf-8").replace('"${BASH_VERSINFO[0]}" -lt 4', '"${BASH_VERSINFO[0]}" -lt 99')
    if reject_all_candidates:
        text = text.replace('-gt 4 ]', '-gt 99 ]')
    destination.write_text(text, encoding="utf-8")
    return destination


def test_bash_guard_reexecs_the_script_under_a_modern_bash_with_its_arguments(tmp_path: Path) -> None:
    guard = _simulate_old_bash(LIB / "bash-guard.sh", tmp_path / "bash-guard.sh", reject_all_candidates=False)
    fake = tmp_path / "modern-bash"
    fake.write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\nprintf "REEXEC:%s|%s\\n" "$HERMES_TEMPLATE_BASH_REEXEC" "$*"\n')
    fake.chmod(0o755)
    entry = tmp_path / "entry.sh"
    entry.write_text(f'#!/bin/bash\nsource "{guard}"\necho NOT-REACHED\n')
    result = subprocess.run(
        ["bash", str(entry), "one", "two words"],
        env={**os.environ, "HERMES_TEMPLATE_BASH": str(fake)},
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "NOT-REACHED" not in result.stdout
    line = result.stdout.strip()
    assert line.startswith("REEXEC:") and f"{entry} one two words" in line
    assert line.split("|")[0] != "REEXEC:"  # the loop-breaker marker was exported


def test_bash_guard_stops_with_instructions_when_no_modern_bash_exists(tmp_path: Path) -> None:
    guard = _simulate_old_bash(LIB / "bash-guard.sh", tmp_path / "bash-guard.sh", reject_all_candidates=True)
    entry = tmp_path / "entry.sh"
    entry.write_text(f'#!/bin/bash\nsource "{guard}"\necho NOT-REACHED\n')
    result = subprocess.run(["bash", str(entry)], text=True, capture_output=True, env={**os.environ, "HERMES_TEMPLATE_BASH": ""})
    assert result.returncode == 1 and "NOT-REACHED" not in result.stdout
    assert "bash >= 4.4 is required" in result.stderr and "brew install bash" in result.stderr


def test_bash_guard_probe_child_sees_no_exported_credentials(tmp_path: Path) -> None:
    guard = _simulate_old_bash(LIB / "bash-guard.sh", tmp_path / "bash-guard.sh", reject_all_candidates=False)
    seen = tmp_path / "seen"
    fake = tmp_path / "modern-bash"
    fake.write_text(f'#!/bin/sh\nif [ "$1" = "-c" ]; then env > "{seen}"; exit 0; fi\nexit 0\n')
    fake.chmod(0o755)
    entry = tmp_path / "entry.sh"
    entry.write_text(f'#!/bin/bash\nsource "{guard}"\n')
    subprocess.run(
        ["bash", str(entry)],
        env={**os.environ, "HERMES_TEMPLATE_BASH": str(fake), "TELEGRAM_BOT_TOKEN": "tok-secret-1", "SLACK_APP_TOKEN": "tok-secret-2"},
        capture_output=True,
    )
    probe_env = seen.read_text()
    assert "tok-secret" not in probe_env and "TELEGRAM_BOT_TOKEN" not in probe_env


def test_every_entrypoint_loads_the_guard_before_capturing_invocation_credentials() -> None:
    for name in ("30-telegram.sh", "31-slack.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        assert text.index("bash-guard.sh") < text.index("INVOCATION_"), name
    assert "bash-guard.sh" in (SCRIPTS / "_lib.sh").read_text(encoding="utf-8")
    assert "bash-guard.sh" in (ROOT / "scripts" / "fleet-sync.sh").read_text(encoding="utf-8")


def test_no_gnu_only_commands_remain_in_the_provisioning_scripts() -> None:
    import re

    pattern = re.compile(r"(?<![\w-])(realpath --relative-to|readlink -f|sha256sum|sed -i)\b")
    allowed = {"_lib.sh": {"sha256sum"}}  # sha256_hex prefers sha256sum when present
    offenders = []
    for path in [*SCRIPTS.glob("*.sh"), *(ROOT / "scripts").glob("*.sh")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for match in pattern.finditer(re.split(r"\s+#", line)[0]):  # code, not trailing comments
                if match.group(1) in allowed.get(path.name, set()):
                    continue
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# ----------------------------------------------------- atomic exchange on Darwin


class _FakeRenamex:
    """Stands in for libSystem's renamex_np(from, to, flags) and swaps like APFS does."""

    def __init__(self, result: int = 0, errno_value: int = 0) -> None:
        self.argtypes = None
        self.restype = None
        self.calls: list[tuple[bytes, bytes, int]] = []
        self.result = result
        self.errno_value = errno_value

    def __call__(self, source: bytes, target: bytes, flags: int) -> int:
        self.calls.append((source, target, flags))
        if self.result != 0:
            ctypes.set_errno(self.errno_value)
            return self.result
        if flags == 0x4:  # RENAME_EXCL: fail if the target exists, else a plain rename
            if os.path.exists(target):
                ctypes.set_errno(errno.EEXIST)
                return -1
            os.rename(source, target)
            return 0
        scratch = os.fsdecode(source) + ".swap"
        os.rename(source, scratch)
        os.rename(target, source)
        os.rename(scratch, target)
        return 0


def test_fleet_env_exchange_uses_renamex_np_swap_on_darwin(tmp_path: Path, monkeypatch) -> None:
    parser = _load(LIB / "parse-fleet-env.py", "pfe_darwin")
    fake = _FakeRenamex()
    monkeypatch.setattr(parser.sys, "platform", "darwin")
    monkeypatch.setattr(parser, "_libc_function", lambda name: fake if name == "renamex_np" else None)
    left, right = tmp_path / "left", tmp_path / "right"
    left.write_text("L")
    right.write_text("R")
    parser._exchange_paths(left, right)
    assert (left.read_text(), right.read_text()) == ("R", "L")
    assert fake.calls == [(os.fsencode(left), os.fsencode(right), 0x2)]  # RENAME_SWAP


def test_fleet_env_exchange_fails_closed_on_darwin_without_swap_support(tmp_path: Path, monkeypatch) -> None:
    parser = _load(LIB / "parse-fleet-env.py", "pfe_darwin_missing")
    monkeypatch.setattr(parser.sys, "platform", "darwin")
    left, right = tmp_path / "left", tmp_path / "right"
    left.write_text("L")
    right.write_text("R")

    monkeypatch.setattr(parser, "_libc_function", lambda name: None)
    with pytest.raises(OSError) as missing:
        parser._exchange_paths(left, right)
    assert missing.value.errno == errno.ENOTSUP

    unsupported = _FakeRenamex(result=-1, errno_value=errno.ENOTSUP)  # a volume without VOL_CAP_INT_RENAME_SWAP
    monkeypatch.setattr(parser, "_libc_function", lambda name: unsupported)
    with pytest.raises(OSError) as refused:
        parser._exchange_paths(left, right)
    assert refused.value.errno == errno.ENOTSUP
    assert (left.read_text(), right.read_text()) == ("L", "R")  # nothing moved


def test_fleet_env_upsert_commits_through_the_darwin_swap(tmp_path: Path, monkeypatch) -> None:
    parser = _load(LIB / "parse-fleet-env.py", "pfe_darwin_upsert")
    fake = _FakeRenamex()
    monkeypatch.setattr(parser.sys, "platform", "darwin")
    monkeypatch.setattr(parser, "_libc_function", lambda name: fake if name == "renamex_np" else None)
    fleet = tmp_path / "fleet.env"
    fleet.write_text("HERMES_FLEET_BIN=/old/hermes\n")
    fleet.chmod(0o640)
    parser.atomic_upsert(fleet, "HERMES_FLEET_BIN", "/new/hermes")
    assert "/new/hermes" in fleet.read_text() and "/old/hermes" not in fleet.read_text()
    assert stat.S_IMODE(fleet.stat().st_mode) == 0o640
    assert len(fake.calls) == 1 and not list(tmp_path.glob(".fleet.env.*"))


def test_fleet_env_upsert_reverses_the_swap_when_the_destination_changed(tmp_path: Path, monkeypatch) -> None:
    """The compare-and-swap still detects a concurrent writer on the Darwin path."""
    parser = _load(LIB / "parse-fleet-env.py", "pfe_darwin_cas")
    fleet = tmp_path / "fleet.env"
    fleet.write_text("A=1\n")
    text, original = parser.read_regular_document(fleet)
    fleet.write_text("A=1\nB=concurrent\n")  # someone else wrote between our read and our swap
    fake = _FakeRenamex()
    monkeypatch.setattr(parser.sys, "platform", "darwin")
    monkeypatch.setattr(parser, "_libc_function", lambda name: fake if name == "renamex_np" else None)
    with pytest.raises(OSError, match="changed during update"):
        parser.write_atomic_document(fleet, "A=2\n", original, text.encode())
    assert fleet.read_text() == "A=1\nB=concurrent\n"  # the concurrent write survived
    assert len(fake.calls) == 2  # swap, then swap back


def test_channel_transaction_maps_exchange_and_noreplace_to_renamex_np(tmp_path: Path, monkeypatch) -> None:
    module = _load(SCRIPTS / "channel-transaction.py", "ct_darwin")
    fake = _FakeRenamex()
    monkeypatch.setattr(module, "_RENAMEX_NP", fake)
    left, right = tmp_path / "left", tmp_path / "right"
    left.write_text("L")
    right.write_text("R")
    module.atomic_exchange(left, right)
    assert (left.read_text(), right.read_text()) == ("R", "L")
    assert fake.calls[-1][2] == 0x2  # RENAME_SWAP
    free = tmp_path / "free"
    module.atomic_move_noreplace(left, free)
    assert fake.calls[-1][2] == 0x4  # RENAME_EXCL
    with pytest.raises(module.TransactionRecoveryError):
        module.renameat2(left, right, 1 << 5)  # a Linux-only flag has no Darwin equivalent


# ------------------------------------------------------------------ 70-systemd on Darwin


FAKE_HERMES = """#!/usr/bin/env bash
printf 'HERMES_HOME=%s ARGS=%s\\n' "$HERMES_HOME" "$*" >> "$FAKE_LOG"
state() { cat "$FAKE_STATE" 2>/dev/null || echo off; }
[ "$1" = gateway ] || exit 0
case "$2" in
  status)
    case "$(state)" in
      loaded) echo "✓ Gateway is supervised by launchd (PID 4242)" ;;
      installed) echo "✗ Gateway service is not loaded" ;;
      *) echo "✗ Gateway is not running" ;;
    esac ;;
  install) echo "${FAKE_INSTALL_RESULT:-loaded}" > "$FAKE_STATE" ;;
  start) echo "${FAKE_START_RESULT:-loaded}" > "$FAKE_STATE" ;;
  stop) echo off > "$FAKE_STATE" ;;
esac
exit 0
"""


def _darwin_role(tmp_path: Path, gateway_state: str, preloaded: str = "off") -> tuple[Path, dict[str, str], Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    (role / "runtime").mkdir(parents=True)
    shutil.copytree(LIB, scripts / "lib")
    for name in ("_lib.sh", "70-systemd.sh", "credential-launch.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "heartbeat.sh").write_text("#!/usr/bin/env bash\n")
    (role / "role.yaml").write_text(
        textwrap.dedent(
            f"""\
            repo: demo
            role: pm
            agent_id: demo-pm
            display_name: "Demo PM"
            profile: demo-pm
            telegram:
              provisioning_status: "deferred"
            slack:
              provisioning_status: "deferred"
            service_state:
              gateway: "{gateway_state}"
              heartbeat: "pending"
            """
        )
    )
    home = tmp_path / "home"
    (home / ".hermes" / "profiles" / "demo-pm").mkdir(parents=True)
    fake = tmp_path / "hermes"
    fake.write_text(FAKE_HERMES)
    fake.chmod(0o755)
    state = tmp_path / "gateway.state"
    state.write_text(preloaded + "\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
        "REGISTRY_FILE": str(tmp_path / "agents-registry.yaml"),
        "HERMES_BIN": str(fake),
        "HERMES_TEMPLATE_PLATFORM": "Darwin",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_STATE": str(state),
        "LAUNCHD_STABILIZATION_ATTEMPTS": "2",
        "LAUNCHD_STABILIZATION_INTERVAL_SECONDS": "0",
    }
    env.pop("HERMES_FLEET_HOME", None)
    (tmp_path / "agents-registry.yaml").write_text("schema_version: 1\nagents: {}\n")
    return role, env, state


def _step(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(role / ".scripts" / "70-systemd.sh")], env=env, text=True, capture_output=True, check=False)


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _service_state(role: Path, key: str) -> str:
    return subprocess.run(
        [sys.executable, str(LIB / "role-yaml.py"), str(role / "role.yaml"), f"service_state.{key}"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()


@pytest.mark.parametrize("declared", ["pending", "deferred", "error", "installed"])
def test_darwin_defers_the_gateway_unless_role_state_says_active(tmp_path: Path, declared: str) -> None:
    role, env, _ = _darwin_role(tmp_path, declared)
    result = _step(role, env)
    assert result.returncode == 0, result.stderr
    assert _service_state(role, "gateway") == "deferred"
    assert _service_state(role, "heartbeat") == "retired"
    assert _calls(env) == []  # the fork was never asked to do anything
    assert not (Path(env["HOME"]) / ".config" / "systemd").exists()
    assert not list((Path(env["HOME"]) / "Library").rglob("*.plist")) if (Path(env["HOME"]) / "Library").exists() else True


def test_darwin_leaves_an_already_supervised_gateway_alone(tmp_path: Path) -> None:
    role, env, _ = _darwin_role(tmp_path, "active", preloaded="loaded")
    result = _step(role, env)
    assert result.returncode == 0, result.stderr
    assert _service_state(role, "gateway") == "active"
    assert not any("gateway install" in call or "gateway start" in call for call in _calls(env))


def test_darwin_installs_the_one_gateway_for_the_fleet_root_not_the_profile(tmp_path: Path) -> None:
    role, env, state = _darwin_role(tmp_path, "active", preloaded="off")
    result = _step(role, env)
    assert result.returncode == 0, result.stderr
    assert _service_state(role, "gateway") == "active"
    assert state.read_text().strip() == "loaded"
    installs = [call for call in _calls(env) if "gateway install" in call]
    assert len(installs) == 1 and "--if-missing" in installs[0]
    # HERMES_HOME is the fleet root: a profile home would name ai.hermes.gateway-<profile>.
    assert all(call.startswith(f"HERMES_HOME={env['HOME']}/.hermes ARGS=") for call in _calls(env))
    assert (role / ".scripts" / ".done-70-systemd").exists()
    assert not list((Path(env["HOME"]) / ".config").rglob("*.service"))  # no per-agent units, ever


def test_darwin_never_leaves_a_gateway_that_did_not_come_up(tmp_path: Path) -> None:
    role, env, state = _darwin_role(tmp_path, "active", preloaded="off")
    env["FAKE_INSTALL_RESULT"] = "installed"  # registered but launchd never supervises it
    env["FAKE_START_RESULT"] = "installed"
    result = _step(role, env)
    assert result.returncode != 0
    assert "not leaving a crash loop running" in result.stderr
    assert _service_state(role, "gateway") == "error"
    assert any("gateway stop" in call for call in _calls(env))
    assert state.read_text().strip() == "off"
    assert not (role / ".scripts" / ".done-70-systemd").exists()


def test_darwin_does_not_stop_a_gateway_it_did_not_start(tmp_path: Path) -> None:
    role, env, _ = _darwin_role(tmp_path, "active", preloaded="installed")
    env["FAKE_INSTALL_RESULT"] = "installed"
    env["FAKE_START_RESULT"] = "installed"
    result = _step(role, env)
    assert result.returncode != 0
    assert _service_state(role, "gateway") == "error"
    # a pre-existing registration may be serving other profiles: report, never stop it
    assert not any("gateway stop" in call for call in _calls(env))
    assert (role / ".scripts" / ".done-70-systemd").exists() is False


def test_darwin_defers_when_the_hermes_binary_is_missing(tmp_path: Path) -> None:
    role, env, _ = _darwin_role(tmp_path, "active")
    env["HERMES_BIN"] = str(tmp_path / "does-not-exist")
    result = _step(role, env)
    assert result.returncode == 0, result.stderr
    assert _service_state(role, "gateway") == "deferred"
    assert _calls(env) == []


def test_linux_branch_is_untouched_by_the_platform_switch(tmp_path: Path) -> None:
    role, env, _ = _darwin_role(tmp_path, "active")
    env["HERMES_TEMPLATE_PLATFORM"] = "Linux"
    env["SKIP_SYSTEMD"] = "1"
    result = _step(role, env)
    assert result.returncode == 0, result.stderr
    assert _calls(env) == []  # SKIP_SYSTEMD short-circuits before any service manager is touched


# --------------------------------------------------------- code root configurability


def test_code_root_defaults_match_the_linux_fleet_layout(tmp_path: Path) -> None:
    role, env = _lock_role(tmp_path)
    for key in ("HERMES_CODE_ROOT", "HERMES_TEMPLATE_REPO", "HERMES_SKILLEX_REPO"):
        env.pop(key, None)
    env["HERMES_TEMPLATE_CONFIG"] = str(tmp_path / "absent.toml")
    result = _bash('source "$1/.scripts/_lib.sh"; echo "$HERMES_CODE_ROOT|$HERMES_TEMPLATE_REPO|$HERMES_SKILLEX_REPO"', env, str(role))
    home = env["HOME"]
    assert result.stdout.strip() == f"{home}/code|{home}/code/33GOD/hermes-agent-template|{home}/code/skillex"


def test_code_root_follows_host_config_and_env_overrides_it(tmp_path: Path) -> None:
    role, env = _lock_role(tmp_path)
    for key in ("HERMES_CODE_ROOT", "HERMES_TEMPLATE_REPO", "HERMES_SKILLEX_REPO"):
        env.pop(key, None)
    config = tmp_path / "config.toml"
    config.write_text('[paths]\ncode_root = "/Users/x/Code"\ntemplate_repo = "/Users/x/Code/hermes-agent-template"\n')
    env["HERMES_TEMPLATE_CONFIG"] = str(config)
    script = 'source "$1/.scripts/_lib.sh"; echo "$HERMES_CODE_ROOT|$HERMES_TEMPLATE_REPO|$HERMES_SKILLEX_REPO"'
    from_config = _bash(script, env, str(role))
    assert from_config.stdout.strip() == "/Users/x/Code|/Users/x/Code/hermes-agent-template|/Users/x/Code/skillex"
    overridden = _bash(script, {**env, "HERMES_CODE_ROOT": "/opt/src", "HERMES_SKILLEX_REPO": "/opt/skillex"}, str(role))
    assert overridden.stdout.strip() == "/opt/src|/Users/x/Code/hermes-agent-template|/opt/skillex"


def test_profile_renderer_locator_honours_the_configured_template_checkout(tmp_path: Path, monkeypatch) -> None:
    locator = _load(LIB / "profile-renderer.py", "profile_renderer_locator")
    checkout = tmp_path / "Code" / "hermes-agent-template"
    (checkout / "scripts").mkdir(parents=True)
    renderer = checkout / "scripts" / locator.RENDERER_NAME
    renderer.write_text("# renderer\n")
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ntemplate_repo = "{checkout}"\n')
    monkeypatch.delenv("PROFILE_RENDERER", raising=False)
    monkeypatch.delenv("HERMES_TEMPLATE_REPO", raising=False)
    monkeypatch.setenv("HERMES_TEMPLATE_CONFIG", str(config))
    monkeypatch.setattr(locator, "__file__", str(tmp_path / "deployed" / "a" / "b" / "c.py"))
    assert locator.locate() == renderer
    monkeypatch.setenv("HERMES_TEMPLATE_REPO", str(checkout))
    candidates = locator.candidates()
    assert renderer in candidates
    assert candidates.index(renderer) < candidates.index(Path.home() / 'code' / '33GOD' / 'hermes-agent-template' / 'scripts' / locator.RENDERER_NAME)
