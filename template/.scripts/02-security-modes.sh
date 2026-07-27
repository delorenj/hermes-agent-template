#!/usr/bin/env bash
# Normalize repository-origin delivery inputs after Copier renders under a
# permissive umask. This task performs no network or fleet mutation.
set -euo pipefail

ROLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
REPO_ROOT="${1:-}"
if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(git -C "$ROLE_DIR" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$ROLE_DIR")"
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"

umask 022
python3 - "$ROLE_DIR" "$REPO_ROOT" <<'PY'
import os
import pathlib
import stat
import sys

role_dir = pathlib.Path(sys.argv[1])
repo_root = pathlib.Path(sys.argv[2])
allowed_owners = {0, os.geteuid()}


def trusted(path: pathlib.Path, kind: str) -> os.stat_result:
    metadata = path.lstat()
    valid_kind = (
        stat.S_ISDIR(metadata.st_mode)
        if kind == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    if not valid_kind or metadata.st_uid not in allowed_owners:
        raise SystemExit("security mode normalization rejected an untrusted path")
    return metadata


def set_directory(path: pathlib.Path, mode: int) -> None:
    trusted(path, "directory")
    path.chmod(mode)


def set_file(path: pathlib.Path, mode: int) -> None:
    trusted(path, "file")
    path.chmod(mode)


set_directory(repo_root, 0o755)
set_directory(role_dir, 0o755)
scripts = role_dir / ".scripts"
set_directory(scripts, 0o755)
for current, directories, files in os.walk(scripts, followlinks=False):
    current_path = pathlib.Path(current)
    set_directory(current_path, 0o755)
    for name in directories:
        child = current_path / name
        if child.is_symlink():
            raise SystemExit("security mode normalization rejected a symlink")
        set_directory(child, 0o755)
    for name in files:
        child = current_path / name
        if child.is_symlink():
            raise SystemExit("security mode normalization rejected a symlink")
        relative = child.relative_to(scripts)
        executable = (
            child.suffix == ".sh"
            or relative == pathlib.Path("sentinel/bin/run-retro.py")
            or relative == pathlib.Path("momo-wip-lock.py")
        )
        set_file(child, 0o755 if executable else 0o644)

hermes = role_dir / "hermes"
if hermes.exists():
    set_file(hermes, 0o755)
for name in ("role.yaml", "SOUL.md"):
    path = role_dir / name
    if path.exists():
        set_file(path, 0o644)

project = repo_root / ".project.json"
if project.exists():
    set_file(project, 0o644)

artifacts = repo_root / "_bmad-output" / "implementation-artifacts"
retro = artifacts / "run-retros"
for path in (repo_root / "_bmad-output", artifacts):
    if path.exists():
        set_directory(path, 0o755)
if retro.exists():
    for current, directories, files in os.walk(retro, followlinks=False):
        current_path = pathlib.Path(current)
        set_directory(current_path, 0o700)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                raise SystemExit("security mode normalization rejected a symlink")
            set_directory(child, 0o700)
        for name in files:
            child = current_path / name
            if child.is_symlink():
                raise SystemExit("security mode normalization rejected a symlink")
            set_file(child, 0o600)
PY
