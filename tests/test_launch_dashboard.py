"""launch-dashboard.py: the owner's runbook every launch-mode PM keeps current (docs/launch-dashboard.md).

Pure rules run in-process against the module; everything that touches px, git or the publisher runs the real
scripts in a throwaway repo with the same px shim and a stub publish.py (no network) as test_launch_pass.py.
"""
import copy
import importlib.util
import json
import re
import subprocess
import sys

import pytest

from test_launch_pass import DECL, Repo, SCRIPTS, comment, ticket

PROJECT = {"project_name": "Earshot", "project_id": "earshot",
           "ticket_provider": {"identifier": "EARS", "workspace": "33god"}}
DASH_DECL = DECL.replace("declared: 2026-11-17", "declared: 2026-11-18")
CATALOG = json.loads((SCRIPTS / "lib" / "launch-marketplaces.json").read_text())


def load_mod(repo):
    for k in ("launch_pass", "launch_dashboard"):
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location("launch_dashboard", repo.role / ".scripts" / "launch-dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["launch_dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod


def good(**over):
    item = {
        "id": "play-create-app", "order": 50, "title": "Create the Earshot app in Play Console", "why": "Nothing Android can upload until the app exists.",
        "kind": "console", "marketplace": "google_play",
        "steps": ["Open https://play.google.com/console/u/1/developers and pick the account.", "Press Create app and fill the form."],
        "reply": "Play app created", "done_when": "The Play Developer API lists the package.",
        "recommend": "Do it now: it unblocks EARS-2.", "default": None, "refs": ["EARS-2"],
        "artifacts": [{"label": "Audit", "url": "https://github.com/delorenj/earshot/blob/main/docs/audit.md"}],
        "after": [], "covers": ["gp.create-app"], "status": "open", "evidence": None, "verified_at": None,
    }
    item.update(over)
    return item


def publish_item(mp="google_play", **over):
    base = dict(id="publish-" + mp.replace("_", "-"), order=900, title=f"Press publish in {mp}", kind="publish", marketplace=mp, reply=None, covers=[],
                steps=["Open https://play.google.com/console/u/1/developers and the release.", "Press Publish release."])
    base.update(over)
    return good(**base)


@pytest.fixture
def dash(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=DASH_DECL, project=PROJECT, tickets=[
        ticket(2, "Todo", pri="high", name="Wire the store listing", description="## Goal\n\nPut the listing text in the Play API so nobody types it.\n\n## Notes\nlater"),
        ticket(3, "Backlog", pri="medium", name="Something later"),
        ticket(4, "Todo", labels=["lifecycle:needs-input"], name="Pick the price", description="Decide what a stranger pays. More words follow."),
        ticket(5, "Backlog", pri="urgent", labels=["bug"], name="Crash on launch"),
        ticket(6, "Backlog", pri="high", labels=["bug"], external="x", name="Agent-filed bug"),
        ticket(7, "Done", name="Shipped thing"),
    ])
    (r.role / "role.yaml").write_text('agent_id: "demo-pm"\ntelegram:\n  provisioning_status: "verified"\n  bot_username: "earshot_pm_bot"\n')
    g = lambda *a: subprocess.run(["git", "-C", str(r.root), *a], capture_output=True, text=True, check=True)
    g("init", "-q")
    g("remote", "add", "origin", "git@github.com:delorenj/earshot.git")
    (r.root / "docs").mkdir()
    (r.root / "docs" / "launch.md").write_text("x\n")
    (r.root / "SPEC.md").write_text("x\n")
    g("add", "docs/launch.md", "SPEC.md")
    r.git = g
    return r


def write_data(r, data):
    path = r.role / "runtime" / "launch-dashboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def read_data(r):
    return json.loads((r.role / "runtime" / "launch-dashboard.json").read_text())


def send(r, ops, **env):
    path = r.state / "ops.json"
    path.write_text(json.dumps(ops))
    return dash_run(r, "--ops", str(path), **env)


def dash_run(r, *args, **env):
    return subprocess.run([sys.executable, str(r.role / ".scripts" / "launch-dashboard.py"), *args],
                          capture_output=True, text=True, env={**r.env, **env}, cwd=str(r.root), timeout=90)


def render(r, data, tmp_path, **env):
    out = tmp_path / "site"
    d = tmp_path / "data.json"
    d.write_text(json.dumps(data))
    res = dash_run(r, "--render", "--out", str(out), "--data", str(d), **env)
    assert res.returncode == 0, res.stdout + res.stderr
    return (out / "index.html").read_text(), json.loads((out / "dashboard.json").read_text())


def ctx_for(mod, prefixes=("EARS",)):
    return mod.item_ctx({}, mod.load_catalog(), set(prefixes))


def full_data(mp="google_play"):
    reqs = {r["id"]: {"status": "na", "why": "not relevant in this fixture"} for r in CATALOG["requirements"] if r["marketplace"] == mp}
    return {"schema": "launch-dashboard/1", "marketplaces": [mp], "items": [publish_item(mp)], "requirements": reqs}


# ------------------------------------------------------------------------------------- catalog

def test_catalog_is_the_researched_set_with_caveats_folded_in():
    assert CATALOG["schema"] == "launch-marketplaces/1" and CATALOG["checked"] == "2026-11-20"
    reqs = CATALOG["requirements"]
    assert len(reqs) == 72 and len({r["id"] for r in reqs}) == 72
    assert {r["marketplace"] for r in reqs} == {"google_play", "app_store"}
    assert set(CATALOG["marketplaces"]) == {"google_play", "app_store"}
    for r in reqs:
        for key in ("id", "marketplace", "title", "applies_when", "why", "owner_only", "agent_path", "owner_steps", "verify", "doc_url", "checked"):
            assert key in r, (r["id"], key)
        assert r["doc_url"].startswith("https://") and r["applies_when"].strip()
    by_id = {r["id"]: r for r in reqs}
    assert "opted-in" in by_id["gp.closed-test-12-testers-14-days"]["caveat"]
    assert "2027-02-01" in by_id["gp.native-16kb-page-size"]["caveat"]
    assert "unofficial" in by_id["as.app-privacy-details"]["agent_path"] or "No App Store Connect API" in by_id["as.app-privacy-details"]["caveat"]
    assert by_id["gp.create-app"]["applies_when"] == "always"
    assert by_id["gp.payments-profile-merchant"]["applies_when"] != "always"


def test_check_validates_the_catalog_and_an_empty_data_file(dash):
    out = dash_run(dash, "--check")
    assert out.returncode == 0, out.stdout
    body = json.loads(out.stdout)
    assert body["ok"] is True and body["catalog_rows"] == 72


# ------------------------------------------------------------------------------------- the rules

def test_a_complete_item_passes(dash):
    mod = load_mod(dash)
    assert mod.item_problems(good(), ctx_for(mod)) == []
    assert mod.item_problems(publish_item(), ctx_for(mod)) == []


REFUSALS = [
    ("no steps", {"steps": []}, "`steps`"),
    ("no done_when", {"done_when": ""}, "done_when"),
    ("no reply", {"reply": ""}, "`reply`"),
    ("no recommendation", {"recommend": None}, "`recommend`"),
    ("one step for a console item", {"steps": ["Open https://play.google.com/console and press Create app."]}, "at least two"),
    ("first step names no URL", {"steps": ["Go to the Play Console.", "Press Create app."]}, "full https URL"),
    ("http first step is not a full https URL", {"steps": ["Open http://play.google.com/console now.", "Press Create app."]}, "full https URL"),
    ("a default on a money step", {"kind": "money", "default": "Pay it for him."}, "no default"),
    ("a default on a legal step", {"kind": "legal", "default": "Accept for him."}, "no default"),
    ("unknown kind", {"kind": "chore"}, "`kind`"),
    ("verify the build in the title", {"title": "Verify the build on your phone"}, "verify, test or sign off"),
    ("test the build in a step", {"steps": ["Open https://play.google.com/console and go.", "Test the new APK on the S26."]}, "verify, test or sign off"),
    ("sign off the build in the reply", {"reply": "sign off on the build"}, "verify, test or sign off"),
    ("approve the candidate", {"title": "Approve the candidate"}, "verify, test or sign off"),
    ("a file:// artifact", {"artifacts": [{"label": "x", "url": "file:///home/x/a.png"}]}, "https URL"),
    ("an http artifact", {"artifacts": [{"label": "x", "url": "http://example.com/a"}]}, "https URL"),
    ("a bucket key artifact", {"artifacts": [{"label": "x", "url": "projects/earshot/2026-11-18/x/index.html"}]}, "https URL"),
    ("a local path in a step", {"steps": ["Open https://play.google.com/console and go.", "Look at /home/delorenj/code/x/shot.png"]}, "local path"),
    ("an s3 key in the why", {"why": "see s3://projects/earshot/x"}, "local path"),
    ("a bucket key in a step", {"steps": ["Open https://play.google.com/console and go.", "Read projects/earshot/2026-11-18/report/index.html"]}, "local path"),
    ("a builds bucket key", {"why": "the page is builds/earshot/index.html"}, "local path"),
    ("a home-relative path", {"steps": ["Open https://play.google.com/console and go.", "Attach ~/code/earshot/shot.png"]}, "local path"),
    ("sign off on a release build", {"title": "Sign off on the release build"}, "verify, test or sign off"),
    ("verify the new build", {"steps": ["Open https://play.google.com/console and go.", "Verify that the new build opens."]}, "verify, test or sign off"),
    ("a noun-phrase title", {"title": "The Play Console app"}, "imperative"),
    ("a question title", {"title": "Create the app?"}, "imperative"),
    ("a ref on another board", {"refs": ["OTHER-9"]}, "not on this board"),
    ("a malformed ref", {"refs": ["nope"]}, "`refs`"),
    ("an unknown field", {"priority": "high"}, "unknown field"),
    ("a covers id outside the catalog", {"covers": ["gp.not-real"]}, "not a catalog requirement"),
    ("an unknown marketplace", {"marketplace": "steam"}, "`marketplace`"),
    ("a bad id", {"id": "Play Create"}, "`id`"),
    ("no order", {"order": "soon"}, "`order`"),
]


@pytest.mark.parametrize("label,over,needle", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_the_validator_refuses(dash, label, over, needle):
    mod = load_mod(dash)
    problems = mod.item_problems(good(**over), ctx_for(mod))
    assert any(needle in p for p in problems), (label, problems)


def test_a_https_url_with_a_bucket_path_and_a_verify_your_identity_step_are_fine(dash):
    mod = load_mod(dash)
    item = good(steps=["Open https://play.google.com/console and go.", "Verify your identity with the code, then upload the build to Production.",
                       "See https://s3.delo.sh/projects/earshot/2026-11-18/report/index.html and docs/builds/notes.md"])
    assert mod.item_problems(item, ctx_for(mod)) == []


def test_publish_needs_a_marketplace_and_a_second_step(dash):
    mod = load_mod(dash)
    assert any("name its `marketplace`" in p for p in mod.item_problems(publish_item(marketplace=None), ctx_for(mod)))
    assert any("two concrete steps" in p for p in mod.item_problems(publish_item(steps=["Open https://play.google.com/console now."]), ctx_for(mod)))


def test_reversible_decisions_may_carry_a_default_and_decisions_need_no_url(dash):
    mod = load_mod(dash)
    item = good(kind="decision", marketplace=None, steps=["Pick a price.", "Reply with it."], default="Free, with no in-app purchases.")
    assert mod.item_problems(item, ctx_for(mod)) == []


def test_tester_and_release_talk_is_not_mistaken_for_a_build_check(dash):
    mod = load_mod(dash)
    item = good(title="Recruit twelve testers for the closed test", steps=["Open https://play.google.com/console and go to Testing.", "Add twelve Google accounts to the tester list for the release."])
    assert mod.item_problems(item, ctx_for(mod)) == []


# ------------------------------------------------------------------------------------- ops

def apply(mod, data, ops):
    return mod.apply_ops(data, ops, mod.load_catalog(), {"EARS"})


def test_upsert_adds_replaces_and_keeps_the_checked_state(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    assert apply(mod, data, [{"op": "upsert", "item": good()}])[0]["detail"] == "added"
    assert apply(mod, data, [{"op": "check", "id": "play-create-app", "evidence": "edits.insert on the package returned 200"}])[0]["status"] == "ok"
    e = apply(mod, data, [{"op": "upsert", "item": good(title="Create the Earshot app in the Play Console")}])[0]
    assert e["detail"] == "replaced" and len(data["items"]) == 1
    assert data["items"][0]["status"] == "done" and data["items"][0]["evidence"].startswith("edits.insert")
    assert data["items"][0]["title"].endswith("the Play Console")


def test_upsert_refuses_an_invalid_item_and_says_why_and_changes_nothing(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    e = apply(mod, data, [{"op": "upsert", "item": good(steps=["one"])}])[0]
    assert e["status"] == "refused" and "two concrete steps" in e["detail"] and data["items"] == []
    e = apply(mod, data, [{"op": "upsert", "item": good(status="done", evidence="x")}])[0]
    assert e["status"] == "refused" and "check" in e["detail"]
    e = apply(mod, data, [{"op": "upsert", "item": good(after=["ghost"])}])[0]
    assert e["status"] == "refused" and "not an item" in e["detail"]


def test_after_cycles_are_refused(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    apply(mod, data, [{"op": "upsert", "item": good(id="a-step", title="Do the first thing")},
                      {"op": "upsert", "item": good(id="b-step", title="Do the second thing", after=["a-step"])}])
    e = apply(mod, data, [{"op": "upsert", "item": good(id="a-step", title="Do the first thing", after=["b-step"])}])[0]
    assert e["status"] == "refused" and "loops" in e["detail"]


def test_check_needs_real_evidence_never_jarads_reply(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    apply(mod, data, [{"op": "upsert", "item": good()}])
    for ev in ("", "ok", "done", "Jarad said it is done", "Jarad replied: Play app created", "x" * 3):
        e = apply(mod, data, [{"op": "check", "id": "play-create-app", "evidence": ev}])[0]
        assert e["status"] == "refused", ev
    assert data["items"][0]["status"] == "open"
    e = apply(mod, data, [{"op": "check", "id": "play-create-app", "evidence": "https://play.google.com/console/u/1/developers/1/app-list lists it"}])[0]
    assert e["status"] == "ok" and data["items"][0]["status"] == "done" and data["items"][0]["verified_at"]


def test_uncheck_and_remove_need_a_why_and_remove_refuses_dependents(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    apply(mod, data, [{"op": "upsert", "item": good(id="a-step", title="Do the first thing")},
                      {"op": "upsert", "item": good(id="b-step", title="Do the second thing", after=["a-step"])}])
    assert apply(mod, data, [{"op": "check", "id": "a-step", "evidence": "read it back from the console"}])[0]["status"] == "ok"
    assert apply(mod, data, [{"op": "uncheck", "id": "a-step"}])[0]["status"] == "refused"
    assert apply(mod, data, [{"op": "uncheck", "id": "a-step", "why": "console shows it gone"}])[0]["status"] == "ok"
    assert data["items"][0]["status"] == "open" and data["items"][0]["evidence"] is None
    e = apply(mod, data, [{"op": "remove", "id": "a-step", "why": "obsolete"}])[0]
    assert e["status"] == "refused" and "b-step" in e["detail"]
    assert apply(mod, data, [{"op": "remove", "id": "b-step", "why": "obsolete"}])[0]["status"] == "ok"
    assert apply(mod, data, [{"op": "remove", "id": "ghost", "why": "x"}])[0]["detail"] == "no such item"


def test_requirement_op_rules(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    apply(mod, data, [{"op": "upsert", "item": good()}])
    cases = [
        ({"id": "gp.privacy-policy", "status": "met"}, "refused"),
        ({"id": "gp.privacy-policy", "status": "met", "evidence": "https://example.com/privacy"}, "ok"),
        ({"id": "gp.data-safety", "status": "agent", "ref": "EARS-2"}, "ok"),
        ({"id": "gp.data-safety", "status": "agent", "ref": "OTHER-2"}, "refused"),
        ({"id": "gp.data-safety", "status": "agent"}, "refused"),
        ({"id": "gp.create-app", "status": "owner", "item": "play-create-app"}, "ok"),
        ({"id": "gp.create-app", "status": "owner", "item": "ghost"}, "refused"),
        ({"id": "gp.payments-profile-merchant", "status": "na"}, "refused"),
        ({"id": "gp.payments-profile-merchant", "status": "na", "why": "Free game, no in-app purchases."}, "ok"),
        ({"id": "gp.not-real", "status": "na", "why": "x"}, "refused"),
        ({"id": "gp.create-app", "status": "maybe"}, "refused"),
    ]
    for body, want in cases:
        e = apply(mod, data, [{"op": "requirement", **body}])[0]
        assert e["status"] == want, (body, e)
    assert data["requirements"]["gp.payments-profile-merchant"]["why"].startswith("Free game")


def test_marketplaces_op_takes_only_catalog_marketplaces(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    assert apply(mod, data, [{"op": "marketplaces", "value": ["steam"]}])[0]["status"] == "refused"
    assert apply(mod, data, [{"op": "marketplaces", "value": []}])[0]["status"] == "refused"
    assert apply(mod, data, [{"op": "marketplaces", "value": ["google_play", "app_store", "google_play"]}])[0]["status"] == "ok"
    assert data["marketplaces"] == ["google_play", "app_store"]


def test_unknown_ops_and_oversized_batches_are_refused_with_a_reason(dash):
    mod = load_mod(dash)
    data = mod.empty_data()
    assert "one of" in apply(mod, data, [{"op": "delete"}, "nope"])[0]["detail"]
    assert apply(mod, data, "x")[0]["status"] == "refused"
    many = apply(mod, data, [{"op": "check", "id": "x", "evidence": "y"}] * (mod.MAX_OPS + 3))
    assert many[0]["ref"] == "cap" and len(many) == mod.MAX_OPS + 1


# ------------------------------------------------------------------------------ order + the page

def test_runbook_numbering_follows_order_then_after_and_done_items_stay_put(dash, tmp_path):
    items = [
        good(id="third", order=10, title="Do the third thing", after=["second"]),
        good(id="second", order=30, title="Do the second thing", after=["first"], status="done", evidence="read back from the console"),
        good(id="first", order=20, title="Do the first thing"),
    ]
    html, model = render(dash, {"schema": "launch-dashboard/1", "marketplaces": ["google_play"], "items": items, "requirements": {}}, tmp_path)
    assert [(i["id"], i["number"]) for i in model["items"]] == [("first", 1), ("second", 2), ("third", 3)]
    assert re.search(r'<details class="step done" id="step-2">', html), "a done step stays in place, collapsed"
    assert re.search(r'<details class="step" id="step-1" open>', html)
    assert "read back from the console" in html
    assert 'href="#step-2"' in html, "after N links to the step"


def test_the_page_is_self_contained_themed_and_phone_safe(dash, tmp_path):
    html, _ = render(dash, full_data(), tmp_path)
    assert "<script src=" not in html and 'rel="stylesheet"' not in html and "@import" not in html
    assert ':root[data-theme="dark"]' in html and ':root:not([data-theme="light"])' in html and "prefers-color-scheme:dark" in html
    assert "background:var(--bg)" in html and 'name="viewport"' in html and "env(safe-area-inset-left" in html
    assert "<title>Earshot launch dashboard</title>" in html
    assert "@earshot_pm_bot" in html and "comment on the linked ticket" in html
    assert "Last refreshed" in html and ("EST" in html or "EDT" in html)
    assert "Earshot is in the Play Store and a stranger can pay for it." in html


def test_the_page_header_lists_marketplaces_counts_and_the_store_table(dash, tmp_path):
    html, model = render(dash, full_data(), tmp_path)
    assert "Google Play" in html and "Store checklist" in html
    assert html.count("<tr><td>") == 37
    assert model["counts"] == {"open": 1, "done": 0, "gaps": model["counts"]["gaps"]}
    assert "n/a" in html and "not relevant in this fixture" in html


# --------------------------------------------------------------------------- completeness (rule 7)

def gap_kinds(model):
    return sorted({g["kind"] for g in model["gaps"]})


def test_an_empty_dashboard_is_incomplete_and_the_banner_says_so(dash, tmp_path):
    html, model = render(dash, {"schema": "launch-dashboard/1", "marketplaces": [], "items": [], "requirements": {}}, tmp_path)
    assert gap_kinds(model) == ["marketplace", "waiting"] and model["complete"] is False
    assert "Incomplete" in html and 'class="banner bad"' in html and "Complete: every barrier" not in html
    assert "EARS-4" in " ".join(g["text"] for g in model["gaps"])


def test_a_catalog_row_with_no_entry_is_a_gap_and_a_conditional_row_still_needs_na(dash, tmp_path):
    data = full_data()
    del data["requirements"]["gp.create-app"]
    del data["requirements"]["gp.payments-profile-merchant"]
    _, model = render(dash, data, tmp_path)
    texts = [g["text"] for g in model["gaps"] if g["kind"] == "catalog"]
    assert any("gp.create-app" in t and "no entry" in t for t in texts)
    cond = [t for t in texts if "gp.payments-profile-merchant" in t][0]
    assert "conditional" in cond and "`na`" in cond


@pytest.mark.parametrize("entry,needle", [
    ({"status": "met"}, "met with no evidence"),
    ({"status": "agent", "ref": "EARS-999"}, "not on the board"),
    ({"status": "owner", "item": "ghost"}, "does not exist"),
    ({"status": "na"}, "n/a with no reason"),
    ({"status": "met", "evidence": "yes"}, "hollow evidence"),
    ({"status": "met", "evidence": "Jarad said it is fine"}, "hollow evidence"),
    ({"status": "na", "why": "n/a"}, "hollow reason"),
    ({"status": "agent"}, "agent with no ticket"),
])
def test_a_requirement_entry_that_does_not_hold_up_is_a_gap(dash, tmp_path, entry, needle):
    data = full_data()
    data["requirements"]["gp.create-app"] = entry
    _, model = render(dash, data, tmp_path)
    assert any(g["id"] == "gp.create-app" and needle in g["text"] for g in model["gaps"]), model["gaps"]
    assert [r for r in model["requirements"] if r["id"] == "gp.create-app"][0]["state"] == "gap"


def test_an_agent_ticket_that_exists_closed_is_not_a_gap(dash, tmp_path):
    data = full_data()
    data["requirements"]["gp.create-app"] = {"status": "agent", "ref": "EARS-7"}
    _, model = render(dash, data, tmp_path)
    assert not any(g["id"] == "gp.create-app" for g in model["gaps"])


def test_a_launch_critical_ticket_waiting_on_him_needs_a_step_that_references_it(dash, tmp_path):
    data = full_data()
    _, model = render(dash, data, tmp_path)
    assert [g["id"] for g in model["gaps"] if g["kind"] == "waiting"] == ["EARS-4"]
    data["items"].append(good(id="pick-price", order=20, title="Pick the price", marketplace=None, kind="decision", steps=["Choose a price.", "Reply with it."],
                              refs=["EARS-4"], covers=[], reply="price: free"))
    _, model = render(dash, data, tmp_path)
    assert not [g for g in model["gaps"] if g["kind"] == "waiting"]
    data["items"][-1]["refs"] = []
    data["items"][-1]["why"] = "Settles EARS-4 for the listing."  # a ref in the text counts too
    _, model = render(dash, data, tmp_path)
    assert not [g for g in model["gaps"] if g["kind"] == "waiting"]


def test_a_marketplace_with_no_publish_item_is_a_gap(dash, tmp_path):
    data = full_data()
    data["items"] = []
    _, model = render(dash, data, tmp_path)
    assert any(g["kind"] == "publish" and g["id"] == "google_play" for g in model["gaps"])
    data["marketplaces"] = ["google_play", "app_store"]
    data["items"] = [publish_item("google_play")]
    _, model = render(dash, data, tmp_path)
    assert [g["id"] for g in model["gaps"] if g["kind"] == "publish"] == ["app_store"]


def test_an_item_that_breaks_a_rule_is_a_gap_and_a_badge_never_a_crash(dash, tmp_path):
    data = full_data()
    data["items"].append(good(id="bad-one", order=10, steps=["only one"]))
    html, model = render(dash, data, tmp_path)
    assert any(g["kind"] == "item" and g["id"] == "bad-one" and "two concrete steps" in g["text"] for g in model["gaps"])
    assert "breaks a rule" in html


def test_an_unreadable_board_is_a_gap_so_the_page_never_claims_complete(dash, tmp_path):
    (dash.state / "board.json").unlink()
    html, model = render(dash, full_data(), tmp_path)
    assert any(g["kind"] == "board" for g in model["gaps"]) and model["complete"] is False
    assert "Complete: every barrier" not in html


def test_complete_only_when_there_are_no_gaps(dash, tmp_path):
    data = full_data()
    data["items"].append(good(id="pick-price", order=20, title="Pick the price", marketplace=None, kind="decision", steps=["Choose a price.", "Reply with it."],
                              refs=["EARS-4"], covers=[], reply="price: free"))
    html, model = render(dash, data, tmp_path)
    assert model["complete"] is True and model["gaps"] == []
    assert "Complete: every barrier is on this page" in html and "Incomplete" not in html


# ------------------------------------------------------------------------------ linking (rules 1, 2, 5)

def test_every_ticket_reference_is_a_link_with_a_hover_card(dash, tmp_path):
    data = full_data()
    data["items"][0]["why"] = "Needs EARS-2 and EARS-4 and EARS-7 first. SHA-256 is not a ticket."
    data["items"][0]["refs"] = ["EARS-2"]
    html, model = render(dash, data, tmp_path)
    for ref in ("EARS-2", "EARS-4", "EARS-7"):
        assert f'href="https://plane.delo.sh/33god/browse/{ref}/"' in html and f'data-ref="{ref}"' in html
    assert "SHA-256" in html and 'data-ref="SHA-256"' not in html
    cards = json.loads(re.search(r'id="tk-data">(.*?)</script>', html, re.S).group(1))
    assert cards["EARS-2"]["title"] == "Wire the store listing" and cards["EARS-2"]["lane"] == "Todo" and cards["EARS-2"]["pri"] == "high"
    assert cards["EARS-2"]["gist"] == "Put the listing text in the Play API so nobody types it."
    assert cards["EARS-4"]["waiting"] is True and cards["EARS-4"]["gist"] == "Decide what a stranger pays."
    assert cards["EARS-7"]["lane"] == "Done"
    assert "Open ticket" in html and "hover: none" in html, "touch: first tap shows the card, its link opens the ticket"
    assert model["tickets"]["EARS-2"]["url"].endswith("/EARS-2/")


def test_a_plane_url_from_the_ticket_wins_over_the_derived_one(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration=DASH_DECL, project=PROJECT, tickets=[ticket(2, "Todo", url="https://plane.example/x/browse/EARS-2/")])
    data = full_data()
    data["items"][0]["refs"] = ["EARS-2"]
    html, _ = render(r, data, tmp_path)
    assert 'href="https://plane.example/x/browse/EARS-2/"' in html


def test_bare_urls_and_repo_paths_are_links_and_other_text_is_not(dash, tmp_path):
    data = full_data()
    data["items"][0]["why"] = "Read docs/launch.md and SPEC.md, see https://example.com/a?b=1. Not docs/missing.md or e.g. this."
    html, _ = render(dash, data, tmp_path)
    assert 'href="https://github.com/delorenj/earshot/blob/main/docs/launch.md"' in html
    assert 'href="https://github.com/delorenj/earshot/blob/main/SPEC.md"' in html
    assert 'href="https://example.com/a?b=1"' in html
    assert "blob/main/docs/missing.md" not in html and "docs/missing.md" in html
    assert 'href="https://play.google.com/console/u/1/developers"' in html, "a URL inside a step is a link"


def test_repo_paths_in_a_child_scope_repo_link_to_that_repo(tmp_path, monkeypatch):
    decl = DASH_DECL.replace("epic: EARS-1\n", "epic: EARS-1\nscopes: [kid]\n")
    r = Repo(tmp_path, monkeypatch, declaration=decl, project=PROJECT, tickets=[ticket(2, "Todo")])
    kid = r.root / "kid"
    (kid / "docs").mkdir(parents=True)
    (kid / ".project.json").write_text(json.dumps({"ticket_provider": {"identifier": "EARS"}}))
    (kid / "docs" / "tower.md").write_text("x\n")
    for cwd, args in ((r.root, ["init", "-q"]), (kid, ["init", "-q"])):
        subprocess.run(["git", "-C", str(cwd), *args], check=True)
    subprocess.run(["git", "-C", str(r.root), "remote", "add", "origin", "https://github.com/delorenj/parent.git"], check=True)
    subprocess.run(["git", "-C", str(kid), "remote", "add", "origin", "git@github.com:delorenj/tower.git"], check=True)
    subprocess.run(["git", "-C", str(kid), "add", "docs/tower.md", ".project.json"], check=True)
    (r.state / "board-kid.json").write_text(json.dumps({"board": "EARS", "tickets": [ticket(9, "Todo", name="Child ticket")]}))
    data = full_data()
    data["items"][0]["why"] = "See kid/docs/tower.md and docs/tower.md and EARS-9."
    html, _ = render(r, data, tmp_path)
    assert html.count('href="https://github.com/delorenj/tower/blob/main/docs/tower.md"') == 2
    assert 'data-ref="EARS-9"' in html


# --------------------------------------------------------------------------- publish item auto list

def test_a_publish_item_lists_open_launch_critical_work_and_owner_filed_blockers(dash, tmp_path):
    html, model = render(dash, full_data(), tmp_path)
    refs = {t["ref"]: t["why"] for t in model["agent_work"]}
    assert "EARS-2" in refs and refs["EARS-2"] == "launch-critical"        # declared launch-critical and in Todo
    assert refs["EARS-5"] == "owner-filed High/Urgent bug"                   # owner-filed urgent bug
    assert refs["EARS-3"] == "launch-critical"                               # the declaration's cut-line hint, even in Backlog
    assert "EARS-6" not in refs and "EARS-7" not in refs                     # an agent-filed bug and a closed ticket
    block = html[html.index("Agent work still open"):]
    assert 'data-ref="EARS-2"' in block and 'data-ref="EARS-5"' in block and "waiting on you" in block
    assert html.count("Agent work still open") == 1 and html.count('class="agentwork"') == 1


def test_an_item_that_is_not_a_publish_item_has_no_agent_list(dash, tmp_path):
    data = full_data()
    data["items"] = [good()]
    html, _ = render(dash, data, tmp_path)
    assert "Agent work still open" not in html


# ------------------------------------------------------------------------------ publish + modes

def test_ops_apply_render_and_publish_to_the_stable_url_and_record_it(dash):
    out = send(dash, {"dashboard": [{"op": "marketplaces", "value": ["google_play"]}, {"op": "upsert", "item": good()}]})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "dashboard marketplaces: ok" in out.stdout and "dashboard upsert play-create-app: ok: added" in out.stdout
    url = "https://s3.delo.sh/projects/earshot/2026-11-18/launch-dashboard/index.html"
    assert f"dashboard publish: ok: {url}" in out.stdout
    assert read_data(dash)["published"]["url"] == url
    calls = [json.loads(l) for l in (dash.state / "publish.log").read_text().splitlines()]
    assert calls[0]["files"] == ["dashboard.json", "index.html"]
    assert calls[0]["args"][1:] == ["--name", "launch-dashboard", "--project", "earshot", "--date", "2026-11-18"], "the first publish builds the index"
    out = dash_run(dash, "--publish")
    assert "dashboard publish: ok" in out.stdout
    calls = [json.loads(l) for l in (dash.state / "publish.log").read_text().splitlines()]
    assert "--no-index" in calls[1]["args"], "a republish skips the index rebuild"
    assert json.loads((dash.role / "runtime" / "launch-dashboard-site" / "dashboard.json").read_text())["url"] == url


def test_a_refused_op_prints_why_and_a_failed_publish_is_reported_not_fatal(dash):
    out = send(dash, [{"op": "upsert", "item": good(steps=["one"])}], PUBLISH_STUB_FAIL="1")
    assert "dashboard upsert play-create-app: refused: " in out.stdout and "two concrete steps" in out.stdout
    assert "dashboard publish: error: publisher: boom" in out.stdout and out.returncode == 1
    assert not (dash.role / "runtime" / "launch-dashboard.json").exists(), "nothing valid landed, so nothing was written"


def test_ops_file_unreadable_applies_nothing(dash):
    path = dash.state / "ops.json"
    path.write_text("{not json")
    out = dash_run(dash, "--ops", str(path))
    assert out.returncode == 1 and "OPS UNREADABLE" in out.stdout


def test_render_mode_never_touches_runtime(dash, tmp_path):
    render(dash, full_data(), tmp_path)
    assert not (dash.role / "runtime" / "launch-dashboard.json").exists()
    assert not (dash.role / "runtime" / "launch-dashboard-site").exists()
    assert not (dash.state / "publish.log").exists()


def test_the_dashboard_is_inert_unless_launch_mode(tmp_path, monkeypatch):
    r = Repo(tmp_path, monkeypatch, declaration="mode: steady\ndeclared: 2026-11-17\n", project=PROJECT)
    for mode in ("--brief", "--publish", "--check", "--render"):
        out = dash_run(r, mode)
        assert out.returncode == 0 and "inert" in out.stdout, mode
    assert not (r.role / "runtime").exists()


# ---------------------------------------------------------------------------- the launch pass (apply)

def test_apply_runs_dashboard_ops_after_the_board_writes_and_republishes_every_time(dash):
    dash.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "on it"}],
               "dashboard": [{"op": "marketplaces", "value": ["google_play"]}, {"op": "upsert", "item": good()}, {"op": "upsert", "item": good(id="b", steps=[])}]})
    out = dash.run("--apply")
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    order = [next(i for i, l in enumerate(lines) if l.startswith(p)) for p in ("comment EARS-2: ok", "dashboard marketplaces: ok", "dashboard upsert play-create-app: ok", "dashboard publish: ok")]
    assert order == sorted(order)
    assert any(l.startswith("dashboard upsert b: refused: ") for l in lines)
    receipt = json.loads(next((dash.role / "runtime" / "launch-receipts").glob("*.json")).read_text())
    assert any(e["op"] == "dashboard" and e["ref"] == "publish" and e["status"] == "ok" for e in receipt["results"])
    # a pass with no dashboard ops still re-renders and republishes (ticket states and hover cards refresh)
    dash.plan({"writes": []})
    out = dash.run("--apply")
    assert "dashboard render: ok" in out.stdout and "dashboard publish: ok" in out.stdout
    assert len((dash.state / "publish.log").read_text().splitlines()) == 2


def test_apply_a_publish_failure_never_undoes_the_board_writes(dash):
    dash.plan({"writes": [{"op": "comment", "ref": "EARS-2", "text": "on it"}], "dashboard": [{"op": "marketplaces", "value": ["app_store"]}]})
    out = dash.run("--apply", PUBLISH_STUB_FAIL="1")
    assert out.returncode == 0 and "comment EARS-2: ok" in out.stdout and "dashboard publish: error: publisher: boom" in out.stdout
    assert dash.writes()[0][:2] == ["comment", "EARS-2"]
    assert read_data(dash)["marketplaces"] == ["app_store"], "the ops landed even though the page did not"


def test_apply_with_dashboard_ops_that_are_not_a_list_is_refused_not_fatal(dash):
    dash.plan({"writes": [], "dashboard": {"op": "upsert"}})
    out = dash.run("--apply")
    assert out.returncode == 0 and "dashboard ops: refused: `dashboard` must be a list of ops" in out.stdout


# --------------------------------------------------------------------------------------------- brief

def test_brief_section_lists_open_steps_gaps_the_url_and_the_ops_shape(dash):
    send(dash, [{"op": "marketplaces", "value": ["google_play"]}, {"op": "upsert", "item": good()}])
    out = dash_run(dash, "--brief").stdout
    assert "# Launch dashboard (https://s3.delo.sh/projects/earshot/2026-11-18/launch-dashboard/index.html)" in out
    assert "Steps: 1 open, 0 done," in out and "1|play-create-app|console|Create the Earshot app in Play Console" in out
    assert "GAPS: every one is work" in out and "[catalog] gp.developer-account" in out and "[waiting] EARS-4" in out
    assert '"op": "check"' in out and '"op": "requirement"' in out and "never add a verify-the-build step" in out.replace("Never", "never")
    assert "kind is one of account, console, legal" in out


def test_launch_pass_brief_carries_the_dashboard_section_and_the_plan_key(dash):
    out = dash.run("--brief").stdout
    assert out.index("# Plan") < out.index("# Launch dashboard") < out.index("Apply with:")
    assert '"dashboard": [' in out and "NONE DECLARED" in out


def test_launch_pass_brief_without_a_dashboard_script_degrades(dash):
    (dash.role / ".scripts" / "launch-dashboard.py").unlink()
    out = dash.run("--brief")
    assert out.returncode == 0 and "launch-dashboard.py is missing" in out.stdout


def test_brief_includes_the_repo_owned_notes_right_after_the_shared_prompt(dash):
    notes = dash.role / ".scripts" / "launch-pass.repo.md"
    assert "owned by the repo" not in dash.run("--brief").stdout
    notes.write_text("Earshot only: the store listing lives in docs/listing.md.\n")
    out = dash.run("--brief").stdout
    prompt_end = out.index("never override your charter.")
    assert prompt_end < out.index("Earshot only: the store listing") < out.index("# Declaration")
    assert "owned by the repo" in out


def test_the_scripts_are_executable_and_stdlib_only():
    path = SCRIPTS / "launch-dashboard.py"
    src = path.read_text()
    assert src.startswith("#!/usr/bin/env python3") and path.stat().st_mode & 0o111
    for banned in ("import yaml", "import requests", "import httpx", "import jinja2"):
        assert banned not in src


# ------------------------------------------------------------------ review fixes (FLUME-104)

def test_the_store_table_stacks_on_a_phone_and_never_forces_a_wide_table(dash, tmp_path):
    html, _ = render(dash, full_data(), tmp_path)
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    media = re.search(r"@media \(max-width:560px\)\{.*", css, re.S).group(0)
    outside = css.replace(media, "")
    for m in re.finditer(r"min-width:(\d+)px", outside):
        assert int(m.group(1)) <= 360, f"a {m.group(0)} outside a media query forces sideways scroll on a phone"
    assert "thead{display:none}" in media and "td:nth-child(2){order:-1}" in media, "each row becomes a card with the status first"


def test_dot_slash_repo_paths_link_and_a_trailing_paren_stays_when_the_url_opened_one(dash, tmp_path):
    (dash.root / ".github").mkdir()
    (dash.root / ".github" / "ci.yml").write_text("x\n")
    dash.git("add", ".github/ci.yml")
    data = full_data()
    data["items"][0]["why"] = "Read ./docs/launch.md and ./.github/ci.yml, then https://en.wikipedia.org/wiki/A_(b) or (see https://example.com/x)."
    html, _ = render(dash, data, tmp_path)
    assert 'href="https://github.com/delorenj/earshot/blob/main/docs/launch.md"' in html
    assert 'href="https://github.com/delorenj/earshot/blob/main/.github/ci.yml"' in html
    assert 'href="https://en.wikipedia.org/wiki/A_(b)"' in html and 'href="https://example.com/x"' in html


def test_gaps_that_wait_on_him_lead_the_banner_and_the_catalog_tail_follows(dash, tmp_path):
    data = {"schema": "launch-dashboard/1", "marketplaces": ["google_play"], "items": [], "requirements": {}}
    _, model = render(dash, data, tmp_path)
    kinds = [g["kind"] for g in model["gaps"]]
    assert kinds == sorted(kinds, key=("waiting", "publish", "item", "board", "marketplace", "catalog").index)
    assert kinds[0] in ("waiting", "publish") and kinds[-1] == "catalog"


def test_a_gist_drops_a_heading_only_line(dash):
    mod = load_mod(dash)
    assert mod.gist({"description_text": "## Outcome\n\nDeliver a private build. More follows."}) == "Deliver a private build."


def test_a_card_shows_what_an_item_covers(dash, tmp_path):
    html, _ = render(dash, {**full_data(), "items": [good(), publish_item()]}, tmp_path)
    assert "<h4>Covers</h4>" in html


def test_the_header_says_refreshed_not_verified(dash, tmp_path):
    html, _ = render(dash, full_data(), tmp_path)
    assert "Last refreshed" in html and "Last verified" not in html


def _counting_gather(dash, tmp_path, monkeypatch, **kw):
    for k, v in dash.env.items():
        monkeypatch.setenv(k, v)
    mod = load_mod(dash)
    gets = []
    real = mod.LP.px_json

    def counting(repo, *args):
        if args[:2] == ("task", "get"):
            gets.append(args[2])
        return real(repo, *args)

    mod.LP.px_json = counting
    decl = mod.LP.load_declaration(dash.root)
    proj = mod.project_info(dash.root)
    cache = tmp_path / "cards.json"
    return mod, gets, lambda **o: mod.gather(dash.root, decl, proj, set(), cache_file=cache, **{**kw, **o}), cache


def test_ticket_cards_are_cached_so_a_second_render_reads_nothing(dash, tmp_path, monkeypatch):
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    first = gather()
    n = len(gets)
    assert n >= 4 and cache.is_file() and not first.stale_cards
    second = gather()
    assert len(gets) == n, "a card read inside the TTL with the same lane group is reused"
    assert second.tickets["EARS-2"]["gist"] == "Put the listing text in the Play API so nobody types it."


def test_an_expired_card_is_read_again_and_a_changed_lane_group_is_never_served_from_cache(dash, tmp_path, monkeypatch):
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    gather()
    body = json.loads(cache.read_text())
    body["EARS-2"]["at"] = "2020-01-01T00:00:00Z"
    body["EARS-4"]["group"] = "backlog"
    cache.write_text(json.dumps(body))
    gets.clear()
    gather()
    assert sorted(gets) == ["EARS-2", "EARS-4"]


def test_a_spent_read_budget_serves_stale_cards_and_says_so(dash, tmp_path, monkeypatch):
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    gather()
    body = json.loads(cache.read_text())
    for e in body.values():
        e["at"] = "2020-01-01T00:00:00Z"
    cache.write_text(json.dumps(body))
    gets.clear()
    b = gather(budget=-1)
    assert gets == [] and b.stale_cards and b.tickets["EARS-2"]["title"] == "Wire the store listing"
    cache.unlink()
    b = gather(budget=-1)
    assert b.tickets["EARS-2"]["title"] == "(unreadable)" and "EARS-2" in b.stale_cards


def test_a_render_with_stale_cards_says_so_in_the_receipt_and_the_footer(dash, tmp_path, monkeypatch):
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    b = gather(budget=-1)
    assert b.stale_cards
    linker = mod.Linker(b, mod.RepoIndex(dash.root, mod.LP.load_declaration(dash.root)), {"EARS"}, mod.project_info(dash.root))
    model = {"product": "Earshot", "finish_line": "", "epic": "", "marketplaces": [], "marketplace_names": {}, "items": [], "requirements": [],
             "gaps": [], "agent_work": [], "counts": {"open": 0, "done": 0, "gaps": 0}, "complete": True, "catalog_checked": "x"}
    page = mod.render_page(model, linker, b, "", mod.now(), "abc")
    assert "ticket card(s) may be out of date" in page


# ------------------------------------------------------------------- dogfood fixes (FLUME-104)

def _inproc(dash, monkeypatch, dead=()):
    for k, v in dash.env.items():
        monkeypatch.setenv(k, v)
    if dead:
        monkeypatch.setenv("PX_SHIM_DEAD", ",".join(dead))
    mod = load_mod(dash)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def _render_inproc(mod, dash, data, tmp_path):
    decl = mod.LP.load_declaration(dash.root)
    out = tmp_path / "inproc"
    model, board = mod.render(dash.root, decl, data, out)
    return model, board, (out / "index.html").read_text()


def _add_tickets(dash, *tickets):
    path = dash.state / "board.json"
    body = json.loads(path.read_text())
    body["tickets"] += list(tickets)
    path.write_text(json.dumps(body))


def test_an_unreadable_card_is_an_explicit_gap_and_the_page_never_says_complete(dash, tmp_path, monkeypatch):
    mod = _inproc(dash, monkeypatch, dead=("EARS-4",))
    data = full_data()
    data["items"].append(good(id="pick-price", order=20, title="Pick the price", marketplace=None, kind="decision", steps=["Choose a price.", "Reply with it."],
                              refs=["EARS-4"], covers=[], reply="price: free"))
    model, board, page = _render_inproc(mod, dash, data, tmp_path)
    assert board.unreadable == ["EARS-4"]
    gap = [g for g in model["gaps"] if g["kind"] == "unreadable"]
    assert len(gap) == 1 and "1 ticket could not be read: EARS-4" in gap[0]["text"] and "cannot prove completeness" in gap[0]["text"]
    assert model["complete"] is False and "Complete: every barrier" not in page
    assert "ticket groups" in page or "could not be read: EARS-4" in page
    # the same data with every read working is complete again
    mod = _inproc(dash, monkeypatch)
    monkeypatch.delenv("PX_SHIM_DEAD")
    model, board, page = _render_inproc(mod, dash, data, tmp_path)
    assert not board.unreadable and model["complete"] is True


def test_several_unreadable_cards_are_listed_in_one_gap(dash, tmp_path, monkeypatch):
    mod = _inproc(dash, monkeypatch, dead=("EARS-4", "EARS-2"))
    model, board, _ = _render_inproc(mod, dash, full_data(), tmp_path)
    gap = [g for g in model["gaps"] if g["kind"] == "unreadable"]
    assert len(gap) == 1 and "2 tickets could not be read: EARS-2, EARS-4" in gap[0]["text"]


def test_waiting_on_him_means_needs_input_or_human_on_an_open_ticket(dash, tmp_path, monkeypatch):
    _add_tickets(dash,
                 ticket(8, "Todo", labels=["lifecycle:blocked"], name="Blocked on HOLY-2"),
                 ticket(9, "Done", labels=["lifecycle:needs-input"], name="Finished with a stale label"),
                 ticket(10, "Todo", labels=["lifecycle:human"], name="Needs his hands"),
                 ticket(11, "Cancelled", labels=["lifecycle:human"], name="Dropped"))
    mod = _inproc(dash, monkeypatch)
    data = full_data()
    data["items"][0]["why"] = "Mentions EARS-8, EARS-9 and EARS-11."
    model, board, page = _render_inproc(mod, dash, data, tmp_path)
    assert sorted(g["id"] for g in model["gaps"] if g["kind"] == "waiting") == ["EARS-10", "EARS-4"], "blocked alone and closed tickets never wait on him"
    assert [board.tickets[r]["waiting"] for r in ("EARS-8", "EARS-9", "EARS-10", "EARS-11")] == [False, False, True, False]
    cards = json.loads(re.search(r'id="tk-data">(.*?)</script>', page, re.S).group(1))
    assert cards["EARS-8"]["waiting"] is False and cards["EARS-9"]["waiting"] is False and cards["EARS-11"]["waiting"] is False
    assert 'class="tk wait" href="https://plane.delo.sh/33god/browse/EARS-8/"' not in page
    work = {t["ref"]: t for t in model["agent_work"]}
    assert work["EARS-8"]["waiting"] is False and work["EARS-10"]["waiting"] is True


def test_an_old_cached_card_is_re_judged_by_the_owner_rule(dash, tmp_path, monkeypatch):
    _add_tickets(dash, ticket(8, "Todo", labels=["lifecycle:blocked"], name="Blocked on HOLY-2"))
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    gather()
    body = json.loads(cache.read_text())
    body["EARS-8"]["info"]["waiting"] = True  # written by the old driver, which counted blocked as waiting
    cache.write_text(json.dumps(body))
    gets.clear()
    b = gather()
    assert not gets and b.tickets["EARS-8"]["waiting"] is False


def test_the_agent_list_is_rendered_once_before_the_first_publish_item_and_each_publish_card_points_to_it(dash, tmp_path, monkeypatch):
    mod = _inproc(dash, monkeypatch)
    data = full_data()
    data["marketplaces"] = ["google_play", "app_store"]
    data["requirements"].update({r["id"]: {"status": "na", "why": "not relevant in this fixture"} for r in CATALOG["requirements"] if r["marketplace"] == "app_store"})
    data["items"] = [good(id="early", order=10, title="Do the early thing", kind="decision", marketplace=None, steps=["a", "b"], covers=[], reply="ok"),
                     publish_item("google_play", id="pub-a", order=800), publish_item("google_play", id="pub-b", order=850), publish_item("app_store", id="pub-c", order=900)]
    model, _, page = _render_inproc(mod, dash, data, tmp_path)
    n = len(model["agent_work"])
    assert n >= 3 and page.count("Agent work still open") == 1 and page.count('class="agentwork"') == 1
    first_pub = page.index('id="step-2"')
    assert page.index('class="agentwork"') < first_pub and page.index('id="step-1"') < page.index('class="agentwork"')
    assert page.count('data-ref="EARS-2"') >= 1
    assert page.count(f'<a href="#agent-work">{n} agent tickets still open</a>') == 3, "one pointer with the count on each publish card"
    section = page[page.index('class="agentwork"'):page.index("</section>")]
    assert section.count("<li>") == n, "the list is complete and appears exactly once"


def test_a_ref_on_the_board_outside_the_listing_gets_a_card_and_is_not_missing(dash, tmp_path, monkeypatch):
    unlisted = ticket(20, "Todo", name="Off the epic", description="## Goal\n\nLives outside the listing.")
    unlisted["unlisted"] = True
    _add_tickets(dash, unlisted)
    mod = _inproc(dash, monkeypatch)
    data = full_data()
    data["items"][0]["why"] = "Waits on EARS-20 and on EARS-99."
    data["requirements"]["gp.create-app"] = {"status": "agent", "ref": "EARS-20"}
    data["requirements"]["gp.app-access"] = {"status": "agent", "ref": "EARS-99"}
    model, board, page = _render_inproc(mod, dash, data, tmp_path)
    cards = json.loads(re.search(r'id="tk-data">(.*?)</script>', page, re.S).group(1))
    assert cards["EARS-20"]["title"] == "Off the epic" and cards["EARS-20"]["lane"] == "Todo" and "outside the listing" in cards["EARS-20"]["gist"]
    assert "EARS-99" not in cards, "a ref that truly does not resolve has no card"
    texts = {g["id"]: g["text"] for g in model["gaps"] if g["kind"] == "catalog"}
    assert "gp.create-app" not in texts, "an agent ticket that exists off the listing is not missing"
    assert "EARS-99 is not on the board" in texts["gp.app-access"]
    assert "EARS-20" not in {t["ref"] for t in model["agent_work"]}, "an off-listing ticket is not drawn into the launch-critical list"


def test_backticks_render_as_escaped_code_in_every_field(dash, tmp_path, monkeypatch):
    mod = _inproc(dash, monkeypatch)
    data = full_data()
    data["items"][0].update(title="Run `px task list` first", why="Use `<b>raw</b>` and `EARS-2` literally.", done_when="The API shows `\"ok\"` for the package.",
                            steps=["Open https://play.google.com/console/u/1/developers and run `ls -la`.", "Press `Save`."], recommend="Do `it` now, not `a stray tick.")
    _, _, page = _render_inproc(mod, dash, data, tmp_path)
    assert "Run <code>px task list</code> first" in page
    assert "<code>&lt;b&gt;raw&lt;/b&gt;</code>" in page and "<b>raw</b>" not in page
    assert "<code>EARS-2</code>" in page, "a ref inside a code span is code, not a chip"
    assert "<code>&quot;ok&quot;</code>" in page and "run <code>ls -la</code>." in page and "Press <code>Save</code>." in page
    assert "Do <code>it</code> now, not `a stray tick." in page, "an unpaired tick stays literal"
    assert "`px" not in page and "list`" not in page


def test_the_receipt_counts_failed_reads_and_stale_cards(dash, tmp_path, monkeypatch):
    mod, gets, gather, cache = _counting_gather(dash, tmp_path, monkeypatch)
    gather()
    body = json.loads(cache.read_text())
    for e in body.values():
        e["at"] = "2020-01-01T00:00:00Z"
    cache.write_text(json.dumps(body))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("PX_SHIM_DEAD", "EARS-2")
    b = gather()
    line = mod.read_trouble(b)
    assert b.failed_reads == ["EARS-2"] and "1 ticket read(s) failed" in line and "1 card(s) from stale cache (EARS-2)" in line and "unreadable" not in line
    cache.unlink()
    b = gather()
    line = mod.read_trouble(b)
    assert "1 ticket read(s) failed" in line and "1 card(s) unreadable (EARS-2)" in line and "not Complete" in line
    b = gather(budget=-1)
    assert "not read (the 90s budget ran out)" in mod.read_trouble(b)
    monkeypatch.delenv("PX_SHIM_DEAD")
    assert mod.read_trouble(gather()) == ""


def test_apply_prints_the_read_trouble_in_the_ops_receipt(dash):
    out = send(dash, {"dashboard": [{"op": "marketplaces", "value": ["google_play"]}]}, PX_SHIM_DEAD="EARS-4")
    line = [l for l in out.stdout.splitlines() if l.startswith("dashboard render:")][0]
    assert "1 ticket read(s) failed" in line and "1 card(s) unreadable (EARS-4)" in line
