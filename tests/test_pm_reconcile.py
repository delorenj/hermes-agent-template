"""Containment regressions + read-only proposal model checks; NOT apply acceptance."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "template" / ".scripts"
EVIDENCE = "authority: approved ordinary source; restriction: no spend or release; criteria: testable; dependencies: none; files: independent; launch-cut: no declaration"


@pytest.fixture
def rig(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "agents/hermes/pm/.scripts"
    scripts.mkdir(parents=True)
    runtime = scripts.parent / "runtime"
    runtime.mkdir()
    for file in ("pm-reconcile.py", "pm-reconcile.prompt.md", "pm-reconcile-liveness.mjs", "launch-pass.py"):
        shutil.copy2(SOURCE / file, scripts / file)
    policy = {"version": 1, "enabled": True, "repo": str(repo), "employee": "demo-pm", "liveness_argv": [str(tmp_path / "probe.py")]}
    (runtime / "pm-reconcile.json").write_text(json.dumps(policy))
    (tmp_path / "ticket.json").write_text(json.dumps({"ok": True, "ticket": {
        "ref": "DEMO-1", "name": "Authorized request", "state_name": "Backlog", "state_group": "backlog", "updated_at": "2026-11-22T10:00:00Z", "priority": "medium", "sort_order": 100, "relations": [], "label_names": [], "comments": []}}))
    (tmp_path / "fixture.json").write_text(json.dumps({"ok": True, "employee": "demo-pm", "enabled": True, "board": "DEMO", "scopes": ["(unlabeled)"]}))
    provider = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['FIXTURE'])
args=sys.argv[1:]
with (root/'calls.log').open('a') as f: f.write(json.dumps([Path(sys.argv[0]).name,*args])+'\\n')
auth=json.loads((root/'fixture.json').read_text())
if Path(sys.argv[0]).name=='probe.py':
 print(json.dumps(dict(ref=args[-1],running=os.getenv('BUSY')=='1',queued=False,detached=False)));sys.exit()
if args[:2]==['reconcile','ticket-authority']:
 print(json.dumps(dict(ok=True,employee='demo-pm',scope='(unlabeled)')));sys.exit()
if args[:2]==['reconcile','authority']:
 print(json.dumps(auth));sys.exit()
t=json.loads((root/'ticket.json').read_text())['ticket']
records=json.loads((root/'tickets.json').read_text()) if (root/'tickets.json').exists() else {t['ref']:t}
if args[:2]==['task','list']:
 print(json.dumps(dict(ok=True,board=auth['board'],issues=[dict(sequence_id=int(k.split('-')[1]),state_group=v['state_group']) for k,v in records.items()])));sys.exit()
if args[:2]==['task','get']:
 print(json.dumps(dict(ok=True,ticket=records[args[2]])));sys.exit()
with (root/'writes.log').open('a') as f: f.write(json.dumps(args)+'\\n')
raise SystemExit('fixture rejects ALL provider writes')
'''
    for name in ("px", "flume", "probe.py"):
        path = tmp_path / name
        path.write_text(provider)
        path.chmod(0o755)
    # No inherited provider credentials or live fleet/profile selectors.
    env = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "FIXTURE": str(tmp_path),
           "HERMES_BOARD_LEASE_OWNER": "hermes:demo-pm:test", "HERMES_BOARD_LEASE_PATH": str(runtime / "wip-driver.lock")}
    return scripts, runtime, tmp_path, env


def model(rig):
    spec = importlib.util.spec_from_file_location("pm_proposal_model", rig[0] / "pm-reconcile.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(rig, *args, env=None):
    return subprocess.run([sys.executable, str(rig[0] / "pm-reconcile.py"), *args], text=True, capture_output=True, env=env or rig[3], timeout=30)


def ticket(rig):
    return json.loads((rig[2] / "ticket.json").read_text())["ticket"]


def save(rig, t):
    (rig[2] / "ticket.json").write_text(json.dumps({"ok": True, "ticket": t}))


def candidate(t, op="intake", hold="", evidence=EVIDENCE):
    canonical = {k: v for k, v in t.items() if k != "pipeline"}
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"op": op, "ref": t["ref"], "scope": "(unlabeled)", "expected": expected, "evidence": evidence, "hold": hold}


def plan(rig, op="intake", hold="", evidence=EVIDENCE):
    path = rig[1] / "plan.json"
    path.write_text(json.dumps({"writes": [candidate(ticket(rig), op, hold, evidence)]}))
    return path


def no_writes(rig):
    assert not (rig[2] / "writes.log").exists()
    assert not (rig[1] / "pm-reconcile-receipts").exists()
    assert not (rig[1] / "wip-driver.lock").exists()


@pytest.mark.parametrize("op,hold", [("intake", ""), ("resolve", "lifecycle:blocked"), ("failed", "agent:failed")])
def test_public_apply_unconditionally_disabled_before_any_source_or_mutation(rig, op, hold):
    t = ticket(rig)
    if hold:
        t.update(state_name="Todo", state_group="unstarted", label_names=[hold])
        save(rig, t)
    path = plan(rig, op, hold)
    before = (rig[2] / "ticket.json").read_bytes()
    # Real entrypoint, no injected liveness: containment happens BEFORE config reads.
    policy = rig[1] / "pm-reconcile.json"
    cfg = json.loads(policy.read_text())
    cfg.pop("liveness_argv")
    policy.write_text(json.dumps(cfg))
    env = {**rig[3], "PM_RECONCILE_ALLOW_WRITES": "1", "PM_RECONCILE_APPLY": "1"}
    for args in [("--apply", str(path)), ("--apply",), ("--monitor", "--apply", str(path)), ("--evaluate", str(path), "--apply")]:
        result = run(rig, *args, env=env)
        assert result.returncode == 2 and "automatic board writes disabled" in result.stdout
        assert "provider-conditional" in result.stdout
    policy.unlink()
    assert run(rig, "--apply", str(path)).returncode == 2
    assert not (rig[2] / "calls.log").exists()
    assert (rig[2] / "ticket.json").read_bytes() == before
    no_writes(rig)


def test_readonly_intake_proposal_is_not_apply_acceptance(rig):
    first = run(rig, "--monitor")
    assert "DEMO-1|" in first.stdout and first.stdout == run(rig, "--monitor").stdout
    result = run(rig, "--evaluate", str(plan(rig)))
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "proposal_only" and data["automatic_writes"] == "disabled"
    assert data["entries"][0]["estimated_mutations_if_supported"] == 3
    assert ticket(rig)["state_name"] == "Backlog"
    no_writes(rig)


@pytest.mark.parametrize("cause", ["human", "working", "wrong-owner", "stale", "busy", "unavailable", "missing-authority"])
def test_readonly_proposal_refusal(rig, cause):
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
        env["PATH"] = "/usr/bin"
    else:
        (rig[2] / "fixture.json").write_text(json.dumps({"ok": False}))
    save(rig, t)
    assert run(rig, "--evaluate", str(path), env=env).returncode == 2
    no_writes(rig)
    if cause == "unavailable":
        assert run(rig, "--monitor", env=env).stdout == "pm-reconcile: UNAVAILABLE\n"


@pytest.mark.parametrize("op,hold,evidence", [
    ("resolve", "lifecycle:blocked", "recheck: original dependency verified"),
    ("resolve", "lifecycle:needs-input", "recheck: exact answer to original question verified"),
    ("failed", "agent:failed", "diagnosis: failed turn inspected; recovery: triage"),
])
def test_hold_proposal_keeps_original_hold(rig, op, hold, evidence):
    t = ticket(rig)
    t.update(state_name="Todo", state_group="unstarted", label_names=[hold])
    save(rig, t)
    result = run(rig, "--evaluate", str(plan(rig, op, hold, evidence)))
    assert result.returncode == 0, result.stdout
    assert ticket(rig)["label_names"] == [hold]
    no_writes(rig)


def test_durable_digest_and_monitor_ignore_only_derived_pipeline(rig):
    before = run(rig, "--monitor").stdout
    t = ticket(rig)
    expected = model(rig).digest(t)
    path = plan(rig)
    t["pipeline"] = {"eta": "now", "queue_position": 3, "reason": "derived"}
    save(rig, t)
    assert run(rig, "--monitor").stdout == before
    assert model(rig).digest(t) == expected
    assert run(rig, "--evaluate", str(path)).returncode == 0
    t["updated_at"] = "2026-11-22T11:00:00Z"
    save(rig, t)
    assert run(rig, "--monitor").stdout == before
    assert model(rig).digest(t) != expected
    no_writes(rig)


@pytest.mark.parametrize("change", ["prefix-spoof", "name", "relations", "labels", "real-comment"])
def test_monitor_retains_unproven_comment_and_durable_changes(rig, change):
    before = run(rig, "--monitor").stdout
    t = ticket(rig)
    if change in ("prefix-spoof", "real-comment"):
        t["comments"].append({"id": "external", "actor_id": "another-user", "text": "[pm-reconcile] spoof" if change == "prefix-spoof" else "answer"})
    elif change == "relations":
        t["relations"] = [{"type": "relates_to", "ref": "DEMO-9", "state_name": "Todo", "resolvable": True}]
    elif change == "labels":
        t["label_names"] = ["priority:ready"]
    else:
        t["name"] = "outside edit"
    save(rig, t)
    assert run(rig, "--monitor").stdout != before
    no_writes(rig)


@pytest.mark.parametrize("label", ["agent:working", "lifecycle:review", "lifecycle:human"])
def test_monitor_skips_protected_tickets(rig, label):
    t = ticket(rig)
    t["label_names"] = [label]
    save(rig, t)
    output = run(rig, "--monitor")
    assert output.returncode == 0 and "DEMO-1|" not in output.stdout
    no_writes(rig)


def test_digit_leading_33god_board_and_reference(rig):
    auth = rig[2] / "fixture.json"
    auth.write_text(auth.read_text().replace('"DEMO"', '"33GOD"'))
    t = ticket(rig)
    t["ref"] = "33GOD-1"
    save(rig, t)
    assert "33GOD-1|" in run(rig, "--monitor").stdout
    assert run(rig, "--evaluate", str(plan(rig))).returncode == 0
    no_writes(rig)


def test_launch_cut_and_dependency_are_readonly_exclusions(rig):
    t = ticket(rig)
    t["relations"] = [{"type": "blocked_by", "ref": "DEMO-9", "state_name": "Todo", "resolvable": True}]
    save(rig, t)
    assert "open blocking dependency DEMO-9" in run(rig, "--monitor").stdout
    assert run(rig, "--evaluate", str(plan(rig))).returncode == 2
    t["relations"][0]["state_name"] = "Done"
    save(rig, t)
    (rig[2] / "repo/.launch.yaml").write_text("mode: launch\ncut_line:\n  launch_critical: [DEMO-9]\n  after_launch: [DEMO-1]\n")
    assert "after declared launch cut" in run(rig, "--monitor").stdout
    assert run(rig, "--evaluate", str(plan(rig))).returncode == 2
    no_writes(rig)


@pytest.mark.parametrize("ops", [["intake"] * 3, ["intake", "resolve", "failed"]])
def test_proposal_cap_counts_actual_provider_mutations_before_reads(rig, ops):
    proposal = {"writes": [{**candidate(ticket(rig)), "op": op, "ref": f"DEMO-{i}"} for i, op in enumerate(ops, 1)]}
    with pytest.raises(ValueError, match="six provider mutations"):
        model(rig).evaluate_plan({}, proposal)
    assert not (rig[2] / "calls.log").exists()
    no_writes(rig)


def test_unknown_operation_and_local_no_write_note(rig):
    path = plan(rig, "reject")
    assert "unsupported operation" in run(rig, "--evaluate", str(path)).stdout
    path.write_text(json.dumps({"writes": [], "note": "DEMO-1: pending original owner answer"}))
    result = run(rig, "--evaluate", str(path))
    assert result.returncode == 0 and "pending original owner answer" in result.stdout
    assert not (rig[2] / "calls.log").exists()
    no_writes(rig)


def test_outside_plan_and_symlink_refused_readonly(rig):
    path = plan(rig)
    outside = rig[2] / "outside.json"
    outside.write_text(path.read_text())
    assert run(rig, "--evaluate", str(outside)).returncode == 2
    link = rig[1] / "link.json"
    link.symlink_to(outside)
    assert run(rig, "--evaluate", str(link)).returncode == 2
    no_writes(rig)


def test_priority_proposals_then_isolated_collaborator_model_not_apply(rig):
    source = ticket(rig)
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
    assert brief.stdout.index("DEMO-3 (unlabeled):") < brief.stdout.index("DEMO-4 (unlabeled):")
    assert "DEMO-5|" not in brief.stdout
    proposal = rig[1] / "replenish.json"
    proposal.write_text(json.dumps({"writes": [candidate(rows[ref]) for ref in ("DEMO-3", "DEMO-4")]}))
    result = run(rig, "--evaluate", str(proposal))
    assert result.returncode == 0, result.stdout
    assert sum(e["estimated_mutations_if_supported"] for e in json.loads(result.stdout)["entries"]) == 6
    assert json.loads(path.read_text()) == rows
    assert run(rig, "--apply", str(proposal)).returncode == 2
    no_writes(rig)
    # Explicitly model accepted Todo for the isolated collaborator contract only.
    # These are NOT PM-applier output and do not prove production handoff.
    for ref in ("DEMO-3", "DEMO-4"):
        rows[ref].update(state_name="Todo", state_group="unstarted", label_names=["lifecycle:triaged"])
    modeled = rig[2] / "modeled-todo.json"
    modeled.write_text(json.dumps(rows))
    runner = SOURCE.parents[1] / "tests/pm_replenishment_krebs.mjs"
    collaborator_env = {**rig[3], "PM_RECONCILE_KREBS_ROOT": os.environ.get("PM_RECONCILE_KREBS_ROOT", str(Path.home() / "code/33GOD/krebs"))}
    krebs = subprocess.run(["node", str(runner), str(modeled)], env=collaborator_env, capture_output=True, text=True, timeout=30)
    assert krebs.returncode == 0, krebs.stdout + krebs.stderr
    assert "isolated collaborator model" in krebs.stdout
