from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
TELEGRAM_SCRIPT = ROOT / "template" / ".scripts" / "30-telegram.sh"
LIB_SCRIPT = ROOT / "template" / ".scripts" / "_lib.sh"
LIB_DIR = ROOT / "template" / ".scripts" / "lib"
REGISTRY_SCRIPT = ROOT / "template" / ".scripts" / "80-registry.sh"
STORE_HELPER = ROOT / "template" / ".scripts" / "store-onepassword-secret.py"
FAKE_OP = ROOT / "tests" / "support" / "fake-op.py"

BOT_TOKEN = "123456:profile-only-secret"
OTHER_TOKEN = "654321:different-profile-secret"


def _make_role(tmp_path: Path) -> tuple[Path, Path, Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    shutil.copy2(TELEGRAM_SCRIPT, scripts / TELEGRAM_SCRIPT.name)
    shutil.copy2(LIB_SCRIPT, scripts / LIB_SCRIPT.name)
    shutil.copy2(STORE_HELPER, scripts / STORE_HELPER.name)
    shutil.copytree(LIB_DIR, scripts / LIB_DIR.name)
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
telegram:
  provisioning_status: "deferred"
  bot_username: "demo_pm_bot"
  bot_id: ""
slack:
  provisioning_status: "deferred"
  team_id: ""
  team_name: ""
  bot_user_id: ""
  bot_id: ""
  bot_username: ""
bloodbank:
  gateway_scope: fleet
  target_agent_id: demo-pm
plane:
  workspace: "test"
  identifier: ""
runtime:
  github_owner: "test"
  github_repo: "agent-hm-demo-pm"
""",
        encoding="utf-8",
    )
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    return role, runtime, registry


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    curl = bindir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import re
import sys

assert sys.argv[1:] == ["--config", "-"]
config = sys.stdin.read()
match = re.search(r'https://api[.]telegram[.]org/bot([^/]+)/getMe', config)
assert match
token = match.group(1)
assert token.encode() not in pathlib.Path("/proc/self/cmdline").read_bytes()
assert token.encode() not in pathlib.Path("/proc/self/environ").read_bytes()
print(json.dumps({"ok": True, "result": {"id": 424242, "username": "verified_demo_bot"}}))
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    hermes = bindir / "hermes"
    hermes.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)
    op = bindir / "op"
    shutil.copy2(FAKE_OP, op)
    op.chmod(0o755)
    return bindir


def _run(
    role: Path,
    registry: Path,
    home: Path,
    bindir: Path,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELEGRAM_") and key != "SKIP_TELEGRAM"
    }
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bindir}:{env['PATH']}",
            "HERMES_BIN": str(bindir / "hermes"),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
            "UNRELATED_PROVIDER_SECRET": "must-not-reach-op",
        }
    )
    env.update(overrides or {})
    profile_name = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["profile"]
    fleet_home = home / ".hermes"
    profile = fleet_home / "profiles" / profile_name
    profile.mkdir(parents=True, exist_ok=True)
    (fleet_home / "config.yaml").write_text(
        "plugins: {}\nplatforms:\n  telegram:\n    enabled: true\n",
        encoding="utf-8",
    )
    delta = profile / "config.delta.yaml"
    if not delta.exists():
        delta.write_text("{}\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(role / ".scripts" / "30-telegram.sh")],
        env=env,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )


def _inject_parent_fsync_failure(
    tmp_path: Path, overrides: dict[str, str], parent: Path, bindir: Path
) -> None:
    injection = tmp_path / "fsync-failure.py"
    injection.write_text(
        """import errno
import os
import stat

_real_fsync = os.fsync
_failed_parent = os.path.realpath(os.environ["FAIL_PARENT_FSYNC"])

def _fsync(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        resolved = os.path.realpath(f"/proc/self/fd/{fd}")
        if resolved == _failed_parent:
            raise OSError(errno.EIO, "injected parent directory fsync failure")
    return _real_fsync(fd)

os.fsync = _fsync
""",
        encoding="utf-8",
    )
    wrapper = bindir / "python3"
    wrapper.write_text(
        f"""#!/usr/bin/env bash
set -o pipefail
if [[ "${{1:-}}" == "-" && -n "${{FAIL_PARENT_FSYNC:-}}" ]]; then
  {{ cat "{injection}"; cat; }} | "{Path(sys.executable).resolve()}" "$@"
else
  exec "{Path(sys.executable).resolve()}" "$@"
fi
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    overrides["FAIL_PARENT_FSYNC"] = str(parent)


def test_telegram_ignores_shared_fleet_token_and_defers_noninteractive(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_ALLOWED_USERS=111\n",
        encoding="utf-8",
    )

    result = _run(role, registry, home, _fake_bin(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "deferred" in result.stderr
    assert not (runtime / ".env").exists()
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert BOT_TOKEN in fleet.read_text(encoding="utf-8")
    generated = home / ".hermes" / "profiles" / "demo-pm" / "config.yaml"
    assert yaml.safe_load(generated.read_text(encoding="utf-8"))["platforms"][
        "telegram"
    ]["enabled"] is False


def test_local_only_marker_does_not_block_later_telegram_activation(tmp_path: Path) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("TELEGRAM_ALLOWED_USERS=111\n", encoding="utf-8")
    marker = role / ".scripts" / ".done-30-telegram"
    unrelated = role / ".scripts" / ".done-20-runtime-repo"
    marker.touch()
    unrelated.touch()
    bindir = _fake_bin(tmp_path)

    deferred = _run(role, registry, home, bindir, {"SKIP_TELEGRAM": "1"})

    assert deferred.returncode == 0, deferred.stderr
    assert not marker.exists()
    assert unrelated.exists()
    assert yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]["provisioning_status"] == "deferred"

    activated = _run(
        role,
        registry,
        home,
        bindir,
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        },
    )

    assert activated.returncode == 0, activated.stderr
    assert marker.exists()
    assert unrelated.exists()
    assert yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]["provisioning_status"] == "verified"


def test_explicit_token_stores_only_reference_and_nonsecret_policy(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("TELEGRAM_ALLOWED_USERS=111,222\n", encoding="utf-8")
    shared = home / ".hermes" / ".env"
    shared.write_text("PROVIDER_KEY=keep-me\n", encoding="utf-8")

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {"TELEGRAM_BOT_TOKEN": BOT_TOKEN},
    )

    assert result.returncode == 0, result.stderr
    env_file = runtime / ".env"
    env_text = env_file.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" not in env_text
    assert 'TELEGRAM_ALLOWED_USERS="111,222"' in env_text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"
    assert fleet.read_text(encoding="utf-8") == "TELEGRAM_ALLOWED_USERS=111,222\n"
    assert BOT_TOKEN not in result.stdout + result.stderr
    profile = home / ".hermes" / "profiles" / "demo-pm"
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text(encoding="utf-8"))
    reference = delta["secrets"]["onepassword"]["env"]["TELEGRAM_BOT_TOKEN"]
    assert reference == "op://DeLoSecrets/hermes-agent-demo-pm-telegram-bot-token/password"
    assert BOT_TOKEN not in (profile / "config.delta.yaml").read_text(encoding="utf-8")
    assert BOT_TOKEN not in (profile / "config.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))[
        "platforms"
    ]["telegram"]["enabled"] is True

    telegram = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]
    assert telegram == {
        "provisioning_status": "verified",
        "bot_username": "verified_demo_bot",
        "bot_id": "424242",
    }


def test_transient_onepassword_outage_preserves_verified_telegram_wiring_and_recovers(
    tmp_path: Path,
) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_bin(tmp_path)

    first = _run(
        role,
        registry,
        home,
        bindir,
        {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_ALLOWED_USERS": "111"},
    )
    assert first.returncode == 0, first.stderr

    role_before = (role / "role.yaml").read_bytes()
    delta_path = home / ".hermes" / "profiles" / "demo-pm" / "config.delta.yaml"
    delta_before = delta_path.read_bytes()
    marker = role / ".scripts" / ".done-30-telegram"
    assert marker.is_file()

    (home / ".fake-onepassword-outage").touch()
    outage = _run(role, registry, home, bindir)

    assert outage.returncode != 0
    assert "temporarily unavailable" in outage.stderr
    assert (role / "role.yaml").read_bytes() == role_before
    assert delta_path.read_bytes() == delta_before
    assert marker.is_file()
    assert BOT_TOKEN not in outage.stdout + outage.stderr

    (home / ".fake-onepassword-outage").unlink()
    recovered = _run(role, registry, home, bindir)

    assert recovered.returncode == 0, recovered.stderr
    assert "existing verified 1Password wiring reconciled" in recovered.stderr
    assert (role / "role.yaml").read_bytes() == role_before
    assert delta_path.read_bytes() == delta_before
    assert marker.is_file()


def test_credential_parent_fsync_failure_is_reported_and_retryable(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("TELEGRAM_ALLOWED_USERS=111\n", encoding="utf-8")
    bindir = _fake_bin(tmp_path)
    overrides = {
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    }
    _inject_parent_fsync_failure(tmp_path, overrides, runtime, bindir)

    failed = _run(role, registry, home, bindir, overrides)

    assert failed.returncode != 0
    assert "injected parent directory fsync failure" in failed.stderr
    env_file = runtime / ".env"
    assert "TELEGRAM_BOT_TOKEN" not in env_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert BOT_TOKEN not in failed.stdout + failed.stderr
    assert "demo-pm" in yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert not (role / ".scripts" / ".done-30-telegram").exists()

    retried = _run(
        role,
        registry,
        home,
        bindir,
        {"TELEGRAM_BOT_TOKEN": BOT_TOKEN},
    )

    assert retried.returncode == 0, retried.stderr
    assert (role / ".scripts" / ".done-30-telegram").exists()


def test_rejects_token_parked_in_shared_fleet_env(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\n", encoding="utf-8")

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
        },
    )

    assert result.returncode != 0
    assert "already assigned to shared fleet environment" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_rejects_token_reused_by_another_profile(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    other_role = tmp_path / "other-role"
    other_runtime = other_role / "runtime"
    other_runtime.mkdir(parents=True)
    (other_runtime / ".env").write_text(
        f'TELEGRAM_BOT_TOKEN="{BOT_TOKEN}"\n', encoding="utf-8"
    )
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "other-pm": {
                        "role_dir": str(other_role),
                        "telegram": {"bot_id": "999999"},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
        },
    )

    assert result.returncode != 0
    assert "already assigned to agent other-pm" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_rejects_verified_bot_identity_owned_by_another_agent(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "other-pm": {
                        "telegram": {
                            "provisioning_status": "verified",
                            "bot_username": "verified_demo_bot",
                            "bot_id": "424242",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": OTHER_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
        },
    )

    assert result.returncode != 0
    assert "bot identity is already assigned to agent other-pm" in result.stderr
    assert OTHER_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_refuses_runtime_env_symlink_before_get_me(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    shared = home / ".hermes" / ".env"
    shared.parent.mkdir(parents=True)
    shared.write_text("PROVIDER_KEY=keep-me\n", encoding="utf-8")
    (runtime / ".env").symlink_to(shared)

    result = _run(
        role,
        registry,
        home,
        _fake_bin(tmp_path),
        {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ALLOWED_USERS": "111",
        },
    )

    assert result.returncode != 0
    assert "refusing to write Telegram credentials through symlink" in result.stderr
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"


def test_registry_persists_telegram_identity_without_token(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy2(REGISTRY_SCRIPT, role / ".scripts" / REGISTRY_SCRIPT.name)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8")
        .replace('provisioning_status: "deferred"', 'provisioning_status: "verified"', 1)
        .replace('bot_username: "demo_pm_bot"', 'bot_username: "verified_demo_bot"')
        .replace('bot_id: ""', 'bot_id: "424242"', 1),
        encoding="utf-8",
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")
    }
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "80-registry.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    serialized = registry.read_text(encoding="utf-8")
    entry = yaml.safe_load(serialized)["agents"]["demo-pm"]
    assert entry["telegram"] == {
        "provisioning_status": "verified",
        "bot_username": "verified_demo_bot",
        "bot_id": "424242",
    }
    assert "TELEGRAM_BOT_TOKEN" not in serialized


def test_concurrent_profiles_cannot_claim_same_telegram_identity(tmp_path: Path) -> None:
    role_a, _, _ = _make_role(tmp_path / "a")
    role_b, _, _ = _make_role(tmp_path / "b")
    role_b_yaml = role_b / "role.yaml"
    role_b_yaml.write_text(
        role_b_yaml.read_text(encoding="utf-8").replace("demo-pm", "demo-reviewer"),
        encoding="utf-8",
    )
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_bin(tmp_path)
    overrides = {
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "TELEGRAM_ALLOWED_USERS": "111",
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda role: _run(role, registry, home, bindir, overrides),
                (role_a, role_b),
            )
        )

    assert sorted(result.returncode for result in results) == [0, 1]
    combined = "".join(result.stdout + result.stderr for result in results)
    assert "bot identity is already assigned" in combined
    assert BOT_TOKEN not in combined
    agents = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert len(agents) == 1
    assert next(iter(agents.values()))["telegram"]["bot_id"] == "424242"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{registry}.lock").stat().st_mode) == 0o600


def test_telegram_refuses_registry_and_lock_symlinks(tmp_path: Path) -> None:
    role, runtime, _ = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_bin(tmp_path)
    target = tmp_path / "registry-target.yaml"
    target.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    registry = tmp_path / "agents-registry.yaml"
    registry.unlink()
    registry.symlink_to(target)
    overrides = {
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "TELEGRAM_ALLOWED_USERS": "111",
    }

    result = _run(role, registry, home, bindir, overrides)

    assert result.returncode != 0
    assert "refusing" in result.stderr and "symlink" in result.stderr
    assert not (runtime / ".env").exists()
    assert target.read_text(encoding="utf-8") == "schema_version: 1\nagents: {}\n"
