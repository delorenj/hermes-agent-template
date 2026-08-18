from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "template" / ".scripts"


def _make_role(tmp_path: Path) -> tuple[Path, Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    shutil.copytree(SCRIPTS / "lib", scripts / "lib")
    for name in (
        "_lib.sh",
        "credential-launch.sh",
        "60-bloodbank.sh",
        "70-systemd.sh",
        "80-registry.sh",
    ):
        shutil.copy2(SCRIPTS / name, scripts / name)
    (scripts / "heartbeat.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "checkpoint.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
model:
  provider: ""
  name: ""
  base_url: ""
  api_mode: ""
  key_env: ""
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
  enabled: false
  gateway_scope: fleet
  target_agent_id: "demo-pm"
  producer: "hermes-agent:demo-pm"
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
    return role, registry


def _environment(tmp_path: Path, registry: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".hermes" / "profiles" / "demo-pm").mkdir(
        parents=True, exist_ok=True
    )
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )
    return env


def _run(role: Path, name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(role / ".scripts" / name)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _install_unavailable_systemd_fixture(tmp_path: Path, env: dict[str, str]) -> None:
    fake_bin = tmp_path / "systemd-unavailable-bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo inactive; exit 4 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo not-found; exit 4 ;;
  *"is-system-running"*) exit 1 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"


def _inject_parent_fsync_failure(
    tmp_path: Path, env: dict[str, str], parent: Path
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
    wrapper_dir = tmp_path / "fsync-failure-bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "python3"
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
    env["PATH"] = f"{wrapper_dir}:{env['PATH']}"
    env["FAIL_PARENT_FSYNC"] = str(parent)


def test_future_runtime_scaffolds_have_no_profile_consumer_or_inbox() -> None:
    for scaffold in (ROOT / "runtime-scaffold", ROOT / "template" / ".runtime-scaffold"):
        assert not (scaffold / "bloodbank-consumer.py").exists()
        assert "bloodbank-inbox" not in (scaffold / ".gitignore").read_text(encoding="utf-8")
        readme = (scaffold / "README.md").read_text(encoding="utf-8")
        assert "fleet-shared" in readme
        assert "no consumer process or inbox bridge" in readme


@pytest.mark.parametrize("skip", ["0", "1"])
def test_step_60_is_a_harmless_compatibility_noop(tmp_path: Path, skip: str) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    env["SKIP_BLOODBANK"] = skip

    result = _run(role, "60-bloodbank.sh", env)

    assert result.returncode == 0, result.stderr
    assert (role / ".scripts" / ".done-60-bloodbank").is_file()
    assert not (role / "runtime" / "bloodbank-consumer.py").exists()
    assert "NATS" not in result.stdout + result.stderr


def test_systemd_installs_only_profile_gateway_and_heartbeat(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo inactive; exit 4 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo not-found; exit 4 ;;
  *"is-system-running"*) exit 1 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    assert (unit_dir / "hermes-demo-pm-gateway.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.timer").is_file()
    assert not (unit_dir / "hermes-demo-pm-consumer.service").exists()
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in unit_dir.iterdir())
    assert "bloodbank-consumer.py" not in rendered
    assert "consumer.log" not in rendered
    assert f'Environment="HERMES_HOME={Path(env["HOME"]) / ".hermes" / "profiles" / "demo-pm"}"' in rendered
    assert f'Environment="TERMINAL_CWD={tmp_path}"' in rendered
    assert 'ExecStart="' + str(role / ".scripts" / "credential-launch.sh") + '" gateway' in rendered
    assert "HERMES_OAUTH_FILE" not in rendered


def test_systemd_rejects_newline_injection_before_writing_units(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    _install_unavailable_systemd_fixture(tmp_path, env)
    env["CODEX_HOME"] = "/safe\nEnvironment=PJAN67_INJECTED=yes"

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode != 0
    assert "systemd" in result.stderr.lower() or "newline" in result.stderr.lower()
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    assert not list(unit_dir.glob("hermes-*.service")) if unit_dir.exists() else True
    assert not (role / ".scripts" / ".done-70-systemd").exists()


def test_systemd_serializes_spaces_quotes_backslashes_percent_and_dollar(
    tmp_path: Path,
) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    _install_unavailable_systemd_fixture(tmp_path, env)
    env["CODEX_HOME"] = '/code x/"quoted"/back\\slash/%token/$dollar'

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    units = Path(env["HOME"]) / ".config" / "systemd" / "user"
    gateway = (units / "hermes-demo-pm-gateway.service").read_text(encoding="utf-8")
    heartbeat = (units / "hermes-demo-pm-heartbeat.service").read_text(encoding="utf-8")
    expected = 'Environment="CODEX_HOME=/code x/\\"quoted\\"/back\\\\slash/%%token/$dollar"'
    assert expected in gateway
    assert expected in heartbeat


def test_systemd_skip_never_queries_or_writes_user_manager_state(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    env["SKIP_SYSTEMD"] = "1"
    fake_bin = tmp_path / "skip-bin"
    fake_bin.mkdir()
    called = tmp_path / "systemctl-called"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\ntouch \"$SYSTEMCTL_CALLED\"\nexit 99\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["SYSTEMCTL_CALLED"] = str(called)

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    assert not called.exists()
    assert not (role / ".scripts" / ".done-70-systemd").exists()
    assert not (Path(env["HOME"]) / ".config" / "systemd" / "user").exists()

    env.pop("SKIP_SYSTEMD")
    systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo inactive; exit 4 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo not-found; exit 4 ;;
  *"is-system-running"*) exit 1 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    activated = _run(role, "70-systemd.sh", env)
    assert activated.returncode == 0, activated.stderr
    assert (role / ".scripts" / ".done-70-systemd").is_file()
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    assert (unit_dir / "hermes-demo-pm-gateway.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.service").is_file()
    assert (unit_dir / "hermes-demo-pm-heartbeat.timer").is_file()


def test_systemd_loads_optional_encrypted_credentials_without_plaintext(
    tmp_path: Path,
) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            'key_env: ""', 'key_env: "DIRECTOR_LITELLM_KEY"'
        ),
        encoding="utf-8",
    )
    credential_dir = Path(env["HOME"]) / ".config" / "hermes-agent" / "credentials"
    credential_dir.mkdir(parents=True)
    telegram_cred = credential_dir / "demo-pm-telegram-bot-token.cred"
    model_cred = credential_dir / "demo-pm-model-api-key.cred"
    telegram_cred.write_text("encrypted-placeholder", encoding="utf-8")
    model_cred.write_text("encrypted-placeholder", encoding="utf-8")
    fake_bin = tmp_path / "bin-encrypted"
    fake_bin.mkdir()
    for name, script in {
        "systemd-creds": "#!/usr/bin/env bash\nexit 0\n",
        "systemctl": """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo inactive; exit 4 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo not-found; exit 4 ;;
  *"is-system-running"*) exit 1 ;;
esac
exit 1
""",
    }.items():
        path = fake_bin / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    gateway = (unit_dir / "hermes-demo-pm-gateway.service").read_text(encoding="utf-8")
    heartbeat = (unit_dir / "hermes-demo-pm-heartbeat.service").read_text(encoding="utf-8")
    assert f'LoadCredentialEncrypted="telegram_bot_token:{telegram_cred}"' in gateway
    assert f'LoadCredentialEncrypted="model_api_key:{model_cred}"' in gateway
    assert f'LoadCredentialEncrypted="model_api_key:{model_cred}"' in heartbeat
    assert "DIRECTOR_LITELLM_KEY" not in gateway + heartbeat
    assert "encrypted-placeholder" not in gateway + heartbeat


def test_systemd_done_marker_still_retires_legacy_consumer(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    consumer = unit_dir / "hermes-demo-pm-consumer.service"
    consumer.write_text("legacy\n", encoding="utf-8")
    for unit in (
        "hermes-demo-pm-gateway.service",
        "hermes-demo-pm-heartbeat.service",
        "hermes-demo-pm-heartbeat.timer",
    ):
        (unit_dir / unit).write_text("existing\n", encoding="utf-8")
    (role / ".scripts" / ".done-70-systemd").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    retired = tmp_path / "retired"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
case "$*" in
  *"disable --now hermes-demo-pm-consumer.service"*) touch "$SYSTEMCTL_RETIRED"; exit 0 ;;
  *"is-active hermes-demo-pm-consumer.service"*)
    if [[ -f "$SYSTEMCTL_RETIRED" ]]; then echo inactive; exit 3; else echo active; exit 0; fi ;;
  *"is-enabled hermes-demo-pm-consumer.service"*)
    if [[ -f "$SYSTEMCTL_RETIRED" ]]; then echo disabled; exit 1; else echo enabled; exit 0; fi ;;
  *"is-system-running"*) echo running; exit 0 ;;
  *"daemon-reload"*) exit 0 ;;
  *"enable --now "*) exit 0 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "SYSTEMCTL_LOG": str(log),
            "SYSTEMCTL_RETIRED": str(retired),
        }
    )

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode == 0, result.stderr
    assert not consumer.exists()
    assert "disable --now hermes-demo-pm-consumer.service" in log.read_text(encoding="utf-8")
    assert "reconciling unit definitions" in result.stderr
    assert "Hermes Gateway" in (unit_dir / "hermes-demo-pm-gateway.service").read_text(encoding="utf-8")


def test_systemd_preserves_legacy_consumer_when_disable_fails(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    consumer = unit_dir / "hermes-demo-pm-consumer.service"
    consumer.write_text("legacy\n", encoding="utf-8")
    marker = role / ".scripts" / ".done-70-systemd"
    marker.touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo active; exit 0 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo enabled; exit 0 ;;
  *"disable --now hermes-demo-pm-consumer.service"*) exit 1 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode != 0
    assert "disable failed" in result.stderr
    assert consumer.read_text(encoding="utf-8") == "legacy\n"
    assert marker.exists()
    assert not (unit_dir / "hermes-demo-pm-gateway.service").exists()


@pytest.mark.parametrize("failed_query", ["is-active", "is-enabled"])
def test_systemd_preserves_legacy_consumer_when_state_query_fails(
    tmp_path: Path, failed_query: str
) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    unit_dir = Path(env["HOME"]) / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    consumer = unit_dir / "hermes-demo-pm-consumer.service"
    consumer.write_text("legacy\n", encoding="utf-8")
    marker = role / ".scripts" / ".done-70-systemd"
    marker.touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
case "$*" in
  *"$FAILED_QUERY hermes-demo-pm-consumer.service"*) echo "Failed to connect to bus" >&2; exit 1 ;;
  *"is-active hermes-demo-pm-consumer.service"*) echo active; exit 0 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo enabled; exit 0 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAILED_QUERY": failed_query,
            "SYSTEMCTL_LOG": str(log),
        }
    )

    result = _run(role, "70-systemd.sh", env)

    assert result.returncode != 0
    assert "cannot safely query" in result.stderr
    assert consumer.read_text(encoding="utf-8") == "legacy\n"
    assert marker.exists()
    assert "disable --now" not in log.read_text(encoding="utf-8")


def test_registry_records_fleet_gateway_contract_without_consumer_unit(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "demo-pm": {
                        "systemd": {
                            "consumer_unit": "hermes-demo-pm-consumer.service"
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _run(role, "80-registry.sh", env)

    assert result.returncode == 0, result.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert entry["bloodbank"] == {
        "enabled": False,
        "gateway_scope": "fleet",
        "target_agent_id": "demo-pm",
    }
    assert entry["systemd"] == {
        "gateway_unit": "hermes-demo-pm-gateway.service",
        "heartbeat_timer": "hermes-demo-pm-heartbeat.timer",
    }
    assert "consumer_unit" not in entry["systemd"]


@pytest.mark.parametrize(
    ("role_name", "agent_id"),
    (("pm", "demo-pm"), ("director", "demo-director")),
)
def test_bloodbank_stays_quarantined_until_explicit_activation(
    tmp_path: Path, role_name: str, agent_id: str
) -> None:
    role, registry = _make_role(tmp_path)
    role_yaml = role / "role.yaml"
    if role_name == "director":
        role_yaml.write_text(
            role_yaml.read_text(encoding="utf-8")
            .replace("role: pm", "role: director")
            .replace("demo-pm", agent_id)
            .replace("Demo PM", "Demo Director"),
            encoding="utf-8",
        )
    env = _environment(tmp_path, registry)

    planned = _run(role, "80-registry.sh", env)
    assert planned.returncode == 0, planned.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"][agent_id]
    assert entry["bloodbank"]["enabled"] is False
    assert isinstance(entry["bloodbank"]["enabled"], bool)

    unchanged = _run(role, "80-registry.sh", env)
    assert unchanged.returncode == 0, unchanged.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"][agent_id]
    assert entry["bloodbank"]["enabled"] is False

    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            "  enabled: false", "  enabled: true"
        ),
        encoding="utf-8",
    )
    activated = _run(role, "80-registry.sh", env)
    assert activated.returncode == 0, activated.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"][agent_id]
    assert entry["bloodbank"]["enabled"] is True
    assert isinstance(entry["bloodbank"]["enabled"], bool)


def test_registry_rejects_malformed_bloodbank_gate_without_mutation(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    seeded = _run(role, "80-registry.sh", env)
    assert seeded.returncode == 0, seeded.stderr
    before = registry.read_bytes()
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            "  enabled: false", "  enabled: yes"
        ),
        encoding="utf-8",
    )

    malformed = _run(role, "80-registry.sh", env)

    assert malformed.returncode != 0
    assert "strict YAML boolean" in malformed.stderr
    assert registry.read_bytes() == before


def test_concurrent_registry_upserts_are_atomic_and_lossless(tmp_path: Path) -> None:
    role_a, _ = _make_role(tmp_path / "a")
    role_b, _ = _make_role(tmp_path / "b")
    role_b_yaml = role_b / "role.yaml"
    role_b_yaml.write_text(
        role_b_yaml.read_text(encoding="utf-8").replace("demo-pm", "demo-reviewer"),
        encoding="utf-8",
    )
    registry = tmp_path / "agents-registry.yaml"
    registry.write_text("schema_version: 1\nagents: {}\n", encoding="utf-8")
    (tmp_path / "env-a").mkdir()
    (tmp_path / "env-b").mkdir()
    env_a = _environment(tmp_path / "env-a", registry)
    env_b = _environment(tmp_path / "env-b", registry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: _run(*args),
                (
                    (role_a, "80-registry.sh", env_a),
                    (role_b, "80-registry.sh", env_b),
                ),
            )
        )

    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    agents = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert set(agents) == {"demo-pm", "demo-reviewer"}
    assert (registry.stat().st_mode & 0o777) == 0o600


def test_registry_parent_fsync_failure_is_reported_and_retryable(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    _inject_parent_fsync_failure(tmp_path, env, registry.parent)

    failed = _run(role, "80-registry.sh", env)

    assert failed.returncode != 0
    assert "injected parent directory fsync failure" in failed.stderr
    assert "demo-pm" in yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert (registry.stat().st_mode & 0o777) == 0o600
    assert not (role / ".scripts" / ".done-80-registry").exists()

    retry_env = _environment(tmp_path, registry)
    retried = _run(role, "80-registry.sh", retry_env)

    assert retried.returncode == 0, retried.stderr
    assert (role / ".scripts" / ".done-80-registry").exists()


def test_registry_upsert_refuses_lock_symlink(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("untouched", encoding="utf-8")
    Path(f"{registry}.lock").symlink_to(lock_target)

    result = _run(role, "80-registry.sh", env)

    assert result.returncode != 0
    assert "lock symlink" in result.stderr
    assert lock_target.read_text(encoding="utf-8") == "untouched"


def test_stale_lock_file_is_safe_when_no_process_holds_flock(tmp_path: Path) -> None:
    role, registry = _make_role(tmp_path)
    env = _environment(tmp_path, registry)
    lock = Path(f"{registry}.lock")
    lock.write_text("left by a crashed provisioner\n", encoding="utf-8")
    lock.chmod(0o644)

    result = _run(role, "80-registry.sh", env)

    assert result.returncode == 0, result.stderr
    assert "demo-pm" in yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert (lock.stat().st_mode & 0o777) == 0o600


def test_template_declares_fleet_scope_and_retains_compatibility_step() -> None:
    role = (ROOT / "template" / "role.yaml.jinja").read_text(encoding="utf-8")
    copier = (ROOT / "copier.yml").read_text(encoding="utf-8")
    step = (SCRIPTS / "60-bloodbank.sh").read_text(encoding="utf-8")

    assert "gateway_scope: fleet" in role
    assert "enabled: false" in role
    assert "target_agent_id: {{ agent_id | tojson }}" in role
    assert './.scripts/60-bloodbank.sh' in copier
    assert "SKIP_BLOODBANK accepted as a compatibility no-op" in step
    for legacy in ("/dev/tcp", "uv pip install", "bloodbank-consumer.py"):
        assert legacy not in step


def test_documented_gateway_lifecycle_matches_canonical_bloodbank_contract() -> None:
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    for event_type in (
        "bloodbank.v1.conversation.turn.started",
        "bloodbank.v1.agent.invocation.started",
        "bloodbank.v1.agent.invocation.completed",
        "bloodbank.v1.agent.invocation.failed",
        "bloodbank.v1.conversation.turn.completed",
    ):
        assert event_type in architecture
    assert "There are no separate `received` or `accepted` lifecycle events" in architecture
    assert "acknowledged only after Hermes processing completion" in architecture
