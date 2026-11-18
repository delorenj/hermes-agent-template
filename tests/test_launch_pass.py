"""launch-pass.py: the repo-agnostic launch-mode driver every PM role ships.

px is mocked with a PATH shim: a tiny python script that serves `task list` / `task get` from a JSON
board file and appends every write verb to a log, so the tests prove what the driver sends to px
without a Plane board.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "template" / ".scripts"
DRIVER = SCRIPTS / "launch-pass.py"

DECL = """\
mode: launch
declared: 2026-11-17
source: "docs/launch.md, owner decision 2026-11-17"
finish_line: "Earshot is in the Play Store and a stranger can pay for it."   # comment
target_date: 2026-12-01
contract: [SPEC.md, docs/launch.md]
epic: EARS-1
cut_line:
  launch_critical: [EARS-2, EARS-3]
  after_launch:
    - EARS-4
"""

SHIM = r'''#!__PY__
import json, os, sys
state = os.environ["PX_SHIM_STATE"]
args = sys.argv[1:]
with open(state + "/board.json") as f:
    board = json.load(f)
if args[:2] == ["task", "list"]:
    issues = [{"sequence_id": t["seq"], "state_name": t["state_name"], "state_group": t["state_group"], "name": t["name"]}
              for t in board["tickets"]]
    print(json.dumps({"ok": True, "board": board["board"], "issues": issues}))
elif args[:2] == ["task", "get"]:
    ref = args[2]
    for t in board["tickets"]:
        if f"{board['board']}-{t['seq']}" == ref:
            print(json.dumps({"ticket": t}))
            break
    else:
        print(json.dumps({"ok": False}))
        sys.exit(1)
else:
    with open(state + "/writes.log", "a") as f:
        f.write(json.dumps(args) + "\n")
    if os.environ.get("PX_SHIM_FAIL") == args[0]:
        print("px: boom", file=sys.stderr)
        sys.exit(1)
    print("ok " + " ".join(args[:3]))
'''


def ticket(seq, lane="Backlog", group=None, pri="none", labels=(), comments=(), external=None, updated="2026-11-17T12:00:00+00:00"):
    group = group or {"Backlog": "backlog", "Todo": "unstarted", "In Progress": "started", "Done": "completed", "Cancelled": "cancelled"}[lane]
    return {
        "seq": seq, "name": f"ticket {seq}", "state_name": lane, "state_group": group, "priority": pri,
        "label_names": list(labels), "comments": list(comments), "external_id": external,
        "created_at": "2026-11-10T12:00:00+00:00", "updated_at": updated,
    }


def comment(text, at):
    return {"id": at, "created_at": at, "text": text, "comment_html": f"<p>{text}</p>"}


class Repo:
    """A throwaway repo with one role dir that carries a copy of the shipped scripts."""

    def __init__(self, tmp_path, monkeypatch, declaration=DECL, tickets=None, project=None):
        self.root = tmp_path / "repo"
        self.role = self.root / "agents" / "hermes" / "pm"
        scripts = self.role / ".scripts"
        scripts.mkdir(parents=True)
        for name in ("launch-pass.py", "board-lease.py", "launch-pass.prompt.md"):
            (scripts / name).write_text((SCRIPTS / name).read_text())
        (self.role / "role.yaml").write_text('agent_id: "demo-pm"\n')
        (self.role / "SOUL.md").write_text("# soul\n")
        (self.root / ".project.json").write_text(json.dumps(project or {"ticket_provider": {"identifier": "EARS"}}))
        if declaration is not None:
            (self.root / ".launch.yaml").write_text(declaration)
        self.state = tmp_path / "px-state"
        self.state.mkdir()
        self.set_board(tickets if tickets is not None else [])
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        px = shim_dir / "px"
        px.write_text(SHIM.replace("__PY__", sys.executable))
        px.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            "PX_SHIM_STATE": str(self.state),
            "LAUNCH_PASS_DATE": "2026-11-17",
            "LAUNCH_LEASE_WAIT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env.pop("HERMES_BOARD_LEASE_OWNER", None)
        self.env.pop("LAUNCH_REPO", None)
        self.env.pop("LAUNCH_PASS_MAX", None)

    def set_board(self, tickets):
        (self.state / "board.json").write_text(json.dumps({"board": "EARS", "tickets": tickets}))

    def run(self, *args, **env):
        return subprocess.run(
            [sys.executable, str(self.role / ".scripts" / "launch-pass.py"), *args],
            capture_output=True, text=True, env={**self.env, **env}, cwd=str(self.root), timeout=60,
        )

    def plan(self, plan):
        path = self.role / "runtime" / "launch-pass.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan))
        return path

    def writes(self):
        log = self.state / "writes.log"
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch, tickets=[
        ticket(2, "Todo", pri="high", labels=["lifecycle:triaged"]),
        ticket(3, "Backlog", pri="medium"),
        ticket(4, "Todo", pri="low"),
        ticket(5, "Backlog", pri="urgent", labels=["bug"]),
        ticket(6, "In Progress", labels=["agent:working"]),
    ])


# ---------------------------------------------------------------------------------- monitor

def test_monitor_without_a_declaration_is_exactly_steady(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=None)
    out = r.run("--monitor")
    assert out.returncode == 0
    assert out.stdout == "launch-mode: steady\n"
    assert r.writes() == []


@pytest.mark.parametrize("declaration", ["mode: steady\ndeclared: 2026-11-17\n", "mode: launch\n  broken: [\n", "::: not yaml", "finish_line: x\n"])
def test_monitor_steady_or_unreadable_declaration_is_exactly_steady(tmp_path, monkeypatch, declaration):
    r = Repo(tmp_path, monkeypatch, declaration=declaration)
    assert r.run("--monitor").stdout == "launch-mode: steady\n"


def test_monitor_steady_never_calls_px(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=None)
    (r.state / "board.json").unlink()  # a px call would crash the shim
    out = r.run("--monitor")
    assert out.stdout == "launch-mode: steady\n" and out.stderr == ""


def test_monitor_is_deterministic_and_sorted(repo):
    first, second = repo.run("--monitor").stdout, repo.run("--monitor").stdout
    assert first == second
    lines = first.splitlines()
    assert lines[0] == "launch-mode: launch"
    assert lines[1] == "date 2026-11-17"
    assert lines[3:5] == ["epic EARS-1", "target 2026-12-01 (14d)"]
    refs = [l.split("|")[0] for l in lines if l.startswith("EARS-")]
    assert refs == sorted(refs, key=lambda r: int(r.split("-")[1])) == ["EARS-2", "EARS-3", "EARS-4", "EARS-5", "EARS-6"]
    assert "EARS-2|Todo|high|-|-|owner|cut:lc|-|owner-waiting:0|-" in lines
    assert "EARS-4|Todo|low|-|-|owner|cut:al|-|owner-waiting:0|-" in lines
    assert "EARS-5|Backlog|urgent|bug|-|owner|cut:-|launch-blocker|owner-waiting:0|-" in lines
    assert "off-cut-todo|EARS-4" in lines
    assert "cut-in-backlog|EARS-3" in lines


def test_monitor_changes_when_the_board_or_declaration_changes(repo):
    base = repo.run("--monitor").stdout
    repo.set_board([ticket(2, "Backlog", pri="high"), ticket(3, "Backlog")])
    assert repo.run("--monitor").stdout != base
    repo.set_board([ticket(2, "Todo", pri="high", labels=["lifecycle:triaged"])])
    before = repo.run("--monitor").stdout
    (repo.root / ".launch.yaml").write_text(DECL.replace("2026-12-01", "2026-12-08"))
    assert repo.run("--monitor").stdout != before


def test_monitor_flags_owner_comments_waiting_and_stale_claims(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, tickets=[
        ticket(2, "Todo", comments=[comment("[pm] launch rank 1: x", "2026-11-16T10:00:00+00:00"), comment("please look", "2026-11-16T11:00:00+00:00")]),
        ticket(3, "In Progress", labels=["agent:working"], updated="2026-11-10T10:00:00+00:00"),
        ticket(4, "Todo", comments=[comment("[em] handled", "2026-11-16T11:00:00+00:00")]),
    ])
    lines = r.run("--monitor").stdout.splitlines()
    assert any(l.startswith("EARS-2|") and "owner-waiting:1" in l for l in lines)
    assert any(l.startswith("EARS-3|") and l.endswith("stale-claim") for l in lines)
    assert any(l.startswith("EARS-4|") and "owner-waiting:0" in l for l in lines)


def test_monitor_board_unavailable_is_one_fixed_line(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch)
    (r.state / "board.json").unlink()
    lines = r.run("--monitor").stdout.splitlines()
    assert lines[0] == "launch-mode: launch" and lines[-1] == "board UNAVAILABLE"


# ---------------------------------------------------------------------------------- brief

def test_brief_steady_has_nothing_to_do(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=None)
    out = r.run("--brief")
    assert out.returncode == 0 and out.stdout.startswith("launch-mode: steady\n") and "quiet: steady" in out.stdout


def test_brief_carries_declaration_snapshot_roadmap_and_schema(repo):
    out = repo.run("--brief").stdout
    for needle in ("# Launch pass: the pass contract", "finish_line:", "## Launch mode".replace("## ", ""), "# Snapshot",
                   "EARS-2|Todo|high", "NO ROADMAP YET", "launch-pass.json", '"op": "rank"', "--apply"):
        assert needle in out, needle
    (repo.role / "runtime").mkdir(exist_ok=True)
    (repo.role / "runtime" / "launch-roadmap.md").write_text("# roadmap\n- ship it\n")
    assert "- ship it" in repo.run("--brief").stdout


# ---------------------------------------------------------------------------------- apply

def test_apply_steady_refuses_and_writes_nothing(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=None)
    r.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}]})
    out = r.run("--apply")
    assert out.returncode == 0 and "apply refused" in out.stdout
    assert r.writes() == []


def test_apply_refuses_disallowed_ops_and_bad_refs(repo):
    repo.plan({"writes": [
        {"op": "delete", "ref": "EARS-2"},
        {"op": "release", "ref": "EARS-2", "text": "x"},
        {"op": "claim", "ref": "EARS-2"},
        {"op": "comment", "ref": "../etc", "text": "x"},
        {"op": "comment", "ref": "OTHER-9", "text": "x"},
        {"op": "move", "ref": "EARS-3", "lane": "In Progress", "text": "x"},
        {"op": "move", "ref": "EARS-3", "lane": "Done", "text": "x"},
    ]})
    out = repo.run("--apply")
    assert out.returncode == 0
    assert repo.writes() == []
    assert out.stdout.count(": skip:") == 7
    assert "op not allowed" in out.stdout and "not on this board" in out.stdout


def test_apply_runs_every_allowed_op_through_px_with_tag(repo):
    repo.plan({"writes": [
        {"op": "comment", "ref": "EARS-2", "text": "launch-critical, keep on top"},
        {"op": "move", "ref": "EARS-3", "lane": "Todo", "text": "cut line item"},
        {"op": "close", "ref": "EARS-4", "text": "accepted: shipped in build 1.2.3"},
        {"op": "cancel", "ref": "EARS-5", "text": "not launch work"},
        {"op": "rank", "ref": "EARS-2", "rank": 1, "text": "gates the Play listing"},
        {"op": "link", "ref": "EARS-3", "other": "EARS-2", "blocks": True},
        {"op": "create", "title": "Write store listing", "description": "d", "labels": ["docs", "lifecycle:triaged"], "priority": "high", "state": "Todo", "key": "launch:listing"},
    ], "roadmap": "# r\n- one", "note": "n"})
    out = repo.run("--apply")
    assert out.returncode == 0, out.stdout + out.stderr
    w = repo.writes()
    assert w[0] == ["comment", "EARS-2", "-m", "[pm] launch-critical, keep on top"]
    assert w[1] == ["move", "EARS-3", "Todo", "-m", "[pm] cut line item"]
    assert w[2] == ["close", "EARS-4", "-m", "[pm] accepted: shipped in build 1.2.3"]
    assert w[3] == ["cancel", "EARS-5", "-m", "[pm] not launch work"]
    assert w[4] == ["comment", "EARS-2", "-m", "[pm] launch rank 1: gates the Play listing"]
    assert w[5] == ["link", "add", "EARS-3", "EARS-2", "--blocks"]
    assert w[6][:3] == ["task", "create", "Write store listing"]
    assert "-l" in w[6] and w[6][w[6].index("-l") + 1] == "docs"  # lifecycle: labels are dropped
    assert w[6][w[6].index("--idempotency-key") + 1] == "launch:listing"
    assert w[6][w[6].index("--state") + 1] == "Todo"
    assert "degraded: px has no rank verb" in out.stdout
    assert (repo.role / "runtime" / "launch-roadmap.md").read_text() == "# r\n- one\n"


def test_apply_write_cap(repo):
    repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": f"note {i}"} for i in range(10)]})
    out = repo.run("--apply", LAUNCH_PASS_MAX="3")
    assert len(repo.writes()) == 3
    assert "applying the first 3" in out.stdout


def test_apply_default_cap_is_twelve(repo):
    repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": f"note {i}"} for i in range(20)]})
    repo.run("--apply")
    assert len(repo.writes()) == 12


def test_apply_create_cap(repo):
    repo.plan({"writes": [{"op": "create", "title": f"t{i}"} for i in range(6)]})
    out = repo.run("--apply")
    assert len([w for w in repo.writes() if w[:2] == ["task", "create"]]) == 3
    assert "at most 3 creates" in out.stdout


def test_apply_guards_in_progress_and_live_workers_and_duplicates(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, tickets=[
        ticket(2, "In Progress", labels=["lifecycle:review"]),
        ticket(3, "Todo", labels=["agent:working"]),
        ticket(4, "Done"),
        ticket(5, "Todo", comments=[comment("[pm] already said", "2026-11-16T10:00:00+00:00")]),
    ])
    r.plan({"writes": [
        {"op": "move", "ref": "EARS-2", "lane": "Todo", "text": "x"},
        {"op": "cancel", "ref": "EARS-3", "text": "x"},
        {"op": "close", "ref": "EARS-4", "text": "x"},
        {"op": "comment", "ref": "EARS-5", "text": "already said"},
        {"op": "close", "ref": "EARS-2", "text": "accepted"},
    ]})
    out = r.run("--apply")
    assert r.writes() == [["close", "EARS-2", "-m", "[pm] accepted"]]
    for needle in ("Krebs' lane", "live worker", "already Done", "already posted"):
        assert needle in out.stdout


def test_apply_one_readout_per_day_on_the_epic(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, tickets=[
        ticket(1, "Todo", comments=[comment("[pm] readout: 3 of 9 launch-critical done", "2026-11-17T15:00:00+00:00")]),
    ])
    r.plan({"writes": [{"op": "comment", "ref": "EARS-1", "text": "readout: 4 of 9 launch-critical done"}]})
    out = r.run("--apply")
    assert r.writes() == [] and "one readout per day" in out.stdout


def test_apply_holds_the_lease_and_releases_it(repo):
    lock = repo.role / "runtime" / "wip-driver.lock"
    repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}]})
    assert repo.run("--apply").returncode == 0
    # A second driver can take it right away: the first one released.
    got = subprocess.run([sys.executable, str(repo.role / ".scripts" / "board-lease.py"), "acquire", str(lock), "someone-else"], capture_output=True, text=True)
    assert got.returncode == 0


def test_apply_backs_off_when_another_driver_holds_the_lease(repo):
    lock = repo.role / "runtime" / "wip-driver.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = subprocess.run([sys.executable, str(repo.role / ".scripts" / "board-lease.py"), "acquire", str(lock), "interactive"], capture_output=True, text=True)
    assert held.returncode == 0
    plan = repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}]})
    out = repo.run("--apply")
    assert out.returncode == 3 and "LEASE HELD" in out.stdout
    assert repo.writes() == []
    assert plan.exists()  # kept so the pass can re-run it


def test_apply_archives_plan_and_writes_receipt(repo):
    plan = repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}], "note": "hello"})
    out = repo.run("--apply")
    assert not plan.exists()
    assert (repo.role / "runtime" / "launch-pass.applied.json").exists()
    receipts = list((repo.role / "runtime" / "launch-receipts").glob("*.json"))
    assert len(receipts) == 1
    body = json.loads(receipts[0].read_text())
    assert body["note"] == "hello" and body["results"][0]["status"] == "ok"
    assert "receipt:" in out.stdout


def test_apply_reports_px_failures(repo):
    repo.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}]})
    out = repo.run("--apply", PX_SHIM_FAIL="comment")
    assert ": error: px: boom" in out.stdout


def test_apply_unreadable_plan_writes_nothing(repo):
    path = repo.role / "runtime" / "launch-pass.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    out = repo.run("--apply")
    assert out.returncode == 1 and "PLAN UNREADABLE" in out.stdout and repo.writes() == []


def test_apply_roadmap_is_capped_at_forty_lines(repo):
    repo.plan({"writes": [], "roadmap": "\n".join(f"line {i}" for i in range(100))})
    repo.run("--apply")
    lines = (repo.role / "runtime" / "launch-roadmap.md").read_text().splitlines()
    assert len(lines) == 40 and "truncated" in lines[-1]


def test_apply_refuses_krebs_managed_boards(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, project={"ticket_provider": {"identifier": "EARS"}, "execution": {"mode": "managed"}},
             tickets=[ticket(2, "Todo")])
    r.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "x"}]})
    out = r.run("--apply")
    assert out.returncode == 4 and r.writes() == []


# ---------------------------------------------------------------------------------- declaration

def test_check_parses_the_declaration(repo):
    out = repo.run("--check")
    d = json.loads(out.stdout)
    assert d["mode"] == "launch" and d["active"] is True
    assert d["finish_line"] == "Earshot is in the Play Store and a stranger can pay for it."
    assert d["contract"] == ["SPEC.md", "docs/launch.md"]
    assert d["launch_critical"] == ["EARS-2", "EARS-3"] and d["after_launch"] == ["EARS-4"]
    assert d["epic"] == "EARS-1" and d["target_date"] == "2026-12-01"


def test_script_is_executable_and_stdlib_only():
    assert os.access(DRIVER, os.X_OK)
    src = DRIVER.read_text()
    assert src.startswith("#!/usr/bin/env python3")
    for banned in ("import yaml", "import requests", "import httpx"):
        assert banned not in src
