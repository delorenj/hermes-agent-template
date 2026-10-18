"""A rendered PM desk must carry its project as an ABSOLUTE ``terminal.cwd``.

2026-10-17: after the 2026-10-10 move to ONE multiplexed gateway (WorkingDirectory=~/.hermes), the
placeholder ``terminal.cwd: "."`` resolved to $HOME for every routed turn, so gateway turns ran in
/home/delorenj: wrong terminal/file-tool cwd, and the Bloodbank artifact sidecar could not attribute
(and so held) every reply. The org chart's ``project_path`` is the source; the renderer writes it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

RENDERER = Path(__file__).parents[1] / "scripts" / "hermes-profile-config.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_profile_config", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry(tmp_path: Path, rows: dict) -> Path:
    path = tmp_path / "agents-registry.yaml"
    path.write_text(yaml.safe_dump({"agents": rows}), encoding="utf-8")
    return path


def _proj(tmp_path: Path, name: str = "proj") -> Path:
    p = tmp_path / name
    p.mkdir()
    return p


def test_placeholder_becomes_the_registry_project_path(tmp_path):
    proj = _proj(tmp_path)
    reg = _registry(tmp_path, {"x-pm": {"profile_name": "x-pm", "project_path": str(proj)}})
    for placeholder in (".", "auto", "cwd", "", None):
        out = _load().pin_terminal_cwd({"terminal": {"backend": "local", "cwd": placeholder}}, "x-pm", reg)
        assert out["terminal"] == {"backend": "local", "cwd": str(proj)}


def test_missing_terminal_block_is_created(tmp_path):
    proj = _proj(tmp_path)
    reg = _registry(tmp_path, {"x-pm": {"profile_name": "x-pm", "project_path": str(proj)}})
    assert _load().pin_terminal_cwd({}, "x-pm", reg)["terminal"] == {"cwd": str(proj)}


def test_explicit_operator_cwd_wins(tmp_path):
    proj = _proj(tmp_path)
    reg = _registry(tmp_path, {"x-pm": {"profile_name": "x-pm", "project_path": str(proj)}})
    cfg = {"terminal": {"cwd": "/srv/elsewhere"}}
    assert _load().pin_terminal_cwd(cfg, "x-pm", reg)["terminal"]["cwd"] == "/srv/elsewhere"


def test_profile_without_project_path_keeps_the_placeholder(tmp_path):
    reg = _registry(tmp_path, {"spec": {"profile_name": "spec"}, "x-pm": {"profile_name": "x-pm"}})
    for name in ("spec", "x-pm", "unlisted"):
        assert _load().pin_terminal_cwd({"terminal": {"cwd": "."}}, name, reg)["terminal"]["cwd"] == "."


def test_stale_or_relative_project_path_is_ignored(tmp_path):
    reg = _registry(tmp_path, {
        "gone": {"profile_name": "gone", "project_path": str(tmp_path / "does-not-exist")},
        "rel": {"profile_name": "rel", "project_path": "relative/dir"},
    })
    for name in ("gone", "rel"):
        assert _load().pin_terminal_cwd({"terminal": {"cwd": "."}}, name, reg)["terminal"]["cwd"] == "."


def test_row_is_matched_by_profile_name_not_only_key(tmp_path):
    proj = _proj(tmp_path)
    reg = _registry(tmp_path, {"agent-id": {"profile_name": "desk-name", "project_path": str(proj)}})
    assert _load().pin_terminal_cwd({"terminal": {"cwd": "."}}, "desk-name", reg)["terminal"]["cwd"] == str(proj)


def test_unreadable_registry_changes_nothing(tmp_path):
    cfg = {"terminal": {"cwd": "."}}
    assert _load().pin_terminal_cwd(cfg, "x-pm", tmp_path / "missing.yaml")["terminal"]["cwd"] == "."
    bad = tmp_path / "bad.yaml"
    bad.write_text("agents: [unclosed", encoding="utf-8")
    assert _load().pin_terminal_cwd({"terminal": {"cwd": "."}}, "x-pm", bad)["terminal"]["cwd"] == "."


def test_render_check_absorb_roundtrip_leaves_no_cwd_in_the_delta(tmp_path, monkeypatch):
    """render writes the pin, check agrees, absorb does NOT fold the generated pin into the delta."""
    proj = _proj(tmp_path)
    home = tmp_path / "hh"
    (home / "profiles" / "x-pm").mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": {"backend": "local", "cwd": "."}}))
    (home / "profiles" / "x-pm" / "config.delta.yaml").write_text("{}\n")
    (home / "agents-registry.yaml").write_text(
        yaml.safe_dump({"agents": {"x-pm": {"profile_name": "x-pm", "project_path": str(proj)}}}))
    monkeypatch.setenv("HERMES_FLEET_HOME", str(home))
    mod = _load()
    base = mod.load_yaml(mod.BASE)
    pdir = home / "profiles" / "x-pm"
    _delta, _cur, expected, _ = mod.profile_state(pdir, base)
    assert expected["terminal"]["cwd"] == str(proj)
    (pdir / "config.yaml").write_text(yaml.safe_dump(expected))
    assert mod.profile_state(pdir, base)[1] == mod.profile_state(pdir, base)[2]
    # absorb: current == pinned effective base -> delta stays empty
    effective_base = mod.pin_terminal_cwd(mod.deep_merge(base, {}), "x-pm")
    assert mod.minimal_delta(expected, effective_base) == {}
