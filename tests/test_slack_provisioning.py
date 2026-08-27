from __future__ import annotations

import os
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
SLACK_SCRIPT = ROOT / "template" / ".scripts" / "31-slack.sh"
LIB_SCRIPT = ROOT / "template" / ".scripts" / "_lib.sh"
LIB_DIR = ROOT / "template" / ".scripts" / "lib"
REGISTRY_SCRIPT = ROOT / "template" / ".scripts" / "80-registry.sh"
STORE_HELPER = ROOT / "template" / ".scripts" / "store-onepassword-secret.py"
FAKE_OP = ROOT / "tests" / "support" / "fake-op.py"

BOT_TOKEN = "xoxb-profile-only-secret"
APP_TOKEN = "xapp-profile-only-secret"
ROTATED_BOT_TOKEN = "xoxb-rotated-profile-secret"
ROTATED_APP_TOKEN = "xapp-rotated-profile-secret"


def _make_role(tmp_path: Path) -> tuple[Path, Path, Path]:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    runtime = role / "runtime"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    shutil.copy2(SLACK_SCRIPT, scripts / SLACK_SCRIPT.name)
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
  bot_username: "demo_pm_bot"
slack:
  provisioning_status: "deferred"
  team_id: ""
  team_name: ""
  bot_user_id: ""
  bot_id: ""
  bot_username: ""
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


def _fake_curl(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    dirname = bindir / "dirname"
    dirname.write_text(
        """#!/usr/bin/env python3
import os
import pathlib
import sys

environ = pathlib.Path("/proc/self/environ").read_bytes()
for raw in (
    "xoxb-profile-only-secret",
    "xapp-profile-only-secret",
    "xoxb-rotated-profile-secret",
    "xapp-rotated-profile-secret",
):
    assert raw.encode() not in environ
pathlib.Path(os.environ["EARLY_CHILD_PROBE"]).write_text("clean\\n", encoding="utf-8")
value = sys.argv[1].rstrip("/")
print(str(pathlib.Path(value).parent) if value else "/")
""",
        encoding="utf-8",
    )
    dirname.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys

assert sys.argv[1:] == ["--config", "-"]
config = sys.stdin.read()
match = re.search(r'Authorization: Bearer ([A-Za-z0-9-]+)', config)
assert match
token = match.group(1)
cmdline = pathlib.Path("/proc/self/cmdline").read_bytes()
environ = pathlib.Path("/proc/self/environ").read_bytes()
assert token.encode() not in cmdline
assert token.encode() not in environ
if 'url = "https://slack.com/api/auth.test"' in config:
    assert token.startswith("xoxb-")
    print(json.dumps({
        "ok": True,
        "team_id": "T123",
        "team": "Example Workspace",
        "user_id": "U123BOT",
        "bot_id": "B123BOT",
        "user": "demo-pm",
    }))
elif 'url = "https://slack.com/api/bots.info"' in config:
    assert token.startswith("xoxb-")
    if os.environ.get("FAKE_SLACK_BOTS_INFO_MISSING_SCOPE") == "1":
        print(json.dumps({"ok": False, "error": "missing_scope", "needed": "users:read"}))
    else:
        print(json.dumps({"ok": True, "bot": {"app_id": "A123APP"}}))
elif 'url = "https://slack.com/api/apps.connections.open"' in config:
    assert token.startswith("xapp-")
    if os.environ.get("FAKE_SLACK_APP_AUTH_FAILURE") == "1":
        print(json.dumps({"ok": False, "error": "invalid_auth"}))
    else:
        app_id = "A999OTHER" if os.environ.get("FAKE_SLACK_APP_MISMATCH") == "1" else "A123APP"
        print(json.dumps({"ok": True, "url": f"wss://wss-primary.slack.test/link?ticket=test&app_id={app_id}"}))
else:
    raise AssertionError("unexpected Slack API endpoint")
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
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
        if not key.startswith("SLACK_") and key not in {"ENABLE_SLACK", "WIRE_SLACK"}
    }
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bindir}:{env['PATH']}",
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
            "UNRELATED_PROVIDER_SECRET": "must-not-reach-op",
            "EARLY_CHILD_PROBE": str(bindir.parent / ".slack-early-child"),
        }
    )
    env.update(overrides or {})
    profile_name = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["profile"]
    fleet_home = home / ".hermes"
    profile = fleet_home / "profiles" / profile_name
    profile.mkdir(parents=True, exist_ok=True)
    (fleet_home / "config.yaml").write_text(
        "plugins: {}\nplatforms:\n  slack:\n    enabled: true\n",
        encoding="utf-8",
    )
    delta = profile / "config.delta.yaml"
    if not delta.exists():
        delta.write_text("{}\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(role / ".scripts" / "31-slack.sh")],
        env=env,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )


def test_slack_is_deferred_by_default_and_ignores_shared_fleet_tokens(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(
        "SLACK_BOT_TOKEN=xoxb-shared\nSLACK_APP_TOKEN=xapp-shared\n"
        "SLACK_ALLOWED_USERS=U999\n",
        encoding="utf-8",
    )

    result = _run(role, registry, home, _fake_curl(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "deferred" in result.stderr
    assert not (runtime / ".env").exists()
    assert not (role / ".scripts" / ".done-31-slack").exists()
    assert "xoxb-shared" in fleet.read_text(encoding="utf-8")
    generated = home / ".hermes" / "profiles" / "demo-pm" / "config.yaml"
    assert yaml.safe_load(generated.read_text(encoding="utf-8"))["platforms"][
        "slack"
    ]["enabled"] is False


def test_explicit_noninteractive_enable_requires_both_tokens(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        role,
        registry,
        home,
        _fake_curl(tmp_path),
        {"ENABLE_SLACK": "1", "SLACK_BOT_TOKEN": BOT_TOKEN},
    )

    assert result.returncode != 0
    assert "requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_invocation_tokens_are_cleared_before_first_path_child(tmp_path: Path) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_curl(tmp_path)

    result = _run(
        role,
        registry,
        home,
        bindir,
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".slack-early-child").read_text(encoding="utf-8") == "clean\n"


def test_slack_rejects_symlinked_profile_root_without_touching_target(
    tmp_path: Path,
) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    profiles = home / ".hermes" / "profiles"
    profiles.mkdir(parents=True)
    target = tmp_path / "shared-profile-target"
    target.mkdir()
    sentinel = target / "operator-state"
    delta = target / "config.delta.yaml"
    sentinel.write_bytes(b"do-not-touch\n")
    delta.write_bytes(b"platforms:\n  slack:\n    enabled: true\n")
    (profiles / "demo-pm").symlink_to(target, target_is_directory=True)
    before = {path.name: path.read_bytes() for path in target.iterdir()}

    result = _run(role, registry, home, _fake_curl(tmp_path))

    assert result.returncode != 0
    assert "refusing symlinked profile root" in result.stderr
    assert "runtime-singleton migration" in result.stderr
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before


def test_both_tokens_store_references_and_only_nonsecret_policy(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True)
    fleet.write_text("SLACK_ALLOWED_USERS=U111,U222\n", encoding="utf-8")
    shared = home / ".hermes" / ".env"
    shared.write_text("PROVIDER_KEY=keep-me\n", encoding="utf-8")

    result = _run(
        role,
        registry,
        home,
        _fake_curl(tmp_path),
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )

    assert result.returncode == 0, result.stderr
    assert "verifying Slack Socket Mode app token via apps.connections.open" in result.stderr
    env_file = runtime / ".env"
    env_text = env_file.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in env_text
    assert "SLACK_APP_TOKEN" not in env_text
    assert 'SLACK_ALLOWED_USERS="U111,U222"' in env_text
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"
    assert fleet.read_text(encoding="utf-8") == "SLACK_ALLOWED_USERS=U111,U222\n"
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert APP_TOKEN not in result.stdout + result.stderr
    profile = home / ".hermes" / "profiles" / "demo-pm"
    delta_text = (profile / "config.delta.yaml").read_text(encoding="utf-8")
    delta = yaml.safe_load(delta_text)
    references = delta["secrets"]["onepassword"]["env"]
    bot_reference = references["SLACK_BOT_TOKEN"]
    app_reference = references["SLACK_APP_TOKEN"]
    assert bot_reference.startswith(
        "op://DeLoSecrets/hermes-agent-demo-pm-slack-credentials-v-"
    )
    assert bot_reference.endswith("/slack_bot_token")
    assert app_reference.endswith("/slack_app_token")
    assert bot_reference.rsplit("/", 1)[0] == app_reference.rsplit("/", 1)[0]
    assert BOT_TOKEN not in delta_text
    assert APP_TOKEN not in delta_text
    generated = (profile / "config.yaml").read_text(encoding="utf-8")
    assert BOT_TOKEN not in generated and APP_TOKEN not in generated
    assert yaml.safe_load(generated)["platforms"]["slack"]["enabled"] is True

    slack = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["slack"]
    assert slack == {
        "provisioning_status": "verified",
        "team_id": "T123",
        "team_name": "Example Workspace",
        "bot_user_id": "U123BOT",
        "bot_id": "B123BOT",
        "bot_username": "demo-pm",
    }


def test_bot_auth_success_without_app_auth_never_enables_or_stores_slack(
    tmp_path: Path,
) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        role,
        registry,
        home,
        _fake_curl(tmp_path),
        {
            "SLACK_BOT_TOKEN": BOT_TOKEN,
            "SLACK_APP_TOKEN": APP_TOKEN,
            "FAKE_SLACK_APP_AUTH_FAILURE": "1",
        },
    )

    assert result.returncode != 0
    assert "apps.connections.open rejected the app token (invalid_auth)" in result.stderr
    assert "Socket Mode app-token verification failed" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert APP_TOKEN not in result.stdout + result.stderr
    assert not (role / ".scripts" / ".done-31-slack").exists()
    assert not (runtime / ".env").exists()
    delta_path = home / ".hermes" / "profiles" / "demo-pm" / "config.delta.yaml"
    delta = yaml.safe_load(delta_path.read_text(encoding="utf-8"))
    assert delta["platforms"]["slack"]["enabled"] is False
    assert "secrets" not in delta
    slack = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["slack"]
    assert slack["provisioning_status"] == "deferred"
    assert not (home / ".fake-onepassword").exists()


def test_slack_requires_users_read_to_correlate_bot_app_identity(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        role,
        registry,
        home,
        _fake_curl(tmp_path),
        {
            "SLACK_BOT_TOKEN": BOT_TOKEN,
            "SLACK_APP_TOKEN": APP_TOKEN,
            "FAKE_SLACK_BOTS_INFO_MISSING_SCOPE": "1",
        },
    )

    assert result.returncode != 0
    assert "requires users:read" in result.stderr
    assert "reinstall the app" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert APP_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()
    delta = yaml.safe_load(
        (home / ".hermes" / "profiles" / "demo-pm" / "config.delta.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert delta["platforms"]["slack"]["enabled"] is False
    assert "secrets" not in delta


def test_independently_valid_slack_tokens_from_different_apps_are_rejected(
    tmp_path: Path,
) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    result = _run(
        role,
        registry,
        home,
        _fake_curl(tmp_path),
        {
            "SLACK_BOT_TOKEN": BOT_TOKEN,
            "SLACK_APP_TOKEN": APP_TOKEN,
            "FAKE_SLACK_APP_MISMATCH": "1",
        },
    )

    assert result.returncode != 0
    assert "belong to different apps" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert APP_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()
    assert not (home / ".fake-onepassword").exists()


def test_slack_pair_rotation_is_atomic_when_second_field_write_fails(
    tmp_path: Path,
) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_curl(tmp_path)
    first = _run(
        role,
        registry,
        home,
        bindir,
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )
    assert first.returncode == 0, first.stderr
    profile = home / ".hermes" / "profiles" / "demo-pm"
    delta_before = (profile / "config.delta.yaml").read_bytes()
    role_before = (role / "role.yaml").read_bytes()
    item_paths_before = sorted((home / ".fake-onepassword").glob("*.json"))
    assert len(item_paths_before) == 1
    item_before = item_paths_before[0].read_bytes()
    (home / ".fake-onepassword-reject-field").write_text(
        "slack_app_token\n", encoding="utf-8"
    )

    rotated = _run(
        role,
        registry,
        home,
        bindir,
        {
            "SLACK_BOT_TOKEN": ROTATED_BOT_TOKEN,
            "SLACK_APP_TOKEN": ROTATED_APP_TOKEN,
        },
    )

    assert rotated.returncode != 0
    assert "credential pair could not be staged" in rotated.stderr
    assert (profile / "config.delta.yaml").read_bytes() == delta_before
    assert (role / "role.yaml").read_bytes() == role_before
    item_paths_after = sorted((home / ".fake-onepassword").glob("*.json"))
    assert item_paths_after == item_paths_before
    assert item_paths_after[0].read_bytes() == item_before
    combined = rotated.stdout + rotated.stderr
    for token in (BOT_TOKEN, APP_TOKEN, ROTATED_BOT_TOKEN, ROTATED_APP_TOKEN):
        assert token not in combined


def test_transient_onepassword_outage_preserves_verified_slack_wiring_and_recovers(
    tmp_path: Path,
) -> None:
    role, _runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = _fake_curl(tmp_path)

    first = _run(
        role,
        registry,
        home,
        bindir,
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )
    assert first.returncode == 0, first.stderr

    role_before = (role / "role.yaml").read_bytes()
    delta_path = home / ".hermes" / "profiles" / "demo-pm" / "config.delta.yaml"
    delta_before = delta_path.read_bytes()
    marker = role / ".scripts" / ".done-31-slack"
    assert marker.is_file()

    (home / ".fake-onepassword-outage").touch()
    outage = _run(role, registry, home, bindir)

    assert outage.returncode != 0
    assert "temporarily unavailable" in outage.stderr
    assert (role / "role.yaml").read_bytes() == role_before
    assert delta_path.read_bytes() == delta_before
    assert marker.is_file()
    assert BOT_TOKEN not in outage.stdout + outage.stderr
    assert APP_TOKEN not in outage.stdout + outage.stderr

    (home / ".fake-onepassword-outage").unlink()
    recovered = _run(role, registry, home, bindir)

    assert recovered.returncode == 0, recovered.stderr
    assert "existing verified 1Password wiring reconciled" in recovered.stderr
    assert (role / "role.yaml").read_bytes() == role_before
    assert delta_path.read_bytes() == delta_before
    assert marker.is_file()


def test_rejects_reused_pair_without_disclosing_tokens(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    other_role = tmp_path / "other-role"
    other_runtime = other_role / "runtime"
    other_runtime.mkdir(parents=True)
    (other_runtime / ".env").write_text(
        f'SLACK_BOT_TOKEN="{BOT_TOKEN}"\nSLACK_APP_TOKEN="{APP_TOKEN}"\n',
        encoding="utf-8",
    )
    registry.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agents": {
                    "other-pm": {
                        "role_dir": str(other_role),
                        "slack": {"provisioning_status": "verified"},
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
        _fake_curl(tmp_path),
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )

    assert result.returncode != 0
    assert "already assigned to agent other-pm" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert APP_TOKEN not in result.stdout + result.stderr
    assert not (runtime / ".env").exists()


def test_refuses_runtime_env_symlink_before_auth_test(tmp_path: Path) -> None:
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
        _fake_curl(tmp_path),
        {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN},
    )

    assert result.returncode != 0
    assert "refusing to write Slack credentials through symlink" in result.stderr
    assert shared.read_text(encoding="utf-8") == "PROVIDER_KEY=keep-me\n"


def test_registry_persists_identity_metadata_without_tokens(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy2(REGISTRY_SCRIPT, role / ".scripts" / REGISTRY_SCRIPT.name)
    role_yaml = role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8")
        .replace('provisioning_status: "deferred"', 'provisioning_status: "verified"')
        .replace('team_id: ""', 'team_id: "T123"')
        .replace('team_name: ""', 'team_name: "Example Workspace"')
        .replace('bot_user_id: ""', 'bot_user_id: "U123BOT"')
        .replace('bot_id: ""', 'bot_id: "B123BOT"')
        .replace('bot_username: ""', 'bot_username: "demo-pm"'),
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SLACK_")
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
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert entry["slack"] == {
        "provisioning_status": "verified",
        "team_id": "T123",
        "team_name": "Example Workspace",
        "bot_user_id": "U123BOT",
        "bot_id": "B123BOT",
        "bot_username": "demo-pm",
    }
    serialized = registry.read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in serialized
    assert "SLACK_APP_TOKEN" not in serialized


def test_concurrent_profiles_cannot_claim_same_slack_identity(tmp_path: Path) -> None:
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
    bindir = _fake_curl(tmp_path)
    overrides = {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN}

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
    assert APP_TOKEN not in combined
    agents = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]
    assert len(agents) == 1
    assert next(iter(agents.values()))["slack"]["bot_id"] == "B123BOT"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{registry}.lock").stat().st_mode) == 0o600
