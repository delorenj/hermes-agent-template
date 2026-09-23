"""Named agents: role.yaml `identity:` is validated, projected and pinned.

A post (`agent_id`/`profile`, e.g. `demo-pm`) is held either by an unnamed
agent -- no identity block, compatibility bank `agent-<profile>` -- or by a
NAMED agent whose name, personal bank and chat identity travel with it. These
tests pin the three places the declaration is consumed by the template:
`lib/role-identity.py` (validation), `80-registry.sh` (registry projection) and
`30-telegram.sh` (adopting a bot token that already lives in the vault).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from test_telegram_provisioning import (  # type: ignore[import-not-found]
    BOT_TOKEN,
    REGISTRY_SCRIPT,
    _environment,
    _fake_bin,
    _make_role,
    _prepare_profile,
)

ROOT = Path(__file__).parents[1]
IDENTITY_READER = ROOT / "template" / ".scripts" / "lib" / "role-identity.py"

NAMED_BLOCK = """identity:
  name: grolf
  recall_banks:
    - agent-demo-pm
    - agent-grolf
"""


def _identity(role_yaml: Path, agent_id: str = "demo-pm", profile: str = "demo-pm") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(IDENTITY_READER), str(role_yaml), agent_id, profile],
        text=True,
        capture_output=True,
        check=False,
    )


def _with_identity(role: Path, block: str) -> None:
    role_yaml = role / "role.yaml"
    role_yaml.write_text(role_yaml.read_text(encoding="utf-8") + block, encoding="utf-8")


def test_unnamed_post_reads_as_empty_identity(tmp_path: Path) -> None:
    role, _, _ = _make_role(tmp_path)
    result = _identity(role / "role.yaml")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


def test_named_agent_defaults_bank_and_puts_it_first(tmp_path: Path) -> None:
    role, _, _ = _make_role(tmp_path)
    _with_identity(role, NAMED_BLOCK)
    result = _identity(role / "role.yaml")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "name": "grolf",
        "write_bank": "agent-grolf",
        "recall_banks": ["agent-grolf", "agent-demo-pm"],
    }


def test_identity_refuses_names_and_banks_that_would_leak_memory(tmp_path: Path) -> None:
    cases = {
        # A name equal to the post would pin this agent to agent-<post>, the
        # bank the next unnamed holder of the post inherits.
        "identity:\n  name: demo-pm\n": "is a post id",
        "identity:\n  name: grolf\n  write_bank: agent-demo-pm\n": "must be 'agent-grolf'",
        "identity:\n  name: Grolf\n": "lower-case id",
        "identity:\n  name: grolf\n  recall_banks: [custom]\n": "shared fallback bank",
        "identity:\n  name: grolf\n  nickname: g\n": "unsupported key",
        "identity: grolf\n": "must be a mapping",
        "identity:\n  name: grolf\n  recall_banks: agent-grolf\n": "must be a list",
    }
    for index, (block, message) in enumerate(cases.items()):
        role, _, _ = _make_role(tmp_path / f"case-{index}")
        _with_identity(role, block)
        result = _identity(role / "role.yaml")
        assert result.returncode != 0, block
        assert message in result.stderr, (block, result.stderr)


def _run_registry(role: Path, registry: Path, home: Path) -> subprocess.CompletedProcess[str]:
    shutil.copy2(REGISTRY_SCRIPT, role / ".scripts" / REGISTRY_SCRIPT.name)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")}
    env.update(
        {
            "HOME": str(home),
            "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env"),
            "REGISTRY_FILE": str(registry),
        }
    )
    return subprocess.run(
        ["bash", str(role / ".scripts" / "80-registry.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_registry_projects_named_identity_and_drops_it_when_role_forgets(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _with_identity(role, NAMED_BLOCK)

    named = _run_registry(role, registry, home)
    assert named.returncode == 0, named.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert entry["identity"] == "grolf"
    assert entry["hindsight"] == {
        "write_bank": "agent-grolf",
        "recall_banks": ["agent-grolf", "agent-demo-pm"],
    }
    # The post stays the routing key; nothing about the row's post changes.
    assert entry["profile_name"] == "demo-pm"
    assert entry["bloodbank"]["target_agent_id"] == "demo-pm"

    role_yaml = role / "role.yaml"
    role_yaml.write_text(role_yaml.read_text(encoding="utf-8").replace(NAMED_BLOCK, ""), encoding="utf-8")
    unnamed = _run_registry(role, registry, home)
    assert unnamed.returncode == 0, unnamed.stderr
    entry = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]
    assert "identity" not in entry
    assert "hindsight" not in entry


def test_registry_refuses_an_invalid_identity_without_writing(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _with_identity(role, "identity:\n  name: demo-pm\n")
    before = registry.read_text(encoding="utf-8")
    result = _run_registry(role, registry, home)
    assert result.returncode != 0
    assert "identity" in result.stderr
    assert registry.read_text(encoding="utf-8") == before


def _vault_item(home: Path, item_id: str, field: str, value: str) -> str:
    store = home / ".fake-onepassword"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{item_id}.json").write_text(
        json.dumps({"id": item_id, "title": "Telegram Bot - Demo", "fields": [{"id": field, "value": value}]}),
        encoding="utf-8",
    )
    return f"op://DeLoSecrets/{item_id}/{field}"


def test_telegram_adopts_an_existing_vault_reference_without_staging(tmp_path: Path) -> None:
    role, runtime, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    reference = _vault_item(home, "operatoritem01", "token", BOT_TOKEN)
    bindir = _fake_bin(tmp_path)
    env = _environment(
        registry,
        home,
        bindir,
        {"TELEGRAM_BOT_TOKEN_REF": reference, "TELEGRAM_ALLOWED_USERS": "7777"},
    )
    _prepare_profile(role, home)

    result = subprocess.run(
        ["bash", str(role / ".scripts" / "30-telegram.sh")],
        env=env,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    # Adopted, not copied: the operator's item is the only one in the vault.
    assert sorted(p.name for p in (home / ".fake-onepassword").glob("*.json")) == ["operatoritem01.json"]
    profile = home / ".hermes" / "profiles" / "demo-pm"
    delta_text = (profile / "config.delta.yaml").read_text(encoding="utf-8")
    delta = yaml.safe_load(delta_text)
    assert delta["secrets"]["onepassword"]["env"]["TELEGRAM_BOT_TOKEN"] == reference
    assert delta["platforms"]["telegram"]["enabled"] is True
    assert BOT_TOKEN not in delta_text
    telegram = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]
    assert telegram == {
        "provisioning_status": "verified",
        "bot_username": "verified_demo_bot",
        "bot_id": "424242",
    }
    registry_telegram = yaml.safe_load(registry.read_text(encoding="utf-8"))["agents"]["demo-pm"]["telegram"]
    assert registry_telegram["provisioning_status"] == "verified"
    assert (runtime / ".env").read_text(encoding="utf-8") == 'TELEGRAM_ALLOWED_USERS="7777"\n'


def test_telegram_refuses_a_token_and_a_reference_together(tmp_path: Path) -> None:
    role, _, registry = _make_role(tmp_path)
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    reference = _vault_item(home, "operatoritem02", "token", BOT_TOKEN)
    bindir = _fake_bin(tmp_path)
    env = _environment(
        registry,
        home,
        bindir,
        {"TELEGRAM_BOT_TOKEN_REF": reference, "TELEGRAM_BOT_TOKEN": BOT_TOKEN},
    )
    _prepare_profile(role, home)
    result = subprocess.run(
        ["bash", str(role / ".scripts" / "30-telegram.sh")],
        env=env,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not both" in result.stderr
    assert BOT_TOKEN not in result.stdout + result.stderr
    assert yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))["telegram"]["provisioning_status"] == "deferred"
