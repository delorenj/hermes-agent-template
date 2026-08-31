from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
LINEAR = ROOT / "template" / ".scripts" / "providers" / "linear.sh"
TRELLO = ROOT / "template" / ".scripts" / "providers" / "trello.sh"


def run_provider(
    provider: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(provider), *args],
        cwd=provider.parents[5],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@contextmanager
def linear_endpoint(
    responder: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            try:
                data = responder(request["query"], request.get("variables") or {})
                body = json.dumps({"data": data}).encode()
                self.send_response(200)
            except Exception as exc:  # pragma: no cover - turns fixture bugs visible
                body = json.dumps({"errors": [{"message": str(exc)}]}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/graphql", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def stage_linear(tmp_path: Path, endpoint: str) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "linear"
    role = repo / "agents" / "hermes" / "pm"
    provider = role / ".scripts" / "providers" / "linear.sh"
    provider.parent.mkdir(parents=True)
    shutil.copy2(LINEAR, provider)
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "project_slug": "linear-fixture",
                "ticket_provider": {
                    "type": "linear",
                    "team": "TEST",
                    "identifier": "TEST",
                },
            }
        ),
        encoding="utf-8",
    )
    (role / "role.yaml").write_text(
        """repo: linear-fixture
ticket_provider:
  name: linear
  team: TEST
  completed: Done
  cancelled: Canceled
  in_review: In Review
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["LINEAR_API_KEY"] = "linear-test-key"
    env["LINEAR_GRAPHQL_URL"] = endpoint
    env.pop("LINEAR_MAX_PAGES", None)
    return provider, env


def issue_page(after: str | None, next_cursor: str | None) -> dict[str, Any]:
    return {
        "issues": {
            "nodes": [
                {
                    "id": f"issue-{after or 'first'}",
                    "identifier": f"TEST-{after or 'first'}",
                    "title": "Fixture",
                    "updatedAt": "2026-08-31T00:00:00Z",
                    "url": "https://linear.app/fixture",
                    "state": {"name": "Todo", "type": "unstarted"},
                    "assignee": None,
                }
            ],
            "pageInfo": {
                "hasNextPage": next_cursor is not None,
                "endCursor": next_cursor,
            },
        }
    }


def test_linear_pagination_detects_non_adjacent_repeated_cursor(
    tmp_path: Path,
) -> None:
    def responder(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert "issues(first:100" in query
        after = variables.get("after")
        return issue_page(after, {None: "A", "A": "B", "B": "A"}[after])

    with linear_endpoint(responder) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "list_issues")

    assert result.returncode != 0
    assert "cursor repeated" in result.stderr
    assert result.stdout == ""
    assert [row["variables"].get("after") for row in requests] == [None, "A", "B"]


def test_linear_pagination_max_page_guard_returns_no_partial_output(
    tmp_path: Path,
) -> None:
    def responder(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        after = variables.get("after")
        return issue_page(after, "A" if after is None else "B")

    with linear_endpoint(responder) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        env["LINEAR_MAX_PAGES"] = "2"
        result = run_provider(provider, env, "list_issues")

    assert result.returncode != 0
    assert "LINEAR_MAX_PAGES=2" in result.stderr
    assert result.stdout == ""
    assert len(requests) == 2


@pytest.mark.parametrize("value", ["0", "1001", "many"])
def test_linear_invalid_max_pages_fails_before_network(
    tmp_path: Path, value: str
) -> None:
    def responder(_query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("network must not be reached")

    with linear_endpoint(responder) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        env["LINEAR_MAX_PAGES"] = value
        result = run_provider(provider, env, "list_issues")

    assert result.returncode != 0
    assert "LINEAR_MAX_PAGES" in result.stderr
    assert requests == []


def transition_responder(
    states: list[dict[str, str]], mutation: dict[str, Any]
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    def respond(query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        if "team{ states" in query:
            return {"issue": {"team": {"states": {"nodes": states}}}}
        if "issueUpdate" in query:
            return {"issueUpdate": mutation}
        raise AssertionError("unexpected Linear operation")

    return respond


def test_linear_configured_state_name_must_be_unique(tmp_path: Path) -> None:
    states = [
        {"id": "done-1", "name": "Done", "type": "completed"},
        {"id": "done-2", "name": "done", "type": "completed"},
    ]
    with linear_endpoint(
        transition_responder(states, {"success": True})
    ) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "transition", "issue-207", "completed")

    assert result.returncode != 0
    assert "resolved 2 states" in result.stderr
    assert "ok" not in result.stdout
    assert len(requests) == 1


def test_linear_unconfigured_state_type_must_be_unique(tmp_path: Path) -> None:
    states = [
        {"id": "started-1", "name": "In Progress", "type": "started"},
        {"id": "started-2", "name": "Review", "type": "started"},
    ]
    with linear_endpoint(
        transition_responder(states, {"success": True})
    ) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "transition", "issue-207", "started")

    assert result.returncode != 0
    assert "workflow type" in result.stderr
    assert "resolved 2 states" in result.stderr
    assert "ok" not in result.stdout
    assert len(requests) == 1


@pytest.mark.parametrize(
    "issue",
    [
        {"id": "wrong-issue", "identifier": "TEST-207", "state": {"id": "done"}},
        {"id": "issue-207", "identifier": "TEST-207", "state": {}},
        {
            "id": "issue-207",
            "identifier": "TEST-207",
            "state": {"id": "wrong-state"},
        },
    ],
    ids=["wrong-issue", "missing-state", "wrong-state"],
)
def test_linear_success_true_still_requires_exact_mutation_readback(
    tmp_path: Path, issue: dict[str, Any]
) -> None:
    states = [{"id": "done", "name": "Done", "type": "completed"}]
    mutation = {"success": True, "issue": issue}
    with linear_endpoint(transition_responder(states, mutation)) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "transition", "issue-207", "completed")

    assert result.returncode != 0
    assert "read-back" in result.stderr
    assert "ok" not in result.stdout
    assert len(requests) == 2


def test_linear_transition_reports_ok_only_for_exact_mutation_readback(
    tmp_path: Path,
) -> None:
    states = [{"id": "done", "name": "Done", "type": "completed"}]
    mutation = {
        "success": True,
        "issue": {
            "id": "issue-207",
            "identifier": "TEST-207",
            "state": {"id": "done", "name": "Done", "type": "completed"},
        },
    }
    with linear_endpoint(transition_responder(states, mutation)) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "transition", "issue-207", "completed")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok TEST-207"
    assert len(requests) == 2


def test_linear_comment_returns_only_a_proven_nonblank_comment_id(
    tmp_path: Path,
) -> None:
    def responder(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert "commentCreate" in query
        assert variables == {"id": "issue-207", "b": "Accepted"}
        return {
            "commentCreate": {
                "success": True,
                "comment": {
                    "id": "comment-207",
                    "issue": {"id": "issue-207"},
                },
            }
        }

    with linear_endpoint(responder) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "comment", "issue-207", "Accepted")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "comment-207"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "created",
    [
        {},
        None,
        {"success": True, "comment": None},
        {"success": True, "comment": {}},
        {
            "success": False,
            "comment": {"id": "comment-207", "issue": {"id": "issue-207"}},
        },
        {
            "success": True,
            "comment": {"id": "comment-207", "issue": {"id": "wrong-issue"}},
        },
    ],
    ids=[
        "empty-object",
        "null-envelope",
        "null-comment",
        "missing-id",
        "success-false",
        "wrong-issue",
    ],
)
def test_linear_comment_rejects_unproven_success_envelopes(
    tmp_path: Path, created: object
) -> None:
    def responder(_query: str, _variables: dict[str, Any]) -> dict[str, Any]:
        return {"commentCreate": created}

    with linear_endpoint(responder) as (endpoint, requests):
        provider, env = stage_linear(tmp_path, endpoint)
        result = run_provider(provider, env, "comment", "issue-207", "Accepted")

    assert result.returncode != 0
    assert result.stdout == ""
    assert len(requests) == 1


TRELLO_CURL = r"""#!/usr/bin/env python3
import json
import os
import sys
import urllib.parse

args = sys.argv[1:]
method = args[args.index("-X") + 1]
url = next(arg for arg in args if arg.startswith("http"))
path = urllib.parse.urlparse(url).path
with open(os.environ["TRELLO_TEST_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"method": method, "path": path, "url": url}) + "\n")
if path.endswith("/boards/board-uuid/lists"):
    payload = os.environ["TRELLO_TEST_LISTS"]
elif method == "POST" and path.endswith("/cards/card-207/actions/comments"):
    payload = os.environ["TRELLO_TEST_COMMENT"]
elif method == "PUT" and path.endswith("/cards/card-207"):
    payload = os.environ.get("TRELLO_TEST_PUT", '{"id":"card-207"}')
elif method == "GET" and path.endswith("/cards/card-207"):
    payload = os.environ["TRELLO_TEST_READBACK"]
else:
    raise SystemExit("unexpected Trello request: %s %s" % (method, path))
sys.stdout.write(payload)
"""


def stage_trello(
    tmp_path: Path,
    *,
    lists: list[dict[str, str]],
    readback: dict[str, str] | None = None,
) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "trello"
    role = repo / "agents" / "hermes" / "pm"
    provider = role / ".scripts" / "providers" / "trello.sh"
    provider.parent.mkdir(parents=True)
    shutil.copy2(TRELLO, provider)
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "project_slug": "trello-fixture",
                "ticket_provider": {"type": "trello", "board_id": "board-uuid"},
            }
        ),
        encoding="utf-8",
    )
    (role / "role.yaml").write_text(
        """repo: trello-fixture
ticket_provider:
  name: trello
  completed: Done
""",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "trello-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(TRELLO_CURL, encoding="utf-8")
    fake_curl.chmod(0o755)
    log = tmp_path / "trello-requests.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["TRELLO_KEY"] = "trello-test-key"
    env["TRELLO_TOKEN"] = "trello-test-token"
    env["TRELLO_TEST_LOG"] = str(log)
    env["TRELLO_TEST_LISTS"] = json.dumps(lists)
    env["TRELLO_TEST_READBACK"] = json.dumps(
        readback or {"id": "card-207", "idList": "done-list"}
    )
    env["TRELLO_TEST_COMMENT"] = json.dumps(
        {"id": "action-207", "data": {"card": {"id": "card-207"}}}
    )
    return provider, env, log


def trello_requests(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trello_exact_lane_name_must_be_unique(tmp_path: Path) -> None:
    provider, env, log = stage_trello(
        tmp_path,
        lists=[
            {"id": "done-1", "name": "Done"},
            {"id": "done-2", "name": " done "},
        ],
    )

    result = run_provider(provider, env, "transition", "card-207", "completed")

    assert result.returncode != 0
    assert "resolved 2 lists" in result.stderr
    assert "ok" not in result.stdout
    assert [row["method"] for row in trello_requests(log)] == ["GET"]


@pytest.mark.parametrize(
    "readback",
    [
        {"id": "wrong-card", "idList": "done-list"},
        {"id": "card-207", "idList": "wrong-list"},
    ],
    ids=["wrong-card", "wrong-list"],
)
def test_trello_put_requires_exact_live_readback(
    tmp_path: Path, readback: dict[str, str]
) -> None:
    provider, env, log = stage_trello(
        tmp_path,
        lists=[{"id": "done-list", "name": "Done"}],
        readback=readback,
    )

    result = run_provider(provider, env, "transition", "card-207", "completed")

    assert result.returncode != 0
    assert "read-back" in result.stderr
    assert "ok" not in result.stdout
    assert [row["method"] for row in trello_requests(log)] == ["GET", "PUT", "GET"]


def test_trello_transition_reports_ok_only_after_exact_live_readback(
    tmp_path: Path,
) -> None:
    provider, env, log = stage_trello(
        tmp_path,
        lists=[{"id": "done-list", "name": "Done"}],
    )

    result = run_provider(provider, env, "transition", "card-207", "completed")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok card-207"
    assert [row["method"] for row in trello_requests(log)] == ["GET", "PUT", "GET"]


def test_trello_comment_returns_only_a_proven_nonblank_action_id(
    tmp_path: Path,
) -> None:
    provider, env, log = stage_trello(tmp_path, lists=[])

    result = run_provider(provider, env, "comment", "card-207", "Accepted")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "action-207"
    assert [row["method"] for row in trello_requests(log)] == ["POST"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        [],
        {"id": ""},
        {"id": "action-207", "data": {"card": None}},
        {"id": "action-207", "data": {"card": {"id": "wrong-card"}}},
    ],
    ids=[
        "empty-object",
        "null",
        "list",
        "empty-id",
        "malformed-card",
        "wrong-card",
    ],
)
def test_trello_comment_rejects_unproven_success_envelopes(
    tmp_path: Path, payload: object
) -> None:
    provider, env, log = stage_trello(tmp_path, lists=[])
    env["TRELLO_TEST_COMMENT"] = json.dumps(payload)

    result = run_provider(provider, env, "comment", "card-207", "Accepted")

    assert result.returncode != 0
    assert result.stdout == ""
    assert [row["method"] for row in trello_requests(log)] == ["POST"]
