"""Read-only Plane scope hydration through the ticket-provider dispatcher.

Found 2026-11-06 on the TOLT board: list_issues/get_issue returned no labels and no
relations, so the forge PM could not see its own `repo:forge` scope or that TOLT-84
blocks TOLT-81, and dispatched a TOLT-81 implementer over the blocker. Labels resolve
only through the bound project's catalog (an unknown or mismatched id never claims a
scope); relations come from the work-item relations endpoint, hydrated to key/state.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "template/.scripts"


def stage(tmp_path, *, unknown=False, issue_labels=None, relations=True):
    role = tmp_path / "repo/agents/hermes/pm"
    provider = role / ".scripts/providers/plane.sh"
    dispatcher = role / ".scripts/lib/ticket-provider.sh"
    provider.parent.mkdir(parents=True)
    dispatcher.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / "providers/plane.sh", provider)
    shutil.copy2(SOURCE / "lib/ticket-provider.sh", dispatcher)
    (role / "role.yaml").write_text("ticket_provider:\n  name: plane\n")
    (tmp_path / "repo/.project.json").write_text(json.dumps({"ticket_provider": {
        "type": "plane", "board_id": "board-uuid", "workspace": "test-space", "identifier": "TOLT"}}))
    forge_labels = issue_labels if issue_labels is not None else ["missing" if unknown else "forge-label"]
    fixtures = {
        "/states/": {"results": [{"id": "todo", "name": "Todo", "group": "unstarted"}]},
        "/labels/": {"results": [{"id": "forge-label", "name": "repo:forge"}, {"id": "game-label", "name": "repo:game"}]},
        "/cycles/": {"results": []},
        "/issues/": {"results": [
            {"id": "forge", "sequence_id": 81, "name": "Forge", "state": "todo", "labels": forge_labels, "parent": "parent-uuid", "blocked_by": ["other-uuid"]},
            {"id": "game", "sequence_id": 82, "name": "Game", "state": "todo", "labels": ["game-label"]},
        ]},
        "/issues/forge/": {"id": "forge", "sequence_id": 81, "name": "Forge", "state": "todo", "labels": forge_labels, "parent": "parent-uuid", "blocked_by": ["other-uuid"]},
        "/issues/forge/comments/": {"results": []},
        "/issues/forge/issue-attachments/": {"results": []},
        "/issues/blocker/": {"id": "blocker", "sequence_id": 84, "name": "Prove pairs first", "state": "todo"},
        "/issues/game/": {"id": "game", "sequence_id": 82, "name": "Game", "state": "todo"},
    }
    if relations:
        fixtures["/work-items/forge/relations/"] = {
            "blocked_by": [{"issue_id": "blocker", "project_id": "board-uuid"}],
            "blocking": [{"issue_id": "game", "project_id": "board-uuid"}],
            "relates_to": [{"issue_id": "gone", "project_id": "board-uuid"}],
            "duplicate": [],
        }
    fixture = tmp_path / "fixtures.json"
    fixture.write_text(json.dumps(fixtures))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
assert '-X' in args and args[args.index('-X')+1]=='GET', args
url=next(x for x in args if x.startswith('https://'))
assert '/workspaces/test-space/projects/board-uuid/' in url, url
rows=json.load(open(os.environ['FIXTURES']))
path=url.split('/projects/board-uuid/')[1]
match=next((v for k,v in rows.items() if path.endswith(k.lstrip('/'))), None)
if match is None: raise SystemExit('unmocked path: '+path)
open(args[args.index('-o')+1], 'w').write(json.dumps(match))
open(args[args.index('-D')+1], 'w').write('HTTP/1.1 200 OK\\r\\n')
print('200', end='')
""")
    curl.chmod(0o755)
    env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"],
               PLANE_API_KEY="fixture-not-a-secret", FIXTURES=str(fixture), PLANE_READ_MAX_ATTEMPTS="1")
    return dispatcher, env


def tp(dispatcher, env, op, *args):
    # Stage a real dispatcher and invoke it via a shell file rather than a direct provider call.
    runner = dispatcher.parents[3] / "run.sh"
    runner.write_text('#!/usr/bin/env bash\nset -euo pipefail\n. "' + str(dispatcher) + '"\ntp "$@"\n')
    return subprocess.run(["bash", str(runner), op, *args], env=env, text=True, capture_output=True)


def test_list_labels_is_project_bound_and_normalized(tmp_path):
    dispatcher, env = stage(tmp_path)
    result = tp(dispatcher, env, "list_labels")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"id": "forge-label", "name": "repo:forge"},
        {"id": "game-label", "name": "repo:game"},
    ]


def test_scoped_list_preserves_names_parent_and_existing_dependencies(tmp_path):
    dispatcher, env = stage(tmp_path)
    result = tp(dispatcher, env, "list_issues")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [x["key"] for x in rows if "repo:forge" in x["labels"]] == [81]
    assert rows[0]["parent"] == "parent-uuid"
    assert rows[0]["blocked_by"] == ["other-uuid"]
    assert rows[1]["labels"] == ["repo:game"]
    assert "blocked_by" not in rows[1]


def test_scoped_get_issue_preserves_names_and_relations(tmp_path):
    dispatcher, env = stage(tmp_path)
    result = tp(dispatcher, env, "get_issue", "TOLT-81")
    assert result.returncode == 0, result.stderr
    issue = json.loads(result.stdout)
    assert issue["labels"] == ["repo:forge"]
    assert issue["parent"] == "parent-uuid"
    assert issue["blocked_by"] == ["other-uuid"]


@pytest.mark.parametrize("op,args", [("list_issues", ()), ("get_issue", ("TOLT-81",))])
def test_unknown_label_refuses_to_claim_scope(tmp_path, op, args):
    dispatcher, env = stage(tmp_path, unknown=True)
    result = tp(dispatcher, env, op, *args)
    assert result.returncode != 0
    assert "could not be resolved" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("op,args", [("list_issues", ()), ("get_issue", ("TOLT-81",))])
@pytest.mark.parametrize("issue_labels", [
    [{"id": "missing-uuid", "name": "repo:forge"}],
    [{"id": "game-label", "name": "repo:forge"}],
    [{"name": "repo:unknown"}],
])
def test_expanded_unbound_or_mismatched_label_refuses_scope(tmp_path, op, args, issue_labels):
    dispatcher, env = stage(tmp_path, issue_labels=issue_labels)
    result = tp(dispatcher, env, op, *args)
    assert result.returncode != 0
    assert "could not be resolved" in result.stderr or "disagreed" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("op,args", [("list_issues", ()), ("get_issue", ("TOLT-81",))])
@pytest.mark.parametrize("issue_labels, expected", [
    ([{"id": "forge-label", "name": "repo:forge"}], "repo:forge"),
    ([{"name": "repo:forge"}], "repo:forge"),
    ([{"name": "repo:game"}], "repo:game"),
])
def test_expanded_and_idless_catalog_labels_only(tmp_path, op, args, issue_labels, expected):
    dispatcher, env = stage(tmp_path, issue_labels=issue_labels)
    result = tp(dispatcher, env, op, *args)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    issue = rows[0] if op == "list_issues" else rows
    assert issue["labels"] == [expected]
    if expected == "repo:game":
        assert "repo:forge" not in issue["labels"]



def test_get_issue_reads_relations_endpoint_and_hydrates_each_side(tmp_path):
    dispatcher, env = stage(tmp_path)
    result = tp(dispatcher, env, "get_issue", "TOLT-81")
    assert result.returncode == 0, result.stderr
    rel = json.loads(result.stdout)["relations"]
    assert rel["blocked_by"] == [{"id": "blocker", "key": 84, "title": "Prove pairs first",
                                  "state": "Todo", "state_type": "unstarted", "project_id": "board-uuid"}]
    assert [x["key"] for x in rel["blocking"]] == [82]
    # An empty relation type is omitted; an unreadable related item is flagged, not dropped.
    assert "duplicate" not in rel
    assert rel["relates_to"][0]["id"] == "gone" and rel["relates_to"][0]["unreadable"] is True
    assert "relations_error" not in json.loads(result.stdout)


def test_get_issue_reports_an_unreadable_relations_endpoint_instead_of_none(tmp_path):
    dispatcher, env = stage(tmp_path, relations=False)
    result = tp(dispatcher, env, "get_issue", "TOLT-81")
    assert result.returncode == 0, result.stderr
    issue = json.loads(result.stdout)
    assert issue["relations"] == {}
    assert issue["relations_error"] == "relations endpoint could not be read"
