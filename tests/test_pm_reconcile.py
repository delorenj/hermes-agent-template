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
    for file in ("pm-reconcile.py", "pm-reconcile.prompt.md", "board-lease.py", "launch-pass.py"):
        shutil.copy2(SOURCE / file, scripts / file)
    policy = {"version": 1, "enabled": True, "repo": str(repo), "employee": "demo-pm", "liveness_argv": [str(tmp_path / "probe.py")]}
    (runtime / "pm-reconcile.json").write_text(json.dumps(policy))
    (tmp_path / "ticket.json").write_text(json.dumps({"ok": True, "ticket": {
        "ref": "DEMO-1", "name": "Authorized request", "state_name": "Backlog", "state_group": "backlog", "updated_at": "2026-11-22T10:00:00Z", "priority": "medium", "sort_order": 100, "relations": [], "label_names": [], "comments": []}}))
    (tmp_path / "fixture.json").write_text(json.dumps({"ok": True, "employee": "demo-pm", "enabled": True, "board": "DEMO", "scopes": ["(unlabeled)"]}))
    (tmp_path / "probe.py").write_text("#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps(dict(ref=sys.argv[-1], running=False, queued=False, detached=False) if os.getenv('BUSY') != '1' else dict(ref=sys.argv[-1],running=True,queued=False,detached=False)))\n")
    px = tmp_path / "px"
    px.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['FIXTURE'])
args=sys.argv[1:]
multifile=root/'tickets.json'
if multifile.exists():
 records=json.loads(multifile.read_text())
 ref=args[2] if args[:2]==['task','get'] else args[1] if args[:1] in (['move'],['comment']) else args[2] if args[:2]==['label','add'] else None
 if args[:2]==['task','list']:
  print(json.dumps(dict(ok=True,board='DEMO',issues=[dict(sequence_id=int(k.split('-')[1]),state_group=v['state_group']) for k,v in records.items()])));sys.exit()
 if args[:2]==['task','get']:
  print(json.dumps(dict(ok=True,ticket=records[ref])));sys.exit()
 with (root/'writes.log').open('a') as f: f.write(json.dumps(args)+'\\n')
 t=records[ref]
 if args[:1]==['move']: t['state_name']=args[2]; t['state_group']='unstarted'
 if args[:2]==['label','add']: t['label_names'].append(args[3])
 records[ref]=t
 multifile.write_text(json.dumps(records))
 print('ok');sys.exit()
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
if args[:2]==['label','add']:t['ticket']['label_names'].append(args[3])
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


def plan(rig, op="intake", hold="", evidence="authority: owner approved ordinary source; restriction: no publication or spend; criteria: acceptance verified; dependencies: none; files: independent; launch-cut: no launch declaration"):
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
    assert ticket(rig)["state_name"] == "Todo" and "lifecycle:triaged" in ticket(rig)["label_names"]
    assert writes(rig)[0][:3] == ["move", "DEMO-1", "Todo"]
    assert writes(rig)[1] == ["label", "add", "DEMO-1", "lifecycle:triaged"]
    assert run(rig, "--apply", str(path)).returncode == 2  # durable receipt prevents replay
    assert len(writes(rig)) == 2


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


def test_two_held_replenish_then_real_krebs_dispatch(rig):
    source = ticket(rig)
    source.update(priority="urgent", sort_order=10)
    def make(ref, state, labels, priority="medium", sort=100):
        return {**source, "ref": ref, "state_name": state,
                "state_group": {"In Progress": "started", "Backlog": "backlog", "Todo": "unstarted"}[state],
                "label_names": labels, "priority": priority, "sort_order": sort, "comments": []}
    rows = {
        "DEMO-1": make("DEMO-1", "In Progress", ["lifecycle:needs-input"], "urgent"),
        "DEMO-2": make("DEMO-2", "In Progress", ["lifecycle:blocked"], "high"),
        "DEMO-3": make("DEMO-3", "Backlog", [], "high", 20),
        "DEMO-4": make("DEMO-4", "Backlog", [], "medium", 30),
        "DEMO-5": make("DEMO-5", "Backlog", ["lifecycle:human"], "urgent", 1),
    }
    path = rig[2] / "tickets.json"
    path.write_text(json.dumps(rows))
    brief = run(rig, "--brief")
    assert brief.returncode == 0
    assert brief.stdout.index("DEMO-3 (unlabeled):") < brief.stdout.index("DEMO-4 (unlabeled):")
    assert "DEMO-5|" not in brief.stdout
    evidence = "authority: ordinary source accepted by owner; restriction: no spend or release; criteria: independent checks pass; dependencies: none; files: independent trees; launch-cut: no launch declaration"
    def candidate(ref):
        t = rows[ref]
        expected = hashlib.sha256(json.dumps(t, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"op": "intake", "ref": ref, "scope": "(unlabeled)", "expected": expected, "evidence": evidence, "hold": ""}
    proposal = rig[1] / "replenish.json"
    proposal.write_text(json.dumps({"writes": [candidate("DEMO-3"), candidate("DEMO-4")]}))
    result = run(rig, "--apply", str(proposal))
    assert result.returncode == 0, result.stdout
    after = json.loads(path.read_text())
    assert after["DEMO-1"]["label_names"] == ["lifecycle:needs-input"]
    assert after["DEMO-2"]["label_names"] == ["lifecycle:blocked"]
    assert after["DEMO-5"]["label_names"] == ["lifecycle:human"]
    assert [(after[k]["state_name"], after[k]["label_names"]) for k in ("DEMO-3", "DEMO-4")] == [("Todo", ["lifecycle:triaged"])] * 2
    assert [(w[0], w[1 if w[0] == "move" else 2]) for w in writes(rig)] == [
        ("move", "DEMO-3"), ("label", "DEMO-3"), ("move", "DEMO-4"), ("label", "DEMO-4")]
    runner = SOURCE.parents[1] / "tests" / "pm_replenishment_krebs.mjs"
    krebs = subprocess.run(["node", str(runner), str(path)], capture_output=True, text=True, timeout=30)
    assert krebs.returncode == 0, krebs.stdout + krebs.stderr
    assert "real Krebs runTick dispatched two" in krebs.stdout


def test_launch_cut_and_open_dependency_refuse_without_board_writes(rig):
    t = ticket(rig)
    t["relations"] = [{"type": "blocked_by", "ref": "DEMO-9", "state_name": "Todo", "resolvable": True}]
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    assert "open blocking dependency DEMO-9" in run(rig, "--monitor").stdout
    proposal = plan(rig)
    assert run(rig, "--apply", str(proposal)).returncode == 1
    assert not writes(rig)
    t["relations"][0]["state_name"] = "Done"
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))
    declaration = rig[2] / "repo" / ".launch.yaml"
    declaration.write_text("mode: launch\ncut_line:\n  launch_critical: [DEMO-9]\n  after_launch: [DEMO-1]\n")
    assert "after declared launch cut" in run(rig, "--monitor").stdout
    new_plan = plan(rig)
    second = rig[1] / "launch-cut.json"
    second.write_text(new_plan.read_text())
    assert run(rig, "--apply", str(second)).returncode == 1
    assert not writes(rig)


def test_actual_write_cap_refuses_four_intakes(rig):
    proposal = plan(rig)
    data = json.loads(proposal.read_text())
    data["writes"] = [{**data["writes"][0], "ref": f"DEMO-{i}"} for i in range(1, 5)]
    proposal.write_text(json.dumps(data))
    assert run(rig, "--apply", str(proposal)).returncode == 2
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
