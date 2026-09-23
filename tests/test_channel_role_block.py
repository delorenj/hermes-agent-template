"""The channel writers edit the channel's OWN role.yaml block, nothing else.

Regression: the block regex ran under DOTALL, so its body reached end-of-file.
A deferred Telegram on tonnybox-pm (2026-09-23) appended
`provisioning_status: "deferred"` to the last block, `service_state`, and the
verified path would have rewritten slack's `bot_id` instead of adding
telegram's own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "template" / ".scripts" / "channel-transaction.py"
SPEC = importlib.util.spec_from_file_location("channel_transaction_role_block", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
# Registered before exec: its dataclasses resolve their module by name.
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

# The shape of a legacy PM role: the telegram block holds only the handle and
# is closed by a column-0 comment; slack (with its own bot_id) and
# service_state follow it.
ROLE = """\
repo: tonnybox
agent_id: tonnybox-pm

# Telegram bot identity (one bot per agent).
telegram:
  bot_username: "TonnyBoxPMBot"
# Ticket-provider binding (linear | plane | trello).
ticket_provider:
  name: plane
slack:
  provisioning_status: "deferred"
  bot_id: "B-SLACK"

service_state:
  heartbeat: "retired"
  gateway: "deferred"
"""


def test_deferred_status_lands_in_the_telegram_block() -> None:
    updated = yaml.safe_load(MODULE.update_role_status(ROLE.encode(), "telegram", "deferred"))
    assert updated["telegram"] == {"bot_username": "TonnyBoxPMBot", "provisioning_status": "deferred"}
    assert updated["service_state"] == {"heartbeat": "retired", "gateway": "deferred"}
    assert updated["slack"]["provisioning_status"] == "deferred"


def test_verified_metadata_never_rewrites_another_blocks_keys() -> None:
    metadata = {"provisioning_status": "verified", "bot_username": "tonnybox_pm_bot", "bot_id": "123"}
    updated = yaml.safe_load(MODULE.update_role(ROLE.encode(), "telegram", metadata))
    assert updated["telegram"] == metadata
    assert updated["slack"] == {"provisioning_status": "deferred", "bot_id": "B-SLACK"}
    assert "provisioning_status" not in updated["service_state"]


def test_a_second_write_is_a_no_op() -> None:
    once = MODULE.update_role_status(ROLE.encode(), "telegram", "deferred")
    assert MODULE.update_role_status(once, "telegram", "deferred") == once
