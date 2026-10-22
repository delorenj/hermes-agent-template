"""The profile renderer must never delete secret *references* from a delta.

2026-10-21: ``hermes-profile-config.py absorb`` ran over 29 PM profiles. ``minimal_delta`` dropped
every key matching SECRET_KEY_RE -- including the top-level ``secrets`` key -- so each profile's
``secrets.onepassword.env`` block of ``op://`` references vanished, and after the next gateway start
12 Telegram bots and a Slack app were dark. References are pointers, not credentials: they survive.
Plaintext literals under secret-shaped keys are still never re-sprayed into a delta.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import yaml

RENDERER = Path(__file__).parents[1] / "scripts" / "hermes-profile-config.py"

SECRETS = {
    "onepassword": {
        "enabled": True,
        "env": {
            "TELEGRAM_BOT_TOKEN": "op://DeLoSecrets/abc123/credential",
            "SLACK_APP_TOKEN": "op://DeLoSecrets/def456/app_token",
            "AUTOMATICAI_GATEWAY_KEY": "${AUTOMATICAI_GATEWAY_KEY}",
        },
    }
}
BASE = {"model": {"default": "base-model"}, "agent": {"max_turns": 10}}


def _load():
    spec = importlib.util.spec_from_file_location("hermes_profile_config", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fleet(tmp_path: Path, delta: dict | None, current_extra: dict | None = None):
    fleet = tmp_path / ".hermes"
    pdir = fleet / "profiles" / "demo-pm"
    pdir.mkdir(parents=True)
    (fleet / "config.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    if delta is not None:
        (pdir / "config.delta.yaml").write_text(yaml.safe_dump(delta), encoding="utf-8")
    merged = _load().deep_merge(BASE, delta or {})
    merged.update(current_extra or {})
    (pdir / "config.yaml").write_text(yaml.safe_dump(merged), encoding="utf-8")
    return fleet, pdir


def _run(fleet: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_FLEET_HOME": str(fleet), "HERMES_AGENTS_REGISTRY": str(fleet / "none.yaml")}
    return subprocess.run(
        [sys.executable, str(RENDERER), *argv], env=env, capture_output=True, text=True, timeout=60
    )


def _delta(pdir: Path) -> dict:
    return yaml.safe_load((pdir / "config.delta.yaml").read_text(encoding="utf-8")) or {}


def test_minimal_delta_keeps_the_secrets_subtree_references():
    out = _load().minimal_delta({**BASE, "secrets": SECRETS}, BASE)
    assert out["secrets"] == SECRETS


def test_minimal_delta_still_drops_plaintext_under_secret_shaped_keys():
    current = {
        **BASE,
        "api_key": "sk-plaintext-literal",
        "provider": {"api_key": "sk-other-literal", "name": "x", "token": "${GOOD_REF}"},
        "secrets": {"onepassword": {"env": {"A_TOKEN": "hunter2", "B_TOKEN": "op://v/i/f"}}},
    }
    out = _load().minimal_delta(current, BASE)
    assert "api_key" not in out
    assert out["provider"] == {"name": "x", "token": "${GOOD_REF}"}
    assert out["secrets"] == {"onepassword": {"env": {"B_TOKEN": "op://v/i/f"}}}
    assert "hunter2" not in yaml.safe_dump(out)


def test_op_references_with_spaces_in_the_item_title_are_references():
    mod = _load()
    assert mod.is_reference("op://DeLoSecrets/Some Bot Item/credential")
    assert mod.is_reference("${SOME_VAR}")
    assert not mod.is_reference("sk-abc op://x")
    assert not mod.is_reference("op://x\nsk-leak")
    out = mod.minimal_delta({**BASE, "x_token": "op://DeLoSecrets/Some Bot Item/credential"}, BASE)
    assert out["x_token"] == "op://DeLoSecrets/Some Bot Item/credential"


def test_minimal_delta_skips_a_reference_the_base_already_carries():
    base = {**BASE, "secrets": {"onepassword": {"env": {"SHARED_TOKEN": "op://v/shared/f"}}}}
    current = {**base, "secrets": {"onepassword": {"env": {"SHARED_TOKEN": "op://v/shared/f", "X_TOKEN": "op://v/x/f"}}}}
    out = _load().minimal_delta(current, base)
    assert out == {"secrets": {"onepassword": {"env": {"X_TOKEN": "op://v/x/f"}}}}


def test_absorb_keeps_onepassword_references(tmp_path):
    fleet, pdir = _fleet(tmp_path, {"secrets": SECRETS, "agent": {"max_turns": 99}})
    result = _run(fleet, "absorb", "--profile", "demo-pm")
    assert result.returncode == 0, result.stderr
    assert _delta(pdir)["secrets"] == SECRETS
    assert _delta(pdir)["agent"] == {"max_turns": 99}


def test_absorb_carries_references_over_when_the_rendered_config_lost_them(tmp_path):
    fleet, pdir = _fleet(tmp_path, {"secrets": SECRETS})
    # config.yaml without the block (e.g. rewritten by something else): the delta still wins.
    (pdir / "config.yaml").write_text(yaml.safe_dump({**BASE, "agent": {"max_turns": 7}}), encoding="utf-8")
    result = _run(fleet, "absorb", "--profile", "demo-pm")
    assert result.returncode == 0, result.stderr
    assert _delta(pdir)["secrets"] == SECRETS
    assert "carried" in result.stdout
    assert "op://" not in result.stdout  # key paths only, never values


def test_absorb_dry_run_writes_nothing(tmp_path):
    fleet, pdir = _fleet(tmp_path, {"secrets": SECRETS})
    before = (pdir / "config.delta.yaml").read_text(encoding="utf-8")
    result = _run(fleet, "absorb", "--all", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert (pdir / "config.delta.yaml").read_text(encoding="utf-8") == before


def test_absorb_does_not_write_plaintext_into_the_delta(tmp_path):
    fleet, pdir = _fleet(
        tmp_path, {"secrets": SECRETS}, {"secrets": {**SECRETS, "plain_token": "sk-live-literal"}}
    )
    result = _run(fleet, "absorb", "--profile", "demo-pm")
    assert result.returncode == 0, result.stderr
    assert "sk-live-literal" not in (pdir / "config.delta.yaml").read_text(encoding="utf-8")
    assert _delta(pdir)["secrets"] == SECRETS


def test_init_recovery_keeps_references(tmp_path):
    fleet, pdir = _fleet(tmp_path, None, {"secrets": SECRETS, "api_key": "sk-literal"})
    assert not (pdir / "config.delta.yaml").exists()
    result = _run(fleet, "init", "--profile", "demo-pm", "--no-prune-stale")
    assert result.returncode == 0, result.stderr
    delta = _delta(pdir)
    assert delta["secrets"] == SECRETS
    assert "api_key" not in delta
    # and the regenerated config still carries the references
    rendered = yaml.safe_load((pdir / "config.yaml").read_text(encoding="utf-8"))
    assert rendered["secrets"] == SECRETS
