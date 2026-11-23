import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "template" / ".scripts"


@pytest.fixture
def rig(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "agents" / "hermes" / "pm" / ".scripts"
    scripts.mkdir(parents=True)
    runtime = scripts.parent / "runtime"
    runtime.mkdir()
    for file in ("pm-reconcile.py", "pm-reconcile.prompt.md", "board-lease.py"):
        shutil.copy2(SOURCE / file, scripts / file)
    policy = {"version": 1, "enabled": True, "repo": str(repo), "employee": "demo-pm", "liveness_argv": [str(tmp_path / "probe.py")]}
    (runtime / "pm-reconcile.json").write_text(json.dumps(policy))
    (tmp_path / "ticket.json").write_text(json.dumps({"ok": True, "ticket": {
        "ref": "DEMO-1", "name": "Authorized request", "state_name": "Backlog", "updated_at": "2026-11-22T10:00:00Z", "label_names": [], "comments": []}}))
    (tmp_path / "fixture.json").write_text(json.dumps({"ok": True, "employee": "demo-pm", "enabled": True, "board": "DEMO", "scopes": ["(unlabeled)"]}))
    (tmp_path / "probe.py").write_text("#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps(dict(ref=sys.argv[-1], running=False, queued=False, detached=False) if os.getenv('BUSY') != '1' else dict(ref=sys.argv[-1],running=True,queued=False,detached=False)))\n")
    px = tmp_path / "px"
    px.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['FIXTURE'])
args=sys.argv[1:]
if args[:2]==['task','list']:
 t=json.loads((root/'ticket.json').read_text())['ticket']
 print(json.dumps(dict(ok=True,board='DEMO',issues=[dict(sequence_id=1,state_group={'Backlog':'backlog','Todo':'unstarted','In Progress':'started'}[t['state_name']])])));sys.exit()
if args[:2]==['task','get']:
 print((root/'ticket.json').read_text());sys.exit()
if args[:2]==['reconcile','ticket-authority']:
    print(json.dumps(dict(ok=True,employee='demo-pm',scope='(unlabeled)')));sys.exit()
if args[:3]==['reconcile','authority',str(root/'repo')]:
 print((root/'fixture.json').read_text());sys.exit()
with (root/'writes.log').open('a') as f: f.write(json.dumps(args)+'\\n')
t=json.loads((root/'ticket.json').read_text())
if args[:1]==['move']:t['ticket']['state_name']=args[2]
if args[:2]==['label','remove']:t['ticket']['label_names'].remove(args[3])
if args[:1]==['comment']:t['ticket']['comments'].append(dict(text=args[-1]))
t['ticket']['updated_at']='2026-11-22T11:00:00Z'
(root/'ticket.json').write_text(json.dumps(t))
print('ok')
""")
    flume = tmp_path / "flume"
    flume.write_text("#!/usr/bin/env python3\nimport pathlib,json,sys\nif sys.argv[2]=='ticket-authority': print(json.dumps(dict(ok=True,employee='demo-pm',scope='(unlabeled)')))\nelse: print((pathlib.Path(__file__).parent/'fixture.json').read_text())\n")
    for path in (px, flume, tmp_path / "probe.py"):
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", FIXTURE=str(tmp_path), HERMES_BOARD_LEASE_OWNER="hermes:demo-pm:test", HERMES_BOARD_LEASE_PATH=str(runtime / "wip-driver.lock"))
    return scripts, runtime, tmp_path, env


def run(rig, *args, env=None):
    scripts, _, _, base = rig
    return subprocess.run([sys.executable, str(scripts / "pm-reconcile.py"), *args], text=True, capture_output=True, env=env or base)


def plan(rig, op="intake", hold="", evidence="authority: owner approved ordinary source; restriction: no publication or spend"):
    _, runtime, tmp, _ = rig
    t = json.loads((tmp / "ticket.json").read_text())["ticket"]
    expected = hashlib.sha256(json.dumps(t, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = runtime / "plan.json"
    path.write_text(json.dumps({"writes": [{"op": op, "ref": "DEMO-1", "scope": "(unlabeled)", "expected": expected, "evidence": evidence, "hold": hold}]}))
    return path


def ticket(rig):
    return json.loads((rig[2] / "ticket.json").read_text())["ticket"]


def writes(rig):
    p = rig[2] / "writes.log"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_intake_quiet_retry(rig):
    first = run(rig, "--monitor")
    assert first.returncode == 0 and first.stdout == run(rig, "--monitor").stdout
    path = plan(rig)
    applied = run(rig, "--apply", str(path))
    assert applied.returncode == 0, applied.stdout
    assert ticket(rig)["state_name"] == "Todo"
    assert writes(rig)[0][:3] == ["move", "DEMO-1", "Todo"]
    assert run(rig, "--apply", str(path)).returncode == 2  # durable receipt prevents replay
    assert len(writes(rig)) == 1


@pytest.mark.parametrize("cause", ["human", "working", "wrong-owner", "stale", "busy", "unavailable", "missing-authority"])
def test_refusal(rig, cause):
    path = plan(rig)
    t = ticket(rig)
    env = dict(rig[3])
    if cause in ("human", "working"):
        t["label_names"].append("lifecycle:human" if cause == "human" else "agent:working")
    elif cause == "wrong-owner":
        t["label_names"].append("repo:foreign")
    elif cause == "stale":
        t["name"] = "changed"
    elif cause == "busy":
        env["BUSY"] = "1"
    elif cause == "unavailable":
        env["PATH"] = "/usr/bin"  # cannot find flume; stable UNAVAILABLE
    elif cause == "missing-authority":
        (rig[2] / "fixture.json").write_text(json.dumps({"ok": False}))
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    result = run(rig, "--apply", str(path), env=env)
    assert result.returncode != 0, result.stdout
    assert not writes(rig)
    if cause == "unavailable":
        assert run(rig, "--monitor", env=env).stdout == "pm-reconcile: UNAVAILABLE\n"


@pytest.mark.parametrize("op,hold,evidence", [
    ("resolve", "lifecycle:blocked", "recheck: blocker DEMO-2 completed; original dependency verified"),
    ("resolve", "lifecycle:needs-input", "recheck: exact owner answer to original question verified"),
    ("failed", "agent:failed", "diagnosis: failed last turn inspected; recovery: return to triage"),
])
def test_specific_hold(rig, op, hold, evidence):
    t = ticket(rig)
    t["state_name"] = "Todo"
    t["label_names"] = [hold]
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    path = plan(rig, op, hold, evidence)
    result = run(rig, "--apply", str(path))
    assert result.returncode == 0, result.stdout
    assert [w[:2] for w in writes(rig)] == [["comment", "DEMO-1"], ["label", "remove"]]
    assert not ticket(rig)["label_names"]


def test_monitor_ignores_own_comment_echo_but_not_other_changes(rig):
    before = run(rig, "--monitor").stdout
    t = ticket(rig)
    t["comments"].append({"text": "[pm-reconcile] evidence"})
    t["updated_at"] = "2026-11-22T11:00:00Z"
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    assert run(rig, "--monitor").stdout == before
    t["name"] = "changed outside pass"
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    assert run(rig, "--monitor").stdout != before
    assert not writes(rig)


def test_monitor_skips_working_and_review_tickets(rig):
    t = ticket(rig)
    for label in ("agent:working", "lifecycle:review", "lifecycle:human"):
        t["label_names"] = [label]
        (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
        output = run(rig, "--monitor")
        assert output.returncode == 0 and "DEMO-1|" not in output.stdout
    assert not writes(rig)


def test_post_comment_foreign_change_keeps_hold(rig):
    t = ticket(rig)
    t["state_name"] = "Todo"
    t["label_names"] = ["lifecycle:blocked"]
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    path = plan(rig, "resolve", "lifecycle:blocked", "recheck: original dependency done")
    px = rig[2] / "px"
    px.write_text(px.read_text().replace("if args[:1]==['comment']:t['ticket']['comments'].append(dict(text=args[-1]))", "if args[:1]==['comment']:\n t['ticket']['comments'].append(dict(text=args[-1])); t['ticket']['name']='external edit'"))
    result = run(rig, "--apply", str(path))
    assert result.returncode == 1
    assert ticket(rig)["label_names"] == ["lifecycle:blocked"]
    assert [w[0] for w in writes(rig)] == ["comment"]


def test_plan_outside_runtime_refused_without_writes(rig):
    path = plan(rig)
    outside = rig[2] / "outside.json"
    outside.write_text(path.read_text())
    assert run(rig, "--apply", str(outside)).returncode == 2
    assert not writes(rig)


def test_lease_contention_and_partial_failure(rig):
    lock = rig[1] / "wip-driver.lock"
    helper = rig[0] / "board-lease.py"
    subprocess.run([sys.executable, str(helper), "acquire", str(lock), "hermes:other:turn"], check=True, capture_output=True)
    path = plan(rig)
    assert run(rig, "--apply", str(path)).returncode == 2
    assert path.exists() and not writes(rig)
    subprocess.run([sys.executable, str(helper), "release", str(lock), "hermes:other:turn"], check=True, capture_output=True)
    t = ticket(rig)
    t["state_name"] = "Todo"
    t["label_names"] = ["lifecycle:blocked"]
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    path = plan(rig, "resolve", "lifecycle:blocked", "recheck: completed dependency")
    # The fixture's comment causes the evidence to be recorded; if the next read refuses,
    # no hold clearing should occur. Force the probe to become busy after the comment.
    px = rig[2] / "px"
    px.write_text(px.read_text().replace("if args[:1]==['comment']:t['ticket']['comments'].append(dict(text=args[-1]))", "if args[:1]==['comment']:\n t['ticket']['comments'].append(dict(text=args[-1])); (root/'busy').write_text('1')"))
    probe = rig[2] / "probe.py"
    probe.write_text(probe.read_text().replace("os.getenv('BUSY') != '1'", "os.getenv('BUSY') != '1' and not (os.path.dirname(__file__) + '/busy' and __import__('pathlib').Path(__file__).with_name('busy').exists())"))
    result = run(rig, "--apply", str(path))
    assert result.returncode == 1 and writes(rig) == [["comment", "DEMO-1", "-m", "[pm-reconcile] recheck: completed dependency"]]
    assert ticket(rig)["label_names"] == ["lifecycle:blocked"]
    receipt = json.loads((rig[1] / "pm-reconcile-receipts" / "plan.json.receipt.json").read_text())
    assert receipt[0]["status"] == "refused"
