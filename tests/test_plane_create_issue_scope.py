"""create_issue stamps the role's scope label on a shared board.

Found 2026-11-06 on the TOLT board: `tp create_issue` filed tickets with no `repo:*`
label, so a ticket a PM filed was unrouted intake that no scope owned. The label is
resolved from role.yaml `board_scope` (else .project.json `scope_label`), must
already exist on the board, and an --if-absent match counts only inside that scope.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "template/.scripts"

LABELS = {"results": [{"id": "forge-label", "name": "repo:forge"}, {"id": "game-label", "name": "repo:game"}]}


def stage(tmp_path, *, board_scope='board_scope: ["repo:forge"]\n', scope_label=None, existing=(), post_labels=None):
    role = tmp_path / "repo/agents/hermes/pm"
    (role / ".scripts/providers").mkdir(parents=True)
    (role / ".scripts/lib").mkdir(parents=True)
    shutil.copy2(SOURCE / "providers/plane.sh", role / ".scripts/providers/plane.sh")
    shutil.copy2(SOURCE / "lib/ticket-provider.sh", role / ".scripts/lib/ticket-provider.sh")
    (role / "role.yaml").write_text("ticket_provider:\n  name: plane\n" + board_scope)
    tp_cfg = {"type": "plane", "board_id": "board-uuid", "workspace": "test-space", "identifier": "TOLT"}
    if scope_label:
        tp_cfg["scope_label"] = scope_label
    (tmp_path / "repo/.project.json").write_text(json.dumps({"ticket_provider": tp_cfg}))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"labels": LABELS, "existing": list(existing), "post_labels": post_labels, "posts": []}))
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
method=args[args.index('-X')+1]
url=next(x for x in args if x.startswith('https://'))
assert '/workspaces/test-space/projects/board-uuid/' in url, url
path=url.split('/projects/board-uuid/')[1].split('?')[0]
st=json.load(open(os.environ['STATE']))
if method=='GET' and path=='labels/': out=st['labels']
elif method=='GET' and path=='issues/': out={'results': st['existing']}
elif method=='POST' and path=='issues/':
    body=json.loads(args[args.index('-d')+1]); st['posts'].append(body)
    labels=body.get('labels', []) if st['post_labels'] is None else st['post_labels']
    out={'id':'new-uuid','sequence_id':99,'name':body['name'],'labels':labels}
    json.dump(st, open(os.environ['STATE'],'w'))
else: raise SystemExit('unmocked '+method+' '+path)
open(args[args.index('-o')+1],'w').write(json.dumps(out))
open(args[args.index('-D')+1],'w').write('HTTP/1.1 200 OK\\r\\n')
print('201' if method=='POST' else '200', end='')
""")
    curl.chmod(0o755)
    env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"],
               PLANE_API_KEY="fixture-not-a-secret", STATE=str(state), PLANE_READ_MAX_ATTEMPTS="1")
    return role / ".scripts/lib/ticket-provider.sh", env, state


def tp(dispatcher, env, *args):
    runner = dispatcher.parents[3] / "run.sh"
    runner.write_text('#!/usr/bin/env bash\nset -euo pipefail\n. "' + str(dispatcher) + '"\ntp "$@"\n')
    return subprocess.run(["bash", str(runner), *args], env=env, text=True, capture_output=True)


def posts(state):
    return json.loads(state.read_text())["posts"]


def test_new_ticket_carries_the_roles_board_scope_label(tmp_path):
    dispatcher, env, state = stage(tmp_path)
    result = tp(dispatcher, env, "create_issue", "Make a trio", "desc")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["created"] is True and out["key"] == "99" and out["scope_label"] == "repo:forge"
    assert posts(state)[0]["labels"] == ["forge-label"]


def test_block_list_board_scope_is_read(tmp_path):
    dispatcher, env, state = stage(tmp_path, board_scope="board_scope:\n  - repo:game\nother: x\n")
    result = tp(dispatcher, env, "create_issue", "Fix viewport")
    assert result.returncode == 0, result.stderr
    assert posts(state)[0]["labels"] == ["game-label"]


def test_project_scope_label_is_the_fallback(tmp_path):
    dispatcher, env, state = stage(tmp_path, board_scope="", scope_label="repo:game")
    result = tp(dispatcher, env, "create_issue", "Intake")
    assert result.returncode == 0, result.stderr
    assert posts(state)[0]["labels"] == ["game-label"]


def test_no_scope_convention_files_unlabeled(tmp_path):
    dispatcher, env, state = stage(tmp_path, board_scope="")
    result = tp(dispatcher, env, "create_issue", "Solo board ticket")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["scope_label"] is None
    assert "labels" not in posts(state)[0]


def test_multi_scope_role_must_choose_and_choice_must_be_declared(tmp_path):
    dispatcher, env, state = stage(tmp_path, board_scope='board_scope: ["repo:forge", "repo:game"]\n')
    refused = tp(dispatcher, env, "create_issue", "Which?")
    assert refused.returncode != 0 and "pass --scope" in refused.stderr
    undeclared = tp(dispatcher, env, "create_issue", "--scope", "repo:tolt", "Which?")
    assert undeclared.returncode != 0 and "not in this role's board_scope" in undeclared.stderr
    assert posts(state) == []
    chosen = tp(dispatcher, env, "create_issue", "--scope", "repo:game", "Which?")
    assert chosen.returncode == 0, chosen.stderr
    assert posts(state)[0]["labels"] == ["game-label"]


def test_missing_board_label_refuses_before_creating(tmp_path):
    dispatcher, env, state = stage(tmp_path, board_scope='board_scope: ["repo:hub"]\n')
    result = tp(dispatcher, env, "create_issue", "Hub thing")
    assert result.returncode != 0 and "does not exist on this board" in result.stderr
    assert posts(state) == []


def test_unapplied_label_is_reported_with_the_created_key(tmp_path):
    dispatcher, env, _state = stage(tmp_path, post_labels=[])
    result = tp(dispatcher, env, "create_issue", "Dropped label")
    assert result.returncode != 0
    assert "created issue 99" in result.stderr and "did not apply scope label" in result.stderr


@pytest.mark.parametrize("labels, reused", [(["forge-label"], True), (["game-label"], False), ([], False)])
def test_if_absent_reuses_only_a_same_scope_title_match(tmp_path, labels, reused):
    existing = [{"id": "old-uuid", "sequence_id": 7, "name": "Make a trio", "labels": labels}]
    dispatcher, env, state = stage(tmp_path, existing=existing)
    result = tp(dispatcher, env, "create_issue", "--if-absent", "make a TRIO")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["created"] is (not reused)
    assert out["key"] == ("7" if reused else "99")
    assert len(posts(state)) == (0 if reused else 1)
