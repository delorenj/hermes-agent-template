#!/usr/bin/env python3
"""Move chat-platform credentials out of a fleet-wide dotenv file safely.

The Hermes fleet shares provider credentials, but polling/socket credentials
belong to one gateway profile.  This helper performs a dry-run by default and
moves only named assignments without ever printing their values.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


DEFAULT_KEYS = ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_ALLOWED_USERS")
ASSIGNMENT = re.compile(r"^(?P<prefix>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _assignments(lines: list[str], keys: set[str]) -> dict[str, tuple[int, str]]:
    found: dict[str, tuple[int, str]] = {}
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line.rstrip("\n"))
        if match and match.group("key") in keys:
            found[match.group("key")] = (index, line)
    return found


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=True) == right.resolve(strict=True)
    except FileNotFoundError:
        return left.resolve() == right.resolve()


def _atomic_write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(central: Path, target: Path, keys: tuple[str, ...], *, apply: bool) -> list[str]:
    if not central.is_file():
        raise ValueError(f"central env file does not exist: {central}")
    if target.is_symlink():
        raise ValueError(f"target env must be a profile-local file, not a symlink: {target}")
    if _same_file(central, target):
        raise ValueError("central and target env resolve to the same file")

    selected = set(keys)
    central_lines = _read(central)
    target_lines = _read(target) if target.exists() else []
    source_values = _assignments(central_lines, selected)
    target_values = _assignments(target_lines, selected)

    missing = [key for key in keys if key not in source_values]
    if missing:
        raise ValueError(f"central env is missing required keys: {', '.join(missing)}")

    conflicts = [
        key
        for key in keys
        if key in target_values and target_values[key][1].rstrip("\n") != source_values[key][1].rstrip("\n")
    ]
    if conflicts:
        raise ValueError(f"target env already has different values for: {', '.join(conflicts)}")

    if not apply:
        return list(keys)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    central_backup = central.with_name(f"{central.name}.bak-{timestamp}-before-chat-scope")
    shutil.copy2(central, central_backup)
    os.chmod(central_backup, stat.S_IRUSR | stat.S_IWUSR)
    if target.exists():
        target_backup = target.with_name(f"{target.name}.bak-{timestamp}-before-chat-scope")
        shutil.copy2(target, target_backup)
        os.chmod(target_backup, stat.S_IRUSR | stat.S_IWUSR)

    remove_indexes = {source_values[key][0] for key in keys}
    next_central = [line for index, line in enumerate(central_lines) if index not in remove_indexes]
    next_target = list(target_lines)
    if next_target and not next_target[-1].endswith("\n"):
        next_target[-1] += "\n"
    if next_target and next_target[-1].strip():
        next_target.append("\n")
    next_target.append("# Profile-scoped chat gateway credentials.\n")
    for key in keys:
        if key not in target_values:
            line = source_values[key][1]
            next_target.append(line if line.endswith("\n") else f"{line}\n")

    _atomic_write(target, next_target)
    _atomic_write(central, next_central)
    return list(keys)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--central-env", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--target-env", type=Path, required=True)
    parser.add_argument("--keys", nargs="+", default=list(DEFAULT_KEYS))
    parser.add_argument("--apply", action="store_true", help="write the migration; default is dry-run")
    return parser


def main() -> int:
    args = _parser().parse_args()
    moved = migrate(args.central_env.expanduser(), args.target_env.expanduser(), tuple(args.keys), apply=args.apply)
    action = "moved" if args.apply else "would move"
    print(f"{action} {', '.join(moved)} from {args.central_env} to {args.target_env}")
    if not args.apply:
        print("dry-run only; rerun with --apply after reviewing the target paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
