from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest


DISPATCHER = (
    Path(__file__).resolve().parents[1]
    / "template"
    / ".scripts"
    / "lib"
    / "ticket-provider.sh"
)
LINEAR_PROVIDER = (
    Path(__file__).resolve().parents[1]
    / "template"
    / ".scripts"
    / "providers"
    / "linear.sh"
)


def stage_dispatcher(tmp_path: Path, provider_name: str) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / provider_name
    scripts = repo / "agents" / "hermes" / "pm" / ".scripts"
    dispatcher = scripts / "lib" / "ticket-provider.sh"
    dispatcher.parent.mkdir(parents=True)
    shutil.copy2(DISPATCHER, dispatcher)
    providers = scripts / "providers"
    providers.mkdir()
    provider = providers / f"{provider_name}.sh"
    provider.write_text(
        """#!/usr/bin/env sh
set -eu
op="$1"
shift
printf '%s\n' "$op|$*" >> "$TP_TEST_LOG"
case "$op" in
  list_issues) printf '%s\\n' "$TP_TEST_ISSUES" ;;
  transition|comment|get_issue)
    printf 'ok\\n'
    ;;
  *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    provider.chmod(0o755)
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "ticket_provider": {
                    "type": provider_name,
                    "identifier": "JIMB",
                }
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / f"{provider_name}.log"
    env = os.environ.copy()
    env["TP_TEST_LOG"] = str(log)
    return dispatcher, env, log


def run_tp(dispatcher: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; shift; tp "$@"',
            "_",
            str(dispatcher),
            *args,
        ],
        cwd=dispatcher.parents[5],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("provider_name", "human_key", "listed_key"),
    [
        ("plane", "JIMB-207", 207),
        ("linear", "JIMB-207", "JIMB-207"),
        ("trello", "abc123", "abc123"),
    ],
)
def test_mutations_resolve_human_key_to_provider_native_id(
    tmp_path: Path,
    provider_name: str,
    human_key: str,
    listed_key: str | int,
) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, provider_name)
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": listed_key, "title": "Ticket"}]
    )

    transition = run_tp(dispatcher, env, "transition", human_key, "completed")
    comment = run_tp(dispatcher, env, "comment", human_key, "reviewed")

    assert transition.returncode == 0, transition.stderr
    assert comment.returncode == 0, comment.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "list_issues|",
        "transition|provider-native-id completed",
        "list_issues|",
        "comment|provider-native-id reviewed",
    ]


def test_unresolved_human_key_fails_before_mutation(tmp_path: Path) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, "plane")
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": 208, "title": "Different ticket"}]
    )

    result = run_tp(dispatcher, env, "transition", "JIMB-207", "completed")

    assert result.returncode != 0
    assert "could not resolve issue reference" in result.stderr
    assert "ok" not in result.stdout
    assert log.read_text(encoding="utf-8").splitlines() == ["list_issues|"]


@pytest.mark.parametrize("reference", ["", " \t "])
@pytest.mark.parametrize(
    ("operation", "trailing_args"),
    [
        ("get_issue", []),
        ("comment", ["reviewed"]),
        ("transition", ["completed"]),
    ],
)
def test_blank_issue_reference_fails_before_any_provider_call(
    tmp_path: Path,
    reference: str,
    operation: str,
    trailing_args: list[str],
) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, "plane")
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": "", "title": "Missing normalized key"}]
    )

    result = run_tp(dispatcher, env, operation, reference, *trailing_args)

    assert result.returncode == 2
    assert "non-blank issue reference" in result.stderr
    assert "ok" not in result.stdout
    assert not log.exists()


def test_linear_native_id_resolves_beyond_first_issue_page(tmp_path: Path) -> None:
    requests: list[dict[str, object]] = []
    target_id = "11111111-2222-4333-8444-555555555555"

    class LinearHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            query = payload["query"]
            variables = payload.get("variables") or {}

            if "issues(first:100" in query and variables.get("after") is None:
                nodes = [
                    {
                        "id": f"page-one-{index}",
                        "identifier": f"TEST-{index}",
                        "title": f"Page one {index}",
                        "updatedAt": "2026-08-30T00:00:00Z",
                        "url": f"https://linear.app/issue/TEST-{index}",
                        "state": {"name": "Todo", "type": "unstarted"},
                        "assignee": None,
                    }
                    for index in range(1, 101)
                ]
                data = {
                    "issues": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-100"},
                    }
                }
            elif "issues(first:100" in query and variables.get("after") == "cursor-100":
                data = {
                    "issues": {
                        "nodes": [
                            {
                                "id": target_id,
                                "identifier": "TEST-101",
                                "title": "Page two target",
                                "updatedAt": "2026-08-30T00:00:00Z",
                                "url": "https://linear.app/issue/TEST-101",
                                "state": {"name": "In Progress", "type": "started"},
                                "assignee": {"name": "Agent"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            elif "issue(id:$id)" in query and variables.get("id") == target_id:
                data = {
                    "issue": {
                        "id": target_id,
                        "identifier": "TEST-101",
                        "title": "Page two target",
                        "description": "Found after page one.",
                        "state": {"name": "In Progress", "type": "started"},
                        "comments": {"nodes": []},
                    }
                }
            else:
                self.send_error(400, "unexpected Linear fixture request")
                return

            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), LinearHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo = tmp_path / "linear-real"
        scripts = repo / "agents" / "hermes" / "pm" / ".scripts"
        dispatcher = scripts / "lib" / "ticket-provider.sh"
        provider = scripts / "providers" / "linear.sh"
        dispatcher.parent.mkdir(parents=True)
        provider.parent.mkdir(parents=True)
        shutil.copy2(DISPATCHER, dispatcher)
        shutil.copy2(LINEAR_PROVIDER, provider)
        (repo / ".project.json").write_text(
            json.dumps(
                {
                    "ticket_provider": {
                        "type": "linear",
                        "identifier": "TEST",
                        "team": "TEST",
                    }
                }
            ),
            encoding="utf-8",
        )
        (scripts.parent / "role.yaml").write_text(
            "ticket_provider:\n  name: linear\n  team: TEST\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["LINEAR_API_KEY"] = "test-key-never-printed"
        env["LINEAR_GRAPHQL_URL"] = f"http://127.0.0.1:{server.server_port}/graphql"

        result = run_tp(dispatcher, env, "get_issue", target_id)

        assert result.returncode == 0, result.stderr
        issue = json.loads(result.stdout)
        assert issue["id"] == target_id
        assert issue["key"] == "TEST-101"
        assert [request.get("variables", {}).get("after") for request in requests[:2]] == [
            None,
            "cursor-100",
        ]
        assert requests[2].get("variables", {}).get("id") == target_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
