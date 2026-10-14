"""A rendered profile must never run `codegraph serve --mcp` without a repo --path.

2026-10-11: an unpinned server run from cwd ~/.hermes built a 49.7 GiB index of the vendored installs/ tree.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

RENDERER = Path(__file__).parents[1] / "scripts" / "hermes-profile-config.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_profile_config", RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(repo: Path | None, args=("serve", "--mcp")) -> dict:
    return {
        "mcp_servers": {"codegraph": {"command": "codegraph", "args": list(args)}, "plane": {"command": "plane"}},
        "skills": {"trusted_project_dirs": [str(repo)] if repo else []},
    }


def test_unpinned_server_gets_the_desks_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = _load().pin_codegraph_root(_config(repo))
    assert out["mcp_servers"]["codegraph"]["args"] == ["serve", "--mcp", "--path", str(repo)]
    assert "plane" in out["mcp_servers"]


def test_explicit_path_is_left_alone(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    args = ["serve", "--mcp", "--path", "/elsewhere"]
    assert _load().pin_codegraph_root(_config(repo, args))["mcp_servers"]["codegraph"]["args"] == args


def test_profile_without_a_git_repo_gets_no_codegraph(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    for root in (None, not_a_repo):
        out = _load().pin_codegraph_root(_config(root))
        assert "codegraph" not in out["mcp_servers"]
        assert "plane" in out["mcp_servers"]


def test_config_without_codegraph_is_untouched():
    cfg = {"mcp_servers": None, "skills": {}}
    assert _load().pin_codegraph_root(cfg) == {"mcp_servers": None, "skills": {}}
