"""Ticket-provider states under Krebs lifecycle v2 (krebs/spec/lifecycle.v2.yaml).

Every Plane board has the five stock lanes (Backlog, Todo, In Progress, Done,
Cancelled); what the retired nine-lane layout's extra lanes meant now lives in
labels. So on Plane two normalized targets are label writes:

* `in_review` is the handback: the started lane (In Progress) plus
  `lifecycle:review`.
* `awaiting_decision` (aliases `needs_attention`, `waiting_reply`) is a hold:
  plus `lifecycle:needs-input`, lane unchanged, on a Todo or In Progress ticket
  only. krebs tick is the one thing that moves Todo -> In Progress.

Plane's issue PATCH replaces the whole labels list, so the adapter re-reads the
issue right before its single PATCH and adds only its label to that fresh set.
The read-back must show the label, or the transition is not claimed.

The retired extended targets (`e2e_testing`, `ready_for_documentation`,
`needs_re_evaluation`, `ready_for_e2e`) are gone from the contract. Linear and
Trello have no lifecycle labels here, so their `awaiting_decision` is still a
lane the role names.
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

STOCK_STATES = {
    "results": [
        {"id": "backlog", "name": "Backlog", "group": "backlog"},
        {"id": "todo", "name": "Todo", "group": "unstarted"},
        {"id": "doing", "name": "In Progress", "group": "started"},
        {"id": "done", "name": "Done", "group": "completed"},
        {"id": "cancelled", "name": "Cancelled", "group": "cancelled"},
    ]
}

LIFECYCLE_LABELS = {
    "results": [
        {"id": "lbl-working", "name": "agent:working"},
        {"id": "lbl-triaged", "name": "lifecycle:triaged"},
        {"id": "lbl-review", "name": "lifecycle:review"},
        {"id": "lbl-input", "name": "lifecycle:needs-input"},
        {"id": "lbl-blocked", "name": "lifecycle:blocked"},
        {"id": "lbl-failed", "name": "agent:failed"},
        {"id": "lbl-scope", "name": "repo:game"},
    ]
}


def issue_reads(*bodies: dict[str, Any]) -> dict[str, Any]:
    """GET /issues/issue-uuid/ answering the pre-write read, then the read-back."""
    return {
        "method": "GET",
        "path": "/issues/issue-uuid/",
        "bodies": [json.dumps(body) for body in bodies],
    }


def patches(request_log: Path) -> list[dict[str, Any]]:
    return [json.loads(entry["body"]) for entry in plane_requests(request_log) if entry["method"] == "PATCH"]


def test_plane_resolve_state_in_review_is_in_progress_plus_the_review_label(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
        ],
    )

    result = run_plane(provider, env, "resolve_state", "in_review")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "id": "doing",
        "state": "In Progress",
        "state_type": "started",
        "normalized": "in_review",
        "label": "lifecycle:review",
        "label_id": "lbl-review",
    }
    assert all(entry["method"] == "GET" for entry in plane_requests(request_log))


@pytest.mark.parametrize("state", ["awaiting_decision", "needs_attention", "waiting_reply"])
def test_plane_resolve_state_hold_names_the_label_and_no_lane(tmp_path: Path, state: str) -> None:
    provider, env, request_log = stage_plane(
        tmp_path, responses=[plane_response("GET", "/labels/", LIFECYCLE_LABELS)]
    )

    result = run_plane(provider, env, "resolve_state", state)

    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved == {
        "id": "",
        "state": "",
        "state_type": "",
        "normalized": "awaiting_decision",
        "label": "lifecycle:needs-input",
        "label_id": "lbl-input",
    }
    assert [entry["method"] for entry in plane_requests(request_log)] == ["GET"]


def test_plane_resolve_state_plain_lane_names_no_label(tmp_path: Path) -> None:
    provider, env, _ = stage_plane(
        tmp_path, responses=[plane_response("GET", "/states/", STOCK_STATES)]
    )

    result = run_plane(provider, env, "resolve_state", "unstarted")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "id": "todo",
        "state": "Todo",
        "state_type": "unstarted",
        "normalized": "unstarted",
    }


def test_plane_handback_lands_in_progress_and_adds_review_to_the_fresh_labels(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads(
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": ["lbl-scope", "lbl-triaged", "lbl-working"]},
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": ["lbl-scope", "lbl-triaged", "lbl-working", "lbl-review"]},
            ),
            plane_response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 12}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok 12"
    assert [entry["method"] for entry in plane_requests(request_log)] == ["GET", "GET", "GET", "PATCH", "GET"]
    # Labels other writers own (agent:working, lifecycle:triaged, the scope) survive the write.
    assert patches(request_log) == [
        {"state": "doing", "labels": ["lbl-scope", "lbl-triaged", "lbl-working", "lbl-review"]}
    ]


def test_plane_handback_with_review_already_present_writes_only_the_lane(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads(
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": [{"id": "lbl-review"}]},
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": [{"id": "lbl-review"}]},
            ),
            plane_response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 12}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode == 0, result.stderr
    assert patches(request_log) == [{"state": "doing"}]


def test_plane_handback_never_claims_a_review_label_the_readback_lacks(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads(
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": []},
                {"id": "issue-uuid", "sequence_id": 12, "state": "doing", "labels": []},
            ),
            plane_response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 12}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "read-back labels did not include lifecycle:review" in result.stderr
    assert "concurrent divergence" in result.stderr
    assert "ok" not in result.stdout
    assert len(patches(request_log)) == 1, "exactly one PATCH, never a retry"


def test_plane_handback_refuses_a_board_without_the_review_label(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", {"results": [{"id": "lbl-scope", "name": "repo:game"}]}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "lifecycle label 'lifecycle:review' does not exist" in result.stderr
    assert patches(request_log) == []


def test_plane_label_write_refuses_a_prewrite_read_of_another_issue(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads({"id": "different-issue", "sequence_id": 3, "state": "doing", "labels": []}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "pre-write read did not identify the requested issue" in result.stderr
    assert patches(request_log) == []


@pytest.mark.parametrize("lane", ["todo", "doing"])
def test_plane_hold_adds_needs_input_and_leaves_the_lane(tmp_path: Path, lane: str) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads(
                {"id": "issue-uuid", "sequence_id": 5, "state": lane, "labels": ["lbl-triaged"]},
                {"id": "issue-uuid", "sequence_id": 5, "state": lane, "labels": ["lbl-triaged", "lbl-input"]},
            ),
            plane_response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 5}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "needs_attention")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok 5"
    # No "state" key: a hold never moves the ticket, so a Todo ticket is never dispatched by it.
    assert patches(request_log) == [{"labels": ["lbl-triaged", "lbl-input"]}]


@pytest.mark.parametrize("lane", ["backlog", "done", "cancelled"])
def test_plane_hold_outside_todo_and_in_progress_is_refused(tmp_path: Path, lane: str) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads({"id": "issue-uuid", "sequence_id": 5, "state": lane, "labels": []}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "awaiting_decision")

    assert result.returncode != 0
    assert "is a hold and applies only to a Todo or In Progress ticket" in result.stderr
    assert patches(request_log) == []


def test_plane_hold_already_held_writes_nothing(tmp_path: Path) -> None:
    provider, env, request_log = stage_plane(
        tmp_path,
        responses=[
            plane_response("GET", "/states/", STOCK_STATES),
            plane_response("GET", "/labels/", LIFECYCLE_LABELS),
            issue_reads({"id": "issue-uuid", "sequence_id": 5, "state": "doing", "labels": ["lbl-input"]}),
        ],
    )

    result = run_plane(provider, env, "transition", "issue-uuid", "waiting_reply")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok 5"
    assert patches(request_log) == []


@pytest.mark.parametrize(
    "state", ["e2e_testing", "ready_for_e2e", "ready_for_documentation", "needs_re_evaluation"]
)
def test_plane_retired_nine_lane_targets_are_refused_before_any_request(
    tmp_path: Path, state: str
) -> None:
    provider, env, request_log = stage_plane(tmp_path, responses=[])

    result = run_plane(provider, env, "transition", "issue-uuid", state)

    assert result.returncode != 0
    assert "invalid normalized state" in result.stderr
    assert plane_requests(request_log) == []


def test_linear_awaiting_decision_resolves_to_the_named_state(tmp_path: Path) -> None:
    states = [
        {"id": "doing", "name": "In Progress", "type": "started"},
        {"id": "waiting", "name": "Awaiting Decision", "type": "started"},
        {"id": "done", "name": "Done", "type": "completed"},
    ]

    def respond(query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        if "team{ states" in query:
            return {"issue": {"team": {"states": {"nodes": states}}}}
        if "issueUpdate" in query:
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {"id": "issue-207", "identifier": "TEST-207", "state": {"id": "waiting"}},
                }
            }
        raise AssertionError("unexpected Linear operation")

    with linear_endpoint(respond) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        role_yaml = provider.parents[2] / "role.yaml"
        role_yaml.write_text(
            role_yaml.read_text(encoding="utf-8") + '  awaiting_decision: "Awaiting Decision"\n',
            encoding="utf-8",
        )
        result = run_provider(provider, env, "transition", "issue-207", "needs_attention")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok TEST-207"
    assert requests[-1]["variables"]["s"] == "waiting"


def test_linear_unconfigured_awaiting_decision_is_refused_before_any_request(
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


def test_trello_awaiting_decision_resolves_by_configured_list(tmp_path: Path) -> None:
    provider, env, log = stage_trello(
        tmp_path,
        lists=[{"id": "done-list", "name": "Done"}, {"id": "wait-list", "name": "Waiting"}],
    )
    role_yaml = provider.parents[2] / "role.yaml"
    role_yaml.write_text(role_yaml.read_text(encoding="utf-8") + '  awaiting_decision: "Waiting"\n', encoding="utf-8")

    result = run_provider(provider, env, "resolve_state", "waiting_reply")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "wait-list"
    assert [row["method"] for row in trello_requests(log)] == ["GET"]


def test_trello_unconfigured_awaiting_decision_is_refused(tmp_path: Path) -> None:
    provider, env, log = stage_trello(tmp_path, lists=[{"id": "done-list", "name": "Done"}])

    result = run_provider(provider, env, "transition", "card-207", "awaiting_decision")

    assert result.returncode != 0
    assert "ticket_provider.awaiting_decision is required" in result.stderr
    assert "PUT" not in [row["method"] for row in trello_requests(log)]


@pytest.mark.parametrize(
    ("state", "valid"),
    [
        ("backlog", True),
        ("unstarted", True),
        ("started", True),
        ("in_review", True),
        ("completed", True),
        ("cancelled", True),
        ("awaiting_decision", True),
        ("needs_attention", True),
        ("waiting_reply", True),
        ("e2e_testing", False),
        ("ready_for_e2e", False),
        ("ready_for_documentation", False),
        ("needs_re_evaluation", False),
        ("in_progress", False),
        ("", False),
    ],
)
def test_dispatcher_accepts_the_lifecycle_v2_state_contract(state: str, valid: bool) -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1" >/dev/null 2>&1 || true; tp_is_valid_state "$2"', "tp", str(DISPATCHER), state],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stderr
