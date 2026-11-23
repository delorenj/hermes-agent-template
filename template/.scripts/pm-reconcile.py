#!/usr/bin/env python3
"""Owning-PM reconciliation. Candybar clocks the external Hermes job; this is not a dispatcher.

The authority and liveness commands are read-only, independently maintained sources. Missing,
malformed or unobservable sources fail closed. Each operation checks a fresh ticket under the
board-driver lease; no timestamp, age or untagged comment alone is an approval.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROLE = HERE.parent
RUNTIME = ROLE / "runtime"
LEASE = HERE / "board-lease.py"
PROMPT = HERE / "pm-reconcile.prompt.md"
POLICY = RUNTIME / "pm-reconcile.json"
PLAN = RUNTIME / "pm-reconcile-plan.json"
RECEIPTS = RUNTIME / "pm-reconcile-receipts"
REF = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
HOLDS = {"lifecycle:needs-input", "lifecycle:blocked", "agent:failed"}
OPS = {"intake", "resolve", "failed"}


def command(argv: list[str], cwd: Path, timeout: int = 30) -> dict:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f"source unavailable ({argv[0]} exit {result.returncode})")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("source returned non-object")
    return value


def configuration() -> dict:
    value = json.loads(POLICY.read_text())
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("enabled") is not True:
        raise ValueError("PM reconciliation not enabled")
    if set(value) - {"version", "enabled", "repo", "employee", "liveness_argv"}:
        raise ValueError("unknown PM reconciliation configuration")
    if not isinstance(value.get("repo"), str) or not Path(value["repo"]).is_dir() or not Path(value["repo"]).is_absolute():
        raise ValueError("repo unavailable")
    if not isinstance(value.get("employee"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value["employee"]):
        raise ValueError("employee unavailable")
    if not (HERE / "pm-reconcile-liveness.mjs").is_file() and not value.get("liveness_argv"):
        raise ValueError("liveness adapter missing")
    return value


def authority(cfg: dict) -> dict:
    data = command(["flume", "reconcile", "authority", cfg["repo"], "--json"], Path(cfg["repo"]))
    if data.get("ok") is not True or data.get("employee") != cfg["employee"] or data.get("enabled") is not True:
        raise ValueError("owner authority unavailable or changed")
    board = data.get("board")
    scopes = data.get("scopes")
    if not isinstance(board, str) or not re.fullmatch(r"[A-Z][A-Z0-9]*", board) or not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise ValueError("scope authority unavailable")
    if len(scopes) != len(set(scopes)):
        raise ValueError("duplicate scope authority")
    if any(s != "(unlabeled)" and not re.fullmatch(r"repo:[a-z0-9_-]+", s) for s in scopes):
        raise ValueError("malformed scope authority")
    return data


def ticket_authority(cfg: dict, ref: str, scope: str) -> None:
    data = command(["flume", "reconcile", "ticket-authority", ref, cfg["repo"], "--json"], Path(cfg["repo"]))
    if data.get("ok") is not True or data.get("employee") != cfg["employee"] or data.get("scope") != scope:
        raise ValueError("fresh ticket owner routing unavailable or changed")


def liveness(cfg: dict, ref: str) -> dict:
    # The default adapter reuses Krebs' read-only Candystore verdict; a fixture may
    # override it with an equally strict executable provider.
    argv = cfg.get("liveness_argv") or ["node", str(HERE / "pm-reconcile-liveness.mjs")]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise ValueError("liveness adapter unavailable")
    data = command([*argv, ref], Path(cfg["repo"]))
    if data.get("ref") != ref or data.get("running") is not False or data.get("queued") is not False or data.get("detached") is not False:
        raise ValueError("liveness unknown or busy")
    return data


def ticket(cfg: dict, ref: str, scope: str, board: str) -> dict:
    data = command(["px", "task", "get", ref, "--json"], Path(cfg["repo"]))
    t = data.get("ticket")
    if data.get("ok") is not True or not isinstance(t, dict) or t.get("ref") != ref or not ref.startswith(board + "-"):
        raise ValueError("ticket unavailable or board mismatch")
    labels = t.get("label_names")
    if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
        raise ValueError("ticket labels unavailable")
    if "lifecycle:human" in labels:
        raise ValueError("human hold: no board writes")
    scopes = [l for l in labels if l.startswith("repo:")]
    if scope == "(unlabeled)":
        if scopes:
            raise ValueError("ticket scope changed")
    elif scopes != [scope]:
        raise ValueError("ticket scope changed or ambiguous")
    if t.get("state_name") not in ("Backlog", "Todo", "In Progress") or not isinstance(t.get("updated_at"), str):
        raise ValueError("ticket state unavailable")
    return t


def intake_relations(t: dict) -> None:
    relations = t.get("relations")
    if not isinstance(relations, list):
        raise ValueError("dependency relations unavailable")
    for relation in relations:
        if not isinstance(relation, dict) or relation.get("resolvable") is not True or not isinstance(relation.get("type"), str) or not isinstance(relation.get("state_name"), str):
            raise ValueError("dependency relation unreadable")
        if relation["type"] == "blocked_by" and relation["state_name"] not in ("Done", "Cancelled"):
            raise ValueError(f'open blocking dependency {relation.get("ref", "unknown")}')


def launch_cut(repo: Path, ref: str) -> None:
    path = repo / ".launch.yaml"
    if not path.exists():
        return
    source = HERE / "launch-pass.py"
    if not source.is_file():
        raise ValueError("launch declaration reader unavailable")
    # Reuse the canonical stdlib-only declaration parser without executing the
    # launch driver's CLI or importing optional runtime dependencies.
    namespace = {"__file__": str(source), "__name__": "pm_launch_declaration"}
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    declaration = namespace["load_declaration"](repo)
    if declaration.get("error") or not declaration.get("raw") or declaration.get("mode") not in ("launch", "live", "steady"):
        raise ValueError("launch declaration unavailable")
    if declaration["mode"] == "launch":
        if not declaration.get("launch_critical") and not declaration.get("after_launch"):
            raise ValueError("launch cut undeclared; ask PM to verify exact cut")
        if ref in declaration.get("after_launch", []):
            raise ValueError("ticket is after declared launch cut")
        if declaration.get("launch_critical") and ref not in declaration["launch_critical"]:
            raise ValueError("ticket not in declared launch critical cut")


def digest(t: dict) -> str:
    # Ignore the PM's own tagged comment on monitor, NOT for apply freshness.
    return hashlib.sha256(json.dumps(t, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def monitored(t: dict) -> str:
    # Ignore only the tagged comment's echo; keep every other ticket field, including
    # a real state/label change. An own comment can update the ticket timestamp.
    copy = dict(t)
    copy.pop("updated_at", None)
    comments = copy.get("comments")
    if isinstance(comments, list):
        copy["comments"] = [c for c in comments if not isinstance(c, dict) or not (str(c.get("text") or c.get("comment_html") or "").startswith("[pm-reconcile]"))]
    return digest(copy)


def read_board(cfg: dict, auth: dict) -> tuple[list[tuple[str, str, dict]], list[str]]:
    listing = command(["px", "task", "list", "--scope", "all", "--json"], Path(cfg["repo"]))
    if listing.get("ok") is not True or listing.get("board") != auth["board"] or not isinstance(listing.get("issues"), list):
        raise ValueError("board unavailable")
    found = []
    excluded: list[str] = []
    for item in listing["issues"]:
        if not isinstance(item, dict) or type(item.get("sequence_id")) is not int or item["sequence_id"] < 1:
            raise ValueError("board entry unreadable")
        ref = f'{auth["board"]}-{item["sequence_id"]}'
        # Read every open ticket to avoid mistaking a listing's missing labels for an empty scope.
        if item.get("state_group") not in ("backlog", "unstarted", "started"):
            continue
        raw = command(["px", "task", "get", ref, "--json"], Path(cfg["repo"]))
        t = raw.get("ticket")
        if raw.get("ok") is not True or not isinstance(t, dict) or t.get("ref") != ref or not isinstance(t.get("label_names"), list) or not all(isinstance(l, str) for l in t["label_names"]):
            raise ValueError("ticket unavailable")
        labels = t["label_names"]
        scopes = [s for s in labels if isinstance(s, str) and s.startswith("repo:")]
        scope = scopes[0] if len(scopes) == 1 else "(unlabeled)" if not scopes else "(ambiguous)"
        if t.get("state_name") == "Backlog":
            if "lifecycle:human" in labels:
                excluded.append(f"{ref}:human hold")
            elif "agent:working" in labels:
                excluded.append(f"{ref}:live worker label")
            elif HOLDS.intersection(labels):
                excluded.append(f"{ref}:held ({','.join(sorted(HOLDS.intersection(labels)))})")
            elif scope not in auth["scopes"]:
                excluded.append(f"{ref}:foreign or ambiguous owner scope ({scope})")
        if scope in auth["scopes"] and not {"lifecycle:human", "agent:working", "lifecycle:review"}.intersection(labels) and (t.get("state_name") == "Backlog" or HOLDS.intersection(labels)):
            ticket_authority(cfg, ref, scope)
            if t.get("state_name") == "Backlog":
                if t.get("priority") not in ("urgent", "high", "medium", "low", "none") or type(t.get("sort_order")) not in (int, float):
                    raise ValueError("Backlog priority unavailable")
                try:
                    intake_relations(t)
                    launch_cut(Path(cfg["repo"]), ref)
                except ValueError as error:
                    excluded.append(f"{ref}:{error}")
                    continue
            found.append((ref, scope, t))
    order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    return sorted(found, key=lambda x: (0 if x[2]["state_name"] == "Backlog" else 1, order.get(str(x[2].get("priority")), 5), x[2].get("sort_order", float("inf")), x[0])), sorted(excluded)


def snapshot(cfg: dict) -> tuple[list[str], list[tuple[str, str, dict]]]:
    auth = authority(cfg)
    rows, excluded = read_board(cfg, auth)
    lines = ["pm-reconcile: enabled", f'owner:{cfg["employee"]}', f'board:{auth["board"]}', "scopes:" + ",".join(sorted(auth["scopes"]))]
    if not any(t["state_name"] == "Backlog" and not HOLDS.intersection(t["label_names"]) for _, _, t in rows):
        lines.append("eligible-backlog:none|reason:no unheld, non-human, non-working Backlog in owned scopes; inspect exclusions and existing Todo")
    lines.extend("excluded|" + why for why in excluded)
    for ref, scope, t in rows:
        try:
            liveness(cfg, ref)
            availability = "idle"
        except (ValueError, OSError, subprocess.SubprocessError):
            availability = "UNAVAILABLE"  # busy and unknown are not actionable
        lines.append(f'{ref}|{scope}|{t["state_name"]}|{",".join(sorted(t["label_names"]))}|{monitored(t)}|{availability}')
    return lines, rows


def monitor() -> int:
    try:
        cfg = configuration()
        lines, _ = snapshot(cfg)
        print("\n".join(lines))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, json.JSONDecodeError):
        print("pm-reconcile: UNAVAILABLE")
    return 0


def brief() -> int:
    try:
        cfg = configuration()
        lines, rows = snapshot(cfg)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, json.JSONDecodeError):
        print("pm-reconcile: UNAVAILABLE; no plan or writes")
        return 0
    print(PROMPT.read_text())
    print("\n# Snapshot\n" + "\n".join(lines))
    for ref, scope, t in rows[:30]:
        print(f'{ref} {scope}: {t.get("name", "")[:100]} (expected {digest(t)})')
    if len(rows) > 30:
        print(f"{len(rows)-30} further candidates omitted; wait for a bounded later pass")
    road = RUNTIME / "pm-reconcile-roadmap.md"
    print("\n# Local context\n" + (road.read_text()[:4000] if road.exists() else "none"))
    print(f"\nWrite a NEW JSON plan filename under {RUNTIME} (never reuse a receipted name). Apply with python3 {HERE / 'pm-reconcile.py'} --apply <absolute-plan-path>.")
    return 0


def lease(cmd: str, lock: Path, owner: str) -> bool:
    return subprocess.run([sys.executable, str(LEASE), cmd, str(lock), owner], capture_output=True, timeout=10).returncode == 0


def receipt(path: Path, entries: list[dict]) -> None:
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    target = RECEIPTS / (path.name + ".receipt.json")
    target.write_text(json.dumps(entries, indent=2) + "\n")


def apply(path: Path) -> int:
    try:
        cfg = configuration()
        if not LEASE.is_file():
            raise ValueError("lease helper missing")
        plan = json.loads(path.read_text())
        if not isinstance(plan, dict) or set(plan) - {"writes", "roadmap", "note"} or not isinstance(plan.get("writes"), list) or len(plan["writes"]) > 6:
            raise ValueError("invalid or over-cap plan")
        if not path.is_absolute() or path.resolve().parent != RUNTIME.resolve() or path.suffix != ".json":
            raise ValueError("plan must be a runtime JSON file")
        if not plan["writes"] or any(not isinstance(w, dict) or set(w) != {"op", "ref", "scope", "expected", "evidence", "hold"} for w in plan["writes"]):
            raise ValueError("plan has no valid writes")
        if len(plan["writes"]) * 2 > 6:
            raise ValueError("plan exceeds six board writes")
        if any(not isinstance(w["ref"], str) for w in plan["writes"]) or len({w["ref"] for w in plan["writes"]}) != len(plan["writes"]):
            raise ValueError("invalid or duplicate ticket in plan")
        owner = os.environ.get("HERMES_BOARD_LEASE_OWNER")
        lock = Path(os.environ.get("HERMES_BOARD_LEASE_PATH") or RUNTIME / "wip-driver.lock")
        if not os.environ.get("HERMES_BOARD_LEASE_PATH") or not owner or not re.fullmatch(r"hermes:" + re.escape(cfg["employee"]) + r":[a-zA-Z0-9_-]+", owner) or lock.resolve() != (RUNTIME / "wip-driver.lock").resolve():
            raise ValueError("exact launcher lease owner/path required")
        # Ensure no existing receipt: a consumed plan is never replayed.
        if (RECEIPTS / (path.name + ".receipt.json")).exists():
            raise ValueError("plan already consumed")
        auth = authority(cfg)
        if not lease("acquire", lock, owner):
            raise ValueError("lease held by another driver")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"REFUSED: {error}; plan retained")
        return 2
    entries: list[dict] = []
    try:
        for i, w in enumerate(plan["writes"]):
            result = {"i": i, "status": "refused"}
            try:
                if not lease("held", lock, owner) or not lease("refresh", lock, owner):
                    raise ValueError("lease lost")
                current_auth = authority(cfg)
                if current_auth != auth:
                    raise ValueError("authority changed")
                if not isinstance(w, dict) or set(w) != {"op", "ref", "scope", "expected", "evidence", "hold"}:
                    raise ValueError("invalid write shape")
                op, ref, scope, expected, evidence, hold = (w[k] for k in ("op", "ref", "scope", "expected", "evidence", "hold"))
                if not isinstance(op, str) or op not in OPS or not isinstance(ref, str) or not REF.fullmatch(ref) or not isinstance(scope, str) or scope not in auth["scopes"] or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1200:
                    raise ValueError("invalid fields or scope")
                if not isinstance(hold, str) or hold not in ("", "lifecycle:needs-input", "lifecycle:blocked", "agent:failed"):
                    raise ValueError("invalid hold")
                t = ticket(cfg, ref, scope, auth["board"])
                ticket_authority(cfg, ref, scope)
                labels = set(t["label_names"])
                if "lifecycle:human" in labels or "agent:working" in labels or "lifecycle:review" in labels:
                    raise ValueError("human, working or review ticket")
                if digest(t) != expected:
                    raise ValueError("stale expected snapshot")
                liveness(cfg, ref)
                if op == "intake":
                    if t["state_name"] != "Backlog" or hold or HOLDS.intersection(labels):
                        raise ValueError("intake requires unheld Backlog")
                    intake_relations(t)
                    launch_cut(Path(cfg["repo"]), ref)
                    if not all(x in evidence.lower() for x in ("authority:", "restriction:", "criteria:", "dependencies:", "files:", "launch-cut:")):
                        raise ValueError("intake requires authority, restriction, criteria, dependencies, files and launch-cut disposition")
                    argv = ["move", ref, "Todo", "-m", "[pm-reconcile] " + evidence]
                else:
                    if (op == "failed" and hold != "agent:failed") or hold not in labels or (op == "resolve" and hold not in HOLDS - {"agent:failed"}):
                        raise ValueError("specified hold not present or wrong recovery")
                    if len(HOLDS.intersection(labels)) != 1 or t["state_name"] not in ("Todo", "In Progress"):
                        raise ValueError("multiple holds or wrong lane")
                    if (op == "resolve" and "recheck:" not in evidence.lower()) or (op == "failed" and ("diagnosis:" not in evidence.lower() or "recovery:" not in evidence.lower())):
                        raise ValueError("hold needs recheck or failed-turn diagnosis and recovery")
                    argv = ["comment", ref, "-m", "[pm-reconcile] " + evidence]
                # All steps are serial under the lease; a failed comment never clears its hold.
                if op != "intake":
                    command_result = subprocess.run(["px", *argv], cwd=cfg["repo"], capture_output=True, text=True, timeout=60)
                    if command_result.returncode:
                        raise ValueError("evidence comment failed")
                    if not lease("held", lock, owner):
                        raise ValueError("lease lost after evidence; hold retained")
                    authority(cfg)
                    fresh = ticket(cfg, ref, scope, auth["board"])
                    ticket_authority(cfg, ref, scope)
                    if monitored(fresh) != monitored(t) or fresh["state_name"] != t["state_name"] or set(fresh["label_names"]) != labels:
                        raise ValueError("ticket changed after evidence; hold retained")
                    liveness(cfg, ref)
                    argv = ["label", "remove", ref, hold]
                run = subprocess.run(["px", *argv], cwd=cfg["repo"], capture_output=True, text=True, timeout=60)
                if run.returncode:
                    raise ValueError(f"px write failed ({run.returncode}); outcome uncertain")
                if op == "intake":
                    if not lease("held", lock, owner) or not lease("refresh", lock, owner):
                        raise ValueError("lease lost after intake; Todo left untriaged")
                    authority(cfg)
                    accepted = ticket(cfg, ref, scope, auth["board"])
                    ticket_authority(cfg, ref, scope)
                    if accepted["state_name"] != "Todo" or not {"agent:working", "lifecycle:human", "lifecycle:review", "lifecycle:triaged"}.isdisjoint(accepted["label_names"]) or HOLDS.intersection(accepted["label_names"]):
                        raise ValueError("ticket changed after intake; Todo left untriaged")
                    intake_relations(accepted)
                    launch_cut(Path(cfg["repo"]), ref)
                    liveness(cfg, ref)
                    triage = subprocess.run(["px", "label", "add", ref, "lifecycle:triaged"], cwd=cfg["repo"], capture_output=True, text=True, timeout=60)
                    if triage.returncode:
                        raise ValueError(f"triage label failed ({triage.returncode}); Todo left untriaged")
                result.update(status="ok", ref=ref, op=op)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                result["reason"] = str(error)[:240]
            entries.append(result)
            receipt(path, entries)
            if result["status"] != "ok":
                break  # preserve remaining operations for an independently fresh plan
        road = plan.get("roadmap")
        if len(entries) == len(plan["writes"]) and all(e["status"] == "ok" for e in entries) and isinstance(road, str) and len(road) <= 4000:
            (RUNTIME / "pm-reconcile-roadmap.md").write_text(road)
    finally:
        lease("release", lock, owner)
    print(json.dumps(entries))
    return 0 if len(entries) == len(plan["writes"]) and all(e["status"] == "ok" for e in entries) else 1


if __name__ == "__main__":
    if "--monitor" in sys.argv:
        raise SystemExit(monitor())
    if "--brief" in sys.argv:
        raise SystemExit(brief())
    if "--apply" in sys.argv:
        pos = sys.argv.index("--apply")
        raise SystemExit(apply(Path(sys.argv[pos + 1]) if len(sys.argv) > pos + 1 else PLAN))
    print(__doc__)
