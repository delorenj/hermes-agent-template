"""Exercise the real step 10 with a repo-less owning desk, never ambient CWD."""
import json
import os
import shutil

import pytest
import yaml

from test_skillex_profile import installed, fixture, provision, run, snapshot, succeeded


def desk_fixture(fixture):
    desk = fixture["root"] / "workforce/analyst"
    desk.parent.mkdir()
    shutil.move(str(fixture["role"]), desk)
    (desk / ".agents").mkdir()
    shutil.copy2(fixture["manifest"], desk / ".agents/skills.json")
    role_path = desk / "role.yaml"
    role = yaml.safe_load(role_path.read_text())
    role.pop("repo")
    role.pop("plane")
    role.pop("runtime")
    role["identity"] = {"name": "analyst", "write_bank": "agent-analyst", "recall_banks": ["infra"]}
    role_path.write_text(yaml.safe_dump(role))
    fixture.update(role=desk, scripts=desk / ".scripts")
    fixture["env"].pop("PJANGLER_PROJECT_ROOT")
    fixture["env"]["HERMES_DESK_ROOT"] = str(desk)
    # Strong adversary: the fleet base enables project discovery and ambient
    # CWD has its own selection. Desk mode must disable discovery, not inherit.
    (fixture["home"] / ".hermes/config.yaml").write_text(
        "skills:\n  external_dirs: [legacy]\n  project_discovery: true\n  inherit_global: true\n")
    return desk


@pytest.mark.parametrize("existing", [False, True])
def test_step10_accepts_desk_without_project_and_refreshes_without_rebinding(fixture, existing):
    desk = desk_fixture(fixture)
    if not existing:
        fixture["profile"].rmdir()
    unrelated = snapshot(fixture["cwd"]), snapshot(fixture["project"])
    result = succeeded(provision(fixture))
    assert "desk-only loadout" in result.stderr
    profile = fixture["profile"]
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["skills"]["external_dirs"] == []
    assert config["skills"]["project_discovery"] is False
    assert config["skills"]["inherit_global"] is True
    assert not config["skills"].get("trusted_project_dirs")
    assert (profile / "SOUL.md").read_text() == "Profile-specific identity.\n"
    assert json.loads((profile / "hindsight/config.json").read_text())["bank_id"] == "agent-analyst"
    assert {p.name for p in (profile / "skills").iterdir()} == {"alpha", "beta", "gamma"}
    assert (profile / "skills/beta").resolve() == fixture["registry"] / "all-skills/beta"
    assert not (profile / ".agents").exists()
    assert not (desk / ".git").exists() and not (desk / ".project.json").exists()
    assert not (fixture["home"] / ".hermes/agents-registry.yaml").exists()
    (profile / "state.db").write_bytes(b"runtime-owned")
    before = snapshot(profile)
    mtimes = {str(p): p.stat().st_mtime_ns for p in profile.rglob("*") if p.is_file()}
    succeeded(provision(fixture))
    assert snapshot(profile) == before
    assert {str(p): p.stat().st_mtime_ns for p in profile.rglob("*") if p.is_file()} == mtimes
    assert (snapshot(fixture["cwd"]), snapshot(fixture["project"])) == unrelated
    # Definition-independent loadout edits use the same desk selection on rerun.
    (desk / ".agents/skills.json").write_text('{"skills":[{"name":"delta"}]}\n')
    succeeded(provision(fixture))
    assert {p.name for p in (profile / "skills").iterdir()} == {"alpha", "gamma", "delta"}
    assert json.loads((profile / "hindsight/config.json").read_text())["bank_id"] == "agent-analyst"


@pytest.mark.parametrize("kind", ["foreign-root", "symlink", "both-roots", "missing-selection"])
def test_invalid_desk_refuses_before_profile_mutation(fixture, kind):
    desk = desk_fixture(fixture)
    fixture["profile"].rmdir()
    if kind == "foreign-root":
        fixture["env"]["HERMES_DESK_ROOT"] = str(fixture["cwd"])
    elif kind == "symlink":
        link = fixture["root"] / "desk-link"
        link.symlink_to(desk)
        fixture["env"]["HERMES_DESK_ROOT"] = str(link)
    elif kind == "both-roots":
        fixture["env"]["PJANGLER_PROJECT_ROOT"] = str(fixture["project"])
    else:
        (desk / ".agents/skills.json").unlink()
    before = snapshot(fixture["home"])
    result = provision(fixture)
    assert result.returncode != 0
    assert "cannot resolve the owning desk" in result.stderr or "selection" in result.stderr
    assert snapshot(fixture["home"]) == before
    assert not (fixture["root"] / "hermes.log").exists()


def unsafe_input(fixture, source, kind):
    if kind == "missing":
        source.unlink()
    elif kind == "fifo":
        source.unlink()
        os.mkfifo(source)
    elif kind == "directory":
        source.unlink()
        source.mkdir()
    elif kind in ("symlink", "dangling"):
        foreign = fixture["root"] / "foreign-input"
        if kind == "symlink":
            foreign.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(foreign)
    elif kind == "ancestor":
        foreign = fixture["root"] / "foreign-selection"
        shutil.move(str(source.parent), foreign)
        source.parent.symlink_to(foreign, target_is_directory=True)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("stage", ["new", "partial", "strict"])
@pytest.mark.parametrize("source,kind", [
    ("SOUL.md", "missing"),
    ("SOUL.md", "symlink"),
    ("SOUL.md", "dangling"),
    ("SOUL.md", "directory"),
    ("SOUL.md", "fifo"),
    (".agents/skills.json", "missing"),
    (".agents/skills.json", "symlink"),
    (".agents/skills.json", "dangling"),
    (".agents/skills.json", "directory"),
    (".agents/skills.json", "ancestor"),
])
def test_unsafe_desk_authored_input_refuses_without_profile_effects(fixture, stage, source, kind):
    desk = desk_fixture(fixture)
    if stage == "new":
        fixture["profile"].rmdir()
    elif stage == "strict":
        succeeded(provision(fixture))
    if stage != "new":
        (fixture["profile"] / "state.db").write_bytes(b"runtime-owned")
        (fixture["profile"] / "gateway.pid").write_text("4242\n")
    unsafe_input(fixture, desk / source, kind)
    before = snapshot(fixture["home"]), snapshot(fixture["registry"])
    result = provision(fixture)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "owning desk" in result.stderr or "skills.json" in result.stdout + result.stderr
    assert (snapshot(fixture["home"]), snapshot(fixture["registry"])) == before
    assert not (fixture["root"] / "hermes.log").exists()


def bind_selection(fixture, selection):
    (selection / ".agents").mkdir(parents=True)
    (selection / ".agents/skills.json").write_text('{"skills":[{"name":"delta"}]}\n')
    succeeded(run([
        fixture["env"]["SKILLEX_BIN"], "profile", "sync", "fixture-pm",
        "--project", str(selection), "--skillex-only",
    ], cwd=fixture["cwd"], env=fixture["env"]))


@pytest.mark.parametrize("binding", ["persisted", "receipt"])
def test_effective_persisted_selection_precedes_unsafe_unused_desk_selection(fixture, binding):
    desk = desk_fixture(fixture)
    succeeded(provision(fixture))
    selection = (fixture["profile"] / ".skillex-selection" if binding == "persisted"
                 else fixture["root"] / "recorded-selection")
    bind_selection(fixture, selection)
    unsafe_input(fixture, desk / ".agents/skills.json", "symlink")
    before = snapshot(fixture["home"]), snapshot(selection)
    succeeded(provision(fixture))
    assert (snapshot(fixture["home"]), snapshot(selection)) == before
    skills = fixture["profile"] / "skills"
    assert {path.name for path in skills.iterdir()} == {"alpha", "gamma", "delta"}
    for name in ("alpha", "gamma", "delta"):
        assert (skills / name).is_symlink()
        assert (skills / name).resolve() == fixture["registry"] / "all-skills" / name


@pytest.mark.parametrize("binding", ["persisted", "receipt"])
@pytest.mark.parametrize("kind", ["missing", "symlink", "dangling", "directory", "ancestor"])
def test_unsafe_effective_persisted_selection_refuses_without_profile_effects(fixture, binding, kind):
    desk = desk_fixture(fixture)
    succeeded(provision(fixture))
    selection = (fixture["profile"] / ".skillex-selection" if binding == "persisted"
                 else fixture["root"] / "recorded-selection")
    bind_selection(fixture, selection)
    # The unused initial desk selection remains valid; it must not rescue the
    # unsafe effective source by silently rebinding the employee's loadout.
    assert (desk / ".agents/skills.json").is_file()
    unsafe_input(fixture, selection / ".agents/skills.json", kind)
    (fixture["profile"] / "state.db").write_bytes(b"runtime-owned")
    before = snapshot(fixture["home"]), snapshot(fixture["root"] / "foreign-selection")
    result = provision(fixture)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (snapshot(fixture["home"]), snapshot(fixture["root"] / "foreign-selection")) == before
    assert not (fixture["root"] / "hermes.log").exists()


@pytest.mark.parametrize("stage", ["new", "partial", "strict"])
def test_invalid_desk_identity_refuses_before_profile_mutation(fixture, stage):
    desk = desk_fixture(fixture)
    if stage == "new":
        fixture["profile"].rmdir()
    elif stage == "strict":
        succeeded(provision(fixture))
    role_path = desk / "role.yaml"
    role = yaml.safe_load(role_path.read_text())
    role["identity"] = {"name": "fixture-pm", "write_bank": "agent-fixture-pm"}
    role_path.write_text(yaml.safe_dump(role))
    before = snapshot(fixture["home"])
    result = provision(fixture)
    assert result.returncode != 0
    assert "is a post id" in result.stderr
    assert snapshot(fixture["home"]) == before
    assert not (fixture["root"] / "hermes.log").exists()
