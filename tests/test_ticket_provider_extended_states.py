"""The four extended transition targets are per-role configuration.

`awaiting_decision`, `e2e_testing`, `ready_for_documentation` and
`needs_re_evaluation` used to exist only in two role directories that had
forked the providers (voxxy-pm, james-brennan-pm). They are template contract
now, enabled per role: a target resolves only when that role's role.yaml
`ticket_provider:` block names its lane, and is refused by name otherwise. The
aliases `needs_attention`/`waiting_reply` -> `awaiting_decision` and
`ready_for_e2e` -> `e2e_testing` resolve to the same lane. Plane keeps its
historical default of "Needs Attention" for `awaiting_decision`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from test_plane_board_control import requests as plane_requests
from test_plane_board_control import response as plane_response
from test_plane_board_control import run_provider as run_plane
from test_plane_board_control import stage_provider as stage_plane
from test_provider_transition_hardening import linear_endpoint
from test_provider_transition_hardening import run_provider
from test_provider_transition_hardening import stage_linear
from test_provider_transition_hardening import stage_trello
from test_provider_transition_hardening import trello_requests


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "template" / ".scripts" / "lib" / "ticket-provider.sh"

PLANE_STATES = {
    "results": [
        {"id": "todo", "name": "Todo", "group": "unstarted"},
        {"id": "doing", "name": "In Progress", "group": "started"},
        {"id": "attention", "name": "Needs Attention", "group": "started"},
        {"id": "e2e", "name": "E2E Testing & QA", "group": "started"},
        {"id": "docs", "name": "Ready for Documentation", "group": "started"},
        {"id": "reeval", "name": "Needs Re-evaluation", "group": "unstarted"},
        {"id": "done", "name": "Done", "group": "completed"},
    ]
}

EXTENDED = (
    '  e2e_testing: "E2E Testing & QA"\n'
    '  ready_for_documentation: "Ready for Documentation"\n'
    '  needs_re_evaluation: "Needs Re-evaluation"\n'
)


def _extend_role(provider: Path, lines: str) -> None:
    role_yaml = provider.parents[2] / "role.yaml"
    role_yaml.write_text(role_yaml.read_text(encoding="utf-8") + lines, encoding="utf-8")


@pytest.mark.parametrize(
    ("state", "expected_id"),
    [
        ("e2e_testing", "e2e"),
        ("ready_for_e2e", "e2e"),
        ("ready_for_documentation", "docs"),
        ("needs_re_evaluation", "reeval"),
        ("awaiting_decision", "attention"),
        ("needs_attention", "attention"),
        ("waiting_reply", "attention"),
    ],
)
def test_plane_resolve_state_reads_configured_extended_lanes(
    tmp_path: Path, state: str, expected_id: str
) -> None:
    provider, env, request_log = stage_plane(
        tmp_path, responses=[plane_response("GET", "/states/", PLANE_STATES)]
    )
    _extend_role(provider, EXTENDED)

    result = run_plane(provider, env, "resolve_state", state)

    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["id"] == expected_id
    assert resolved["normalized"] in {
        "e2e_testing",
        "ready_for_documentation",
        "needs_re_evaluation",
        "awaiting_decision",
    }
    assert [entry["method"] for entry in plane_requests(request_log)] == ["GET"]


@pytest.mark.parametrize(
    "state", ["e2e_testing", "ready_for_e2e", "ready_for_documentation", "needs_re_evaluation"]
)
def test_plane_unconfigured_extended_target_is_refused_before_any_write(
    tmp_path: Path, state: str
) -> None:
    provider, env, request_log = stage_plane(
        tmp_path, responses=[plane_response("GET", "/states/", PLANE_STATES)]
    )

    result = run_plane(provider, env, "transition", "issue-uuid", state)

    assert result.returncode != 0
    assert "is required in role.yaml" in result.stderr
    assert "ok" not in result.stdout
    assert all(entry["method"] == "GET" for entry in plane_requests(request_log))


def test_plane_transition_to_an_alias_patches_the_configured_lane(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", PLANE_STATES),
            plane_response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 9}),
            plane_response(
                "GET", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 9, "state": "e2e"}
            ),
        ],
    )
    _extend_role(provider, EXTENDED)

    result = run_plane(provider, env, "transition", "issue-uuid", "ready_for_e2e")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok")
    patches = [entry for entry in plane_requests(request_log) if entry["method"] == "PATCH"]
    assert len(patches) == 1
    assert json.loads(patches[0]["body"]) == {"state": "e2e"}


def test_plane_extended_lane_in_the_wrong_group_is_refused(tmp_path: Path) -> None:
    provider, env, _ = stage_plane(
        tmp_path, responses=[plane_response("GET", "/states/", PLANE_STATES)]
    )
    # needs_re_evaluation must be an unstarted-group lane; naming a started one
    # never falls back to some other lane.
    _extend_role(provider, '  needs_re_evaluation: "In Progress"\n')

    result = run_plane(provider, env, "resolve_state", "needs_re_evaluation")

    assert result.returncode != 0
    assert "needs_re_evaluation" in result.stderr


def test_linear_extended_target_resolves_on_the_issue_team(tmp_path: Path) -> None:
    states = [
        {"id": "doing", "name": "In Progress", "type": "started"},
        {"id": "e2e", "name": "E2E Testing & QA", "type": "started"},
        {"id": "done", "name": "Done", "type": "completed"},
    ]

    def respond(query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        if "team{ states" in query:
            return {"issue": {"team": {"states": {"nodes": states}}}}
        if "issueUpdate" in query:
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {"id": "issue-207", "identifier": "TEST-207", "state": {"id": "e2e"}},
                }
            }
        raise AssertionError("unexpected Linear operation")

    with linear_endpoint(respond) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        _extend_role(provider, '  e2e_testing: "E2E Testing & QA"\n')
        result = run_provider(provider, env, "transition", "issue-207", "ready_for_e2e")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok TEST-207"
    assert requests[-1]["variables"]["s"] == "e2e"


def test_linear_unconfigured_extended_target_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    def respond(query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no request may be sent for an unconfigured target")

    with linear_endpoint(respond) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "transition", "issue-207", "awaiting_decision")

    assert result.returncode != 0
    assert "ticket_provider.awaiting_decision is required" in result.stderr
    assert requests == []


def test_trello_extended_target_resolves_by_configured_list(tmp_path: Path) -> None:
    provider, env, log = stage_trello(
        tmp_path,
        lists=[{"id": "done-list", "name": "Done"}, {"id": "qa-list", "name": "QA"}],
    )
    _extend_role(provider, '  e2e_testing: "QA"\n')

    result = run_provider(provider, env, "resolve_state", "ready_for_e2e")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "qa-list"
    assert [row["method"] for row in trello_requests(log)] == ["GET"]


def test_trello_unconfigured_extended_target_is_refused(tmp_path: Path) -> None:
    provider, env, log = stage_trello(tmp_path, lists=[{"id": "done-list", "name": "Done"}])

    result = run_provider(provider, env, "transition", "card-207", "needs_re_evaluation")

    assert result.returncode != 0
    assert "ticket_provider.needs_re_evaluation is required" in result.stderr
    assert "PUT" not in [row["method"] for row in trello_requests(log)]


@pytest.mark.parametrize(
    ("state", "valid"),
    [
        ("started", True),
        ("awaiting_decision", True),
        ("e2e_testing", True),
        ("ready_for_documentation", True),
        ("needs_re_evaluation", True),
        ("needs_attention", True),
        ("waiting_reply", True),
        ("ready_for_e2e", True),
        ("in_progress", False),
        ("", False),
    ],
)
def test_dispatcher_accepts_the_twelve_state_contract(state: str, valid: bool) -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1" >/dev/null 2>&1 || true; tp_is_valid_state "$2"', "tp", str(DISPATCHER), state],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stderr
