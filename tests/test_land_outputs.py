"""land-outputs.sh: the PM pass commits and pushes its own evidence (no dangling work, 2026-11-15).

By 2026-11-15 gruvato's main checkout held 338 evidence files the hourly sentinel pass had written
and nobody committed. heartbeat.sh now runs land-outputs.sh after every full pass. These tests pin
what it may and may not touch: only modified/untracked files under the land paths, only on the
default branch, never what another agent staged or wrote elsewhere, and it never fails the pass.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "template" / ".scripts"
EVIDENCE = "_bmad-output/implementation-artifacts/issue-evidence"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=check, capture_output=True,
                          text=True, env=env).stdout.rstrip("\n")


def make_repo(tmp: Path, role_yaml: str = 'agent_id: "demo-pm"\n') -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "config", "core.hooksPath", "/dev/null")
    git(repo, "checkout", "-q", "-b", "main")
    role = repo / "agents" / "hermes" / "pm"
    (role / ".scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPTS / "land-outputs.sh", role / ".scripts" / "land-outputs.sh")
    shutil.copy(SCRIPTS / "lib" / "role-yaml.py", role / ".scripts" / "lib" / "role-yaml.py")
    (role / "role.yaml").write_text(role_yaml)
    (repo / EVIDENCE).mkdir(parents=True)
    (repo / EVIDENCE / "GRUV-1.md").write_text("start\n")
    (repo / "src.txt").write_text("code\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return repo, origin


def age(path: Path, seconds: int = 120) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def run(repo: Path, **env: str) -> subprocess.CompletedProcess:
    role = repo / "agents" / "hermes" / "pm"
    return subprocess.run(["bash", str(role / ".scripts" / "land-outputs.sh")], cwd=repo,
                          capture_output=True, text=True,
                          env={**os.environ, "ROLE_DIR": str(role), "REPO_ROOT": str(repo), **env})


def test_lands_only_evidence_and_pushes(tmp_path: Path) -> None:
    repo, origin = make_repo(tmp_path)
    (repo / EVIDENCE / "GRUV-1.md").write_text("start\nmore\n")
    (repo / EVIDENCE / "sentinel-pass-20261115T140000Z.md").write_text("pass\n")
    (repo / "src.txt").write_text("someone else's edit\n")
    (repo / "stray.txt").write_text("not ours\n")
    for f in (repo / EVIDENCE).iterdir():
        age(f)
    res = run(repo)
    assert res.returncode == 0, res.stderr
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(committed) == [f"{EVIDENCE}/GRUV-1.md", f"{EVIDENCE}/sentinel-pass-20261115T140000Z.md"]
    assert git(repo, "rev-parse", "HEAD") == git(origin, "rev-parse", "main")
    status = git(repo, "status", "--porcelain")
    assert " M src.txt" in status and "?? stray.txt" in status


def test_never_commits_what_another_agent_staged(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / "src.txt").write_text("staged by a worker\n")
    git(repo, "add", "src.txt")
    (repo / EVIDENCE / "GRUV-2.md").write_text("x\n")
    age(repo / EVIDENCE / "GRUV-2.md")
    assert run(repo).returncode == 0
    assert git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == [f"{EVIDENCE}/GRUV-2.md"]
    assert "M  src.txt" in git(repo, "status", "--porcelain")


def test_skips_files_still_being_written(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / EVIDENCE / "fresh.md").write_text("mid-write\n")
    head = git(repo, "rev-parse", "HEAD")
    assert run(repo).returncode == 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert run(repo, LAND_QUIET_SECONDS="0").returncode == 0
    assert git(repo, "rev-parse", "HEAD") != head


def test_does_not_land_on_a_side_branch_or_detached_head(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "side")
    (repo / EVIDENCE / "GRUV-3.md").write_text("x\n")
    age(repo / EVIDENCE / "GRUV-3.md")
    head = git(repo, "rev-parse", "HEAD")
    res = run(repo)
    assert res.returncode == 0 and "not landing" in res.stdout
    assert git(repo, "rev-parse", "HEAD") == head
    git(repo, "checkout", "-q", "--detach")
    res = run(repo)
    assert res.returncode == 0 and "detached" in res.stdout


def test_merges_when_origin_moved_and_keeps_other_dirty_files(tmp_path: Path) -> None:
    repo, origin = make_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    git(other, "config", "core.hooksPath", "/dev/null")
    (other / "upstream.txt").write_text("from another agent\n")
    git(other, "add", "upstream.txt")
    git(other, "commit", "-q", "-m", "upstream")
    git(other, "push", "-q", "origin", "main")
    (repo / "src.txt").write_text("local dirty\n")
    (repo / EVIDENCE / "GRUV-4.md").write_text("x\n")
    age(repo / EVIDENCE / "GRUV-4.md")
    assert run(repo).returncode == 0
    assert git(repo, "rev-parse", "HEAD") == git(origin, "rev-parse", "main")
    assert (repo / "upstream.txt").exists()
    assert " M src.txt" in git(repo, "status", "--porcelain")


def test_role_yaml_land_paths_override(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path, 'agent_id: "demo-pm"\nland:\n  paths: "reports"\n')
    (repo / "reports").mkdir()
    (repo / "reports" / "r.md").write_text("x\n")
    (repo / EVIDENCE / "GRUV-5.md").write_text("x\n")
    age(repo / "reports" / "r.md")
    age(repo / EVIDENCE / "GRUV-5.md")
    assert run(repo).returncode == 0
    assert git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["reports/r.md"]


def test_disabled_and_failure_paths_exit_zero(tmp_path: Path) -> None:
    repo, _ = make_repo(tmp_path)
    (repo / EVIDENCE / "GRUV-6.md").write_text("x\n")
    age(repo / EVIDENCE / "GRUV-6.md")
    head = git(repo, "rev-parse", "HEAD")
    assert run(repo, LAND_OUTPUTS="0").returncode == 0
    assert git(repo, "rev-parse", "HEAD") == head
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    res = run(repo)
    assert res.returncode == 0
    assert git(repo, "rev-parse", "HEAD") != head  # committed locally; push retried next pass


def test_heartbeat_runs_land_outputs_after_the_pass() -> None:
    text = (SCRIPTS / "heartbeat.sh").read_text()
    land = text.index('LAND_SCRIPT="$ROLE_DIR/.scripts/land-outputs.sh"')
    assert text.index("runner_exit=") < land < text.rindex('exit "$runner_exit"')
    assert '|| true' in text[land:text.rindex('exit "$runner_exit"')]
