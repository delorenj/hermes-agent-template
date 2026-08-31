from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


PROVIDER = (
    Path(__file__).resolve().parents[1]
    / "template"
    / ".scripts"
    / "providers"
    / "plane.sh"
)


def stage_provider(
    tmp_path: Path,
    *,
    in_review: str = "",
    timezone: str = "",
    responses: list[dict[str, str]],
) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    role = repo / "agents" / "hermes" / "pm"
    provider = role / ".scripts" / "providers" / "plane.sh"
    provider.parent.mkdir(parents=True)
    shutil.copy2(PROVIDER, provider)

    board_id = "board-uuid"
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "ticket_provider": {
                    "type": "plane",
                    "workspace": "test-space",
                    "identifier": "TEST",
                    "board_id": board_id,
                }
            }
        ),
        encoding="utf-8",
    )
    role_yaml = "\n".join(
        [
            "ticket_provider:",
            "  name: plane",
            f'  timezone: "{timezone}"',
            f'  in_review: "{in_review}"',
            '  completed: "Done"',
            '  cancelled: "Cancelled"',
        ]
    )
    (role / "role.yaml").write_text(role_yaml + "\n", encoding="utf-8")

    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses), encoding="utf-8")
    request_log = tmp_path / "requests.jsonl"
    state = tmp_path / "state.json"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
method = "GET"
url = ""
body = ""
outfile = ""
headerfile = ""
writeout = ""
i = 0
while i < len(argv):
    arg = argv[i]
    if arg == "-X":
        method = argv[i + 1]
        i += 2
    elif arg == "-d":
        body = argv[i + 1]
        i += 2
    elif arg == "-H":
        i += 2
    elif arg == "-o":
        outfile = argv[i + 1]
        i += 2
    elif arg == "-D":
        headerfile = argv[i + 1]
        i += 2
    elif arg == "-w":
        writeout = argv[i + 1]
        i += 2
    elif arg.startswith("-"):
        i += 1
    else:
        url = arg
        i += 1

with open(os.environ["PLANE_TEST_REQUEST_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"method": method, "url": url, "body": body}) + "\\n")

responses = sorted(
    json.load(open(os.environ["PLANE_TEST_RESPONSES"], encoding="utf-8")),
    key=lambda item: (len(item["path"]), bool(item.get("query_contains"))),
    reverse=True,
)
chosen = None
for response in responses:
    if response["method"] != method or response["path"] not in url:
        continue
    if response.get("query_contains") and response["query_contains"] not in url:
        continue
    chosen = response
    break
if chosen is None:
    sys.stderr.write(f"no fixture for {method} {url}\\n")
    raise SystemExit(22)

state_path = os.environ.get("PLANE_TEST_STATE", "")
counts = {}
if state_path and os.path.exists(state_path):
    with open(state_path, encoding="utf-8") as stream:
        counts = json.load(stream)
key = f"{method} {chosen['path']}"
count = counts.get(key, 0)
counts[key] = count + 1
if state_path:
    with open(state_path, "w", encoding="utf-8") as stream:
        json.dump(counts, stream)

fail_times = int(chosen.get("fail_times", 0))
if count < fail_times:
    status = int(chosen.get("fail_status", 429))
    headers = chosen.get("fail_headers", {"Retry-After": "0"})
    payload = chosen.get("fail_body", '{"detail":"throttled"}')
else:
    status = int(chosen.get("status", 200))
    headers = chosen.get("headers", {})
    bodies = chosen.get("bodies")
    payload = bodies[min(max(count - fail_times, 0), len(bodies) - 1)] if bodies else chosen["body"]

if headerfile:
    with open(headerfile, "w", encoding="utf-8") as stream:
        stream.write(f"HTTP/1.1 {status} Status\\r\\n")
        for header_name, header_value in headers.items():
            stream.write(f"{header_name}: {header_value}\\r\\n")
if outfile:
    with open(outfile, "w", encoding="utf-8") as stream:
        stream.write(payload)
else:
    sys.stdout.write(payload)
if writeout:
    sys.stdout.write(writeout.replace("%{http_code}", str(status)))
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["PLANE_API_KEY"] = "test-key-never-printed"
    env["PLANE_TEST_RESPONSES"] = str(fixture)
    env["PLANE_TEST_REQUEST_LOG"] = str(request_log)
    env["PLANE_TEST_STATE"] = str(state)
    return provider, env, request_log


def run_provider(provider: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(provider), *args],
        cwd=provider.parents[5],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def response(method: str, path: str, body: object) -> dict[str, str]:
    return {"method": method, "path": path, "body": json.dumps(body)}


def requests(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_active_milestone_does_not_promote_an_expired_cycle(tmp_path: Path) -> None:
    provider, env, _ = stage_provider(
        tmp_path,
        responses=[
            response(
                "GET",
                "/cycles/",
                {
                    "results": [
                        {
                            "id": "expired-cycle",
                            "name": "Sprint 1",
                            "start_date": "2026-08-18T00:05:36-04:00",
                            "end_date": "2026-08-21T23:59:00-04:00",
                        }
                    ]
                },
            )
        ],
    )

    result = run_provider(provider, env, "active_milestone")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"id": "", "name": "", "state": "inactive"}


def test_active_milestone_uses_configured_project_calendar_before_utc_boundary(
    tmp_path: Path,
) -> None:
    provider, env, _ = stage_provider(
        tmp_path,
        timezone="America/New_York",
        responses=[
            response(
                "GET",
                "/cycles/",
                {
                    "results": [
                        {
                            "id": "august-29",
                            "name": "Local August 29",
                            "start_date": "2026-08-29T00:00:00-04:00",
                            "end_date": "2026-08-29T23:59:59-04:00",
                        }
                    ]
                },
            )
        ],
    )
    env["TICKET_PROVIDER_NOW"] = "2026-08-30T03:59:59Z"

    result = run_provider(provider, env, "active_milestone")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "id": "august-29",
        "name": "Local August 29",
        "state": "active",
    }


def test_active_milestone_flips_at_configured_project_midnight(tmp_path: Path) -> None:
    provider, env, _ = stage_provider(
        tmp_path,
        timezone="America/New_York",
        responses=[
            response(
                "GET",
                "/cycles/",
                {
                    "results": [
                        {
                            "id": "august-29",
                            "name": "Local August 29",
                            "start_date": "2026-08-29T00:00:00-04:00",
                            "end_date": "2026-08-29T23:59:59-04:00",
                        }
                    ]
                },
            )
        ],
    )
    env["TICKET_PROVIDER_NOW"] = "2026-08-30T04:00:00Z"

    result = run_provider(provider, env, "active_milestone")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"id": "", "name": "", "state": "inactive"}


def test_list_issues_marks_exact_current_cycle_membership(tmp_path: Path) -> None:
    states = {
        "results": [
            {"id": "todo-state", "name": "Todo", "group": "unstarted"},
            {"id": "doing-state", "name": "In Progress", "group": "started"},
        ]
    }
    issues = {
        "results": [
            {"id": "issue-in", "sequence_id": 7, "name": "Visible", "state": "doing-state"},
            {"id": "issue-out", "sequence_id": 8, "name": "Outside", "state": "todo-state"},
        ]
    }
    cycles = {
        "results": [
            {
                "id": "current-cycle",
                "name": "Current delivery",
                "start_date": "2000-01-01T00:00:00-05:00",
                "end_date": "2999-12-31T23:59:59-05:00",
            },
            {
                "id": "expired-cycle",
                "name": "Old delivery",
                "start_date": "1999-01-01",
                "end_date": "1999-01-31",
            },
        ]
    }
    provider, env, _ = stage_provider(
        tmp_path,
        responses=[
            response("GET", "/states/", states),
            response("GET", "/issues/", issues),
            response("GET", "/cycles/", cycles),
            response("GET", "/cycles/current-cycle/cycle-issues/", {"results": [issues["results"][0]]}),
        ],
    )

    result = run_provider(provider, env, "list_issues")

    assert result.returncode == 0, result.stderr
    listed = {issue["id"]: issue for issue in json.loads(result.stdout)}
    assert listed["issue-in"]["active_milestone_id"] == "current-cycle"
    assert listed["issue-in"]["in_active_milestone"] is True
    assert listed["issue-out"]["active_milestone_id"] == "current-cycle"
    assert listed["issue-out"]["in_active_milestone"] is False


def test_in_review_refuses_to_fall_back_to_first_started_lane(tmp_path: Path) -> None:
    provider, env, request_log = stage_provider(
        tmp_path,
        responses=[
            response(
                "GET",
                "/states/",
                {"results": [{"id": "doing-state", "name": "In Progress", "group": "started"}]},
            )
        ],
    )

    result = run_provider(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "exact Plane state 'In Review'" in result.stderr
    assert "ok" not in result.stdout
    assert [entry["method"] for entry in requests(request_log)] == ["GET"]


def test_transition_patches_exact_state_and_requires_readback(tmp_path: Path) -> None:
    provider, env, request_log = stage_provider(
        tmp_path,
        in_review="Ready for QA",
        responses=[
            response(
                "GET",
                "/states/",
                {
                    "results": [
                        {"id": "doing-state", "name": "In Progress", "group": "started"},
                        {"id": "qa-state", "name": "Ready for QA", "group": "started"},
                    ]
                },
            ),
            response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7}),
            response("GET", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7, "state": "doing-state"}),
        ],
    )
    env["PLANE_MUTATION_MAX_ATTEMPTS"] = "1"

    result = run_provider(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "read-back state" in result.stderr
    assert "ok" not in result.stdout
    sent = requests(request_log)
    assert [entry["method"] for entry in sent] == ["GET", "PATCH", "GET"]
    assert json.loads(sent[1]["body"]) == {"state": "qa-state"}


def test_transition_reports_ok_only_after_exact_readback(tmp_path: Path) -> None:
    provider, env, request_log = stage_provider(
        tmp_path,
        in_review="Ready for QA",
        responses=[
            response(
                "GET",
                "/states/",
                {"results": [{"id": "qa-state", "name": "Ready for QA", "group": "started"}]},
            ),
            response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7}),
            response("GET", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7, "state": "qa-state"}),
        ],
    )

    result = run_provider(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok 7"
    sent = requests(request_log)
    assert [entry["method"] for entry in sent] == ["GET", "PATCH", "GET"]
    assert json.loads(sent[1]["body"]) == {"state": "qa-state"}


def test_list_issues_paginates_every_plane_page(tmp_path: Path) -> None:
    page_two = response(
        "GET",
        "/issues/",
        {
            "results": [
                {"id": "issue-page-two", "sequence_id": 9, "name": "Second page", "state": "todo-state"}
            ],
            "next_page_results": False,
            "next_cursor": "",
        },
    )
    page_two["query_contains"] = "cursor=cursor-2"
    provider, env, request_log = stage_provider(
        tmp_path,
        responses=[
            response(
                "GET",
                "/states/",
                {"results": [{"id": "todo-state", "name": "Todo", "group": "unstarted"}]},
            ),
            response(
                "GET",
                "/issues/",
                {
                    "results": [
                        {"id": "issue-page-one", "sequence_id": 8, "name": "First page", "state": "todo-state"}
                    ],
                    "next_page_results": True,
                    "next_cursor": "cursor-2",
                },
            ),
            page_two,
            response("GET", "/cycles/", {"results": []}),
        ],
    )

    result = run_provider(provider, env, "list_issues")

    assert result.returncode == 0, result.stderr
    listed = {issue["id"] for issue in json.loads(result.stdout)}
    assert listed == {"issue-page-one", "issue-page-two"}
    issue_requests = [entry["url"] for entry in requests(request_log) if "/issues/" in entry["url"]]
    assert any("cursor=cursor-2" in url for url in issue_requests)


def test_reads_retry_429_honoring_retry_after(tmp_path: Path) -> None:
    import time

    throttled = response(
        "GET",
        "/cycles/",
        {
            "results": [
                {
                    "id": "current-cycle",
                    "name": "Current delivery",
                    "start_date": "2000-01-01",
                    "end_date": "2999-12-31",
                }
            ]
        },
    )
    throttled["fail_times"] = 1
    throttled["fail_status"] = 429
    throttled["fail_headers"] = {"Retry-After": "1"}
    provider, env, request_log = stage_provider(tmp_path, responses=[throttled])

    started = time.monotonic()
    result = run_provider(provider, env, "active_milestone")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == "current-cycle"
    assert elapsed >= 0.9, f"Retry-After: 1 was not honored (elapsed {elapsed:.2f}s)"
    assert [entry["method"] for entry in requests(request_log)] == ["GET", "GET"]


def test_reads_fail_explicitly_when_rate_limit_never_clears(tmp_path: Path) -> None:
    throttled = response("GET", "/cycles/", {"results": []})
    throttled["fail_times"] = 99
    throttled["fail_status"] = 429
    throttled["fail_headers"] = {"Retry-After": "0"}
    provider, env, request_log = stage_provider(tmp_path, responses=[throttled])
    env["PLANE_READ_MAX_ATTEMPTS"] = "3"

    result = run_provider(provider, env, "active_milestone")

    assert result.returncode != 0
    assert "HTTP 429" in result.stderr
    assert len(requests(request_log)) == 3


def test_transition_confirms_landed_patch_before_repeating(tmp_path: Path) -> None:
    throttled_patch = response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7})
    throttled_patch["fail_times"] = 1
    throttled_patch["fail_status"] = 429
    throttled_patch["fail_headers"] = {"Retry-After": "0"}
    readback = response("GET", "/issues/issue-uuid/", {})
    readback["bodies"] = [
        json.dumps({"id": "issue-uuid", "sequence_id": 7, "state": "doing-state"}),
        json.dumps({"id": "issue-uuid", "sequence_id": 7, "state": "qa-state"}),
    ]
    provider, env, request_log = stage_provider(
        tmp_path,
        in_review="Ready for QA",
        responses=[
            response(
                "GET",
                "/states/",
                {"results": [{"id": "qa-state", "name": "Ready for QA", "group": "started"}]},
            ),
            throttled_patch,
            readback,
        ],
    )
    env["PLANE_MUTATION_RETRY_DELAY"] = "0"

    result = run_provider(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok 7"
    sent = requests(request_log)
    assert [entry["method"] for entry in sent] == ["GET", "PATCH", "GET", "PATCH", "GET"]
    assert json.loads(sent[3]["body"]) == {"state": "qa-state"}


def test_transition_never_claims_completion_without_matching_readback(tmp_path: Path) -> None:
    provider, env, request_log = stage_provider(
        tmp_path,
        in_review="Ready for QA",
        responses=[
            response(
                "GET",
                "/states/",
                {"results": [{"id": "qa-state", "name": "Ready for QA", "group": "started"}]},
            ),
            response("PATCH", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7}),
            response("GET", "/issues/issue-uuid/", {"id": "issue-uuid", "sequence_id": 7, "state": "doing-state"}),
        ],
    )
    env["PLANE_MUTATION_MAX_ATTEMPTS"] = "2"
    env["PLANE_MUTATION_RETRY_DELAY"] = "0"

    result = run_provider(provider, env, "transition", "issue-uuid", "in_review")

    assert result.returncode != 0
    assert "read-back state" in result.stderr
    assert "refusing to claim" in result.stderr
    assert "ok" not in result.stdout
    sent = requests(request_log)
    assert [entry["method"] for entry in sent] == ["GET", "PATCH", "GET", "PATCH", "GET"]
