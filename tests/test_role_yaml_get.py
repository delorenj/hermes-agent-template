"""`yaml_get` in template/.scripts/_lib.sh reads one scalar from ONE block.

The walker it replaced found the parent key and then searched the rest of the
file for the leaf, so every dotted lookup could be answered by a LATER block
whenever its own block lacked the key. These cases pin the boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
LIB = ROOT / "template" / ".scripts" / "_lib.sh"

ROLE_YAML = """\
# header comment
repo: demo
role: pm
agent_id: "demo-pm"
display_name: 'Demo ''PM'''
model:
  provider: ""
  key_env: DEMO_KEY   # a variable NAME only
telegram:
  provisioning_status: "verified"
  bot_username: demo_pm_bot
slack:
  provisioning_status: "deferred"
  bot_id: "B-SLACK"

ticket_provider:
  name: plane
  board_url: "https://plane.example/demo/projects/0c0f017d-ea58-4196-95a3-088261\\
    ed06b3/issues/"
  # a comment inside the block
  workspace: "demo"
bloodbank:
  gateway_scope: fleet
  target_agent_id: "demo-pm"
reconcile:
  enabled: false
  explicit_opt_out: true  # distinguishes operator choice from the legacy default
runtime:
  checkpoint:
    cadence: "disabled"
purpose: |
  Line one.
  Line two.
"""


def _yaml_get(tmp_path: Path, key: str, text: str = ROLE_YAML) -> subprocess.CompletedProcess[str]:
    role = tmp_path / "role"
    (role / ".scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(LIB, role / ".scripts" / "_lib.sh")
    if not (role / ".scripts" / "lib").exists():
        shutil.copytree(LIB.parent / "lib", role / ".scripts" / "lib")
    (role / "role.yaml").write_text(text, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": str(home), "HERMES_FLEET_ENV": str(home / ".hermes" / "fleet.env")})
    return subprocess.run(
        ["bash", "-c", 'source "$1"; yaml_get "$2"', "yaml-get", str(role / ".scripts" / "_lib.sh"), key],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        # An absent leaf is absent, never a later block's key of the same name.
        ("bloodbank.enabled", ""),
        ("telegram.bot_id", ""),
        ("model.name", ""),
        ("plane.identifier", ""),
        # Present leaves: quotes removed, trailing comments dropped.
        ("bloodbank.gateway_scope", "fleet"),
        ("bloodbank.target_agent_id", "demo-pm"),
        ("reconcile.explicit_opt_out", "true"),
        ("reconcile.enabled", "false"),
        ("model.key_env", "DEMO_KEY"),
        ("model.provider", ""),
        ("slack.bot_id", "B-SLACK"),
        ("telegram.bot_username", "demo_pm_bot"),
        ("agent_id", "demo-pm"),
        ("display_name", "Demo 'PM'"),
        # A blank line and a comment inside a block do not end it.
        ("ticket_provider.workspace", "demo"),
        ("ticket_provider.board_url", "https://plane.example/demo/projects/0c0f017d-ea58-4196-95a3-088261ed06b3/issues/"),
        # Deeper nesting is reached only through its own parent.
        ("runtime.cadence", ""),
        ("runtime.checkpoint.cadence", "disabled"),
        ("cadence", ""),
        ("purpose", "Line one.\nLine two."),
        ("nope", ""),
        ("nope.enabled", ""),
    ),
)
def test_yaml_get_is_scoped_to_one_block(tmp_path: Path, key: str, expected: str) -> None:
    result = _yaml_get(tmp_path, key)
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == expected


def test_yaml_get_refuses_a_duplicated_key(tmp_path: Path) -> None:
    result = _yaml_get(tmp_path, "bloodbank.gateway_scope", ROLE_YAML + "bloodbank:\n  gateway_scope: host\n")
    assert result.returncode != 0
    assert "duplicate key 'bloodbank'" in result.stderr
    assert result.stdout == ""


def test_yaml_get_refuses_to_descend_into_a_scalar(tmp_path: Path) -> None:
    result = _yaml_get(tmp_path, "repo.enabled")
    assert result.returncode != 0
    assert "not a block mapping" in result.stderr
