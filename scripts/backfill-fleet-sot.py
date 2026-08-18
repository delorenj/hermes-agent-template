#!/usr/bin/env python3
"""Plan and atomically apply the Hermes fleet source-of-truth backfill.

This helper is intentionally outside the Copier/MCP execution graph.  The
shell maintenance entry point invokes it only after loading fleet.env through
the canonical data-only parser.  It performs a complete read-only plan before
creating any destination directory or temporary output, then commits the
planned local files as a rollback-capable batch.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from types import ModuleType
from typing import Any

import yaml


AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ROLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class BackfillError(RuntimeError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise BackfillError("registry contains a duplicate mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    content: bytes
    mode: int
    uid: int
    gid: int
    device: int
    inode: int


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: bytes
    mode: int
    original: FileSnapshot | None


@dataclass
class PreparedFile:
    plan: PlannedFile
    temporary: Path
    installed_identity: tuple[int, int] | None = None


def absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def contained_by(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def require_real_directory(path: Path, label: str) -> os.stat_result:
    metadata = lstat_optional(path)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise BackfillError(f"{label} must be a real directory")
    return metadata


def read_regular_file(path: Path, label: str) -> FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BackfillError(f"{label} must be a regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackfillError(f"{label} must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise BackfillError(f"{label} changed while it was read")
    return FileSnapshot(
        path=path,
        content=b"".join(chunks),
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def load_parser(path: Path) -> ModuleType:
    snapshot = read_regular_file(path, "canonical fleet parser")
    if path.is_symlink():
        raise BackfillError("canonical fleet parser must not be a symlink")
    spec = importlib.util.spec_from_file_location("pjangler_fleet_env_parser", snapshot.path)
    if spec is None or spec.loader is None:
        raise BackfillError("canonical fleet parser cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("INITIAL_DOCUMENT", "parse_document", "read_regular_document", "render_upsert", "_exchange_paths"):
        if not hasattr(module, name):
            raise BackfillError("canonical fleet parser has an unsupported contract")
    return module


class BatchPlan:
    def __init__(self, exchange_paths: Any) -> None:
        self.exchange_paths = exchange_paths
        self.directories: dict[Path, int] = {}
        self.files: dict[Path, PlannedFile] = {}

    def plan_directory(self, path: Path, mode: int = 0o755) -> None:
        path = absolute(path)
        missing: list[Path] = []
        cursor = path
        while True:
            metadata = lstat_optional(cursor)
            if metadata is not None:
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise BackfillError(f"destination directory is unsafe: {cursor}")
                break
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise BackfillError("destination has no existing directory ancestor")
            cursor = parent
        for directory in reversed(missing):
            self.directories.setdefault(directory, mode)

    def plan_file(
        self,
        path: Path,
        content: bytes,
        *,
        mode: int | None = None,
        parent_mode: int = 0o755,
    ) -> None:
        path = absolute(path)
        self.plan_directory(path.parent, parent_mode)
        metadata = lstat_optional(path)
        original: FileSnapshot | None
        if metadata is None:
            original = None
            selected_mode = 0o600 if mode is None else mode
        else:
            original = read_regular_file(path, f"destination {path.name}")
            selected_mode = original.mode if mode is None else mode
        selected_mode &= 0o777
        planned = PlannedFile(path, content, selected_mode, original)
        previous = self.files.get(path)
        if previous is not None and previous != planned:
            raise BackfillError(f"conflicting operations target {path}")
        if original is not None and original.content == content and original.mode == selected_mode:
            return
        self.files[path] = planned

    @staticmethod
    def _write_prepared(plan: PlannedFile) -> PreparedFile:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{plan.path.name}.pjangler-backfill-",
            dir=plan.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, plan.mode)
            if plan.original is not None:
                try:
                    os.fchown(descriptor, plan.original.uid, plan.original.gid)
                except PermissionError:
                    pass
            view = memoryview(plan.content)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        os.close(descriptor)
        return PreparedFile(plan, temporary)

    @staticmethod
    def _same_snapshot(expected: FileSnapshot, actual: FileSnapshot) -> bool:
        return (
            expected.content == actual.content
            and expected.mode == actual.mode
            and expected.uid == actual.uid
            and expected.gid == actual.gid
            and expected.device == actual.device
            and expected.inode == actual.inode
        )

    def _commit_one(self, prepared: PreparedFile) -> None:
        plan = prepared.plan
        if plan.original is None:
            if lstat_optional(plan.path) is not None:
                raise BackfillError("destination appeared after backfill preflight")
            os.link(prepared.temporary, plan.path, follow_symlinks=False)
            installed = plan.path.lstat()
            prepared.installed_identity = (installed.st_dev, installed.st_ino)
            prepared.temporary.unlink()
            return

        current = read_regular_file(plan.path, f"destination {plan.path.name}")
        if not self._same_snapshot(plan.original, current):
            raise BackfillError("destination changed after backfill preflight")
        self.exchange_paths(prepared.temporary, plan.path)
        displaced = read_regular_file(prepared.temporary, "displaced backfill destination")
        if not self._same_snapshot(plan.original, displaced):
            self.exchange_paths(prepared.temporary, plan.path)
            raise BackfillError("destination changed at backfill commit boundary")
        installed = plan.path.lstat()
        prepared.installed_identity = (installed.st_dev, installed.st_ino)

    def _rollback(self, committed: list[PreparedFile]) -> list[Path]:
        preserved: list[Path] = []
        for prepared in reversed(committed):
            plan = prepared.plan
            try:
                current = plan.path.lstat()
                if prepared.installed_identity != (current.st_dev, current.st_ino):
                    preserved.append(prepared.temporary)
                    continue
                if plan.original is None:
                    plan.path.unlink()
                else:
                    self.exchange_paths(prepared.temporary, plan.path)
                    prepared.temporary.unlink()
            except OSError:
                if prepared.temporary.exists():
                    preserved.append(prepared.temporary)
        return preserved

    @staticmethod
    def _sync_directories(paths: set[Path]) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        for path in sorted(paths):
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def apply(self) -> None:
        created_directories: list[Path] = []
        prepared: list[PreparedFile] = []
        committed: list[PreparedFile] = []
        try:
            for path, mode in sorted(self.directories.items(), key=lambda item: len(item[0].parts)):
                if lstat_optional(path) is not None:
                    require_real_directory(path, "planned destination directory")
                    continue
                path.mkdir(mode=mode)
                created_directories.append(path)
            for plan in self.files.values():
                prepared.append(self._write_prepared(plan))
            for item in prepared:
                self._commit_one(item)
                committed.append(item)
            self._sync_directories({plan.path.parent for plan in self.files.values()})
        except BaseException as error:
            preserved = self._rollback(committed)
            for item in prepared:
                if item in committed or item.temporary in preserved:
                    continue
                try:
                    item.temporary.unlink()
                except FileNotFoundError:
                    pass
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if preserved:
                raise BackfillError("backfill apply failed; rollback data was preserved") from error
            raise
        else:
            for item in committed:
                if item.plan.original is not None:
                    item.temporary.unlink()


def source_tree(plan: BatchPlan, source: Path, destination: Path) -> int:
    require_real_directory(source, "runtime scaffold source")
    count = 0
    plan.plan_directory(destination)
    for entry in sorted(source.rglob("*")):
        relative = entry.relative_to(source)
        metadata = entry.lstat()
        target = destination / relative
        if stat.S_ISLNK(metadata.st_mode):
            raise BackfillError("runtime scaffold source contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            plan.plan_directory(target, stat.S_IMODE(metadata.st_mode))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackfillError("runtime scaffold source contains a non-regular file")
        snapshot = read_regular_file(entry, "runtime scaffold source file")
        plan.plan_file(target, snapshot.content, mode=snapshot.mode)
        count += 1
    return count


def patch_unit(original: bytes, oauth_file: str, codex_home: str) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BackfillError("systemd unit must be valid UTF-8") from error
    lines = text.splitlines()
    home_lines = [line for line in lines if line.startswith("Environment=HERMES_HOME=")]
    if len(home_lines) != 1:
        raise BackfillError("systemd unit must contain exactly one HERMES_HOME environment line")
    rendered: list[str] = []
    for line in lines:
        if line.startswith("Environment=HERMES_OAUTH_FILE=") or line.startswith("Environment=CODEX_HOME="):
            continue
        rendered.append(line)
        if line.startswith("Environment=HERMES_HOME="):
            rendered.append(f"Environment=HERMES_OAUTH_FILE={oauth_file}")
            rendered.append(f"Environment=CODEX_HOME={codex_home}")
    suffix = "\n" if text.endswith("\n") else ""
    return ("\n".join(rendered) + suffix).encode("utf-8")


def load_registry(path: Path) -> tuple[FileSnapshot, dict[str, Any]]:
    snapshot = read_regular_file(path, "fleet registry")
    try:
        decoded = snapshot.content.decode("utf-8")
        loaded = yaml.load(decoded, Loader=UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise BackfillError("fleet registry is invalid") from error
    if not isinstance(loaded, dict):
        raise BackfillError("fleet registry must contain a mapping")
    agents = loaded.get("agents")
    if not isinstance(agents, dict):
        raise BackfillError("fleet registry agents must contain a mapping")
    return snapshot, loaded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--fleet-env", required=True)
    parser.add_argument("--fleet-bin", required=True)
    parser.add_argument("--fleet-repo", required=True)
    parser.add_argument("--oauth-file", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--systemd-dir", required=True)
    parser.add_argument("--scaffold-source", required=True)
    parser.add_argument("--fleet-library-source", required=True)
    parser.add_argument("--fleet-parser-source", required=True)
    parser.add_argument("--role-library-source", required=True)
    parser.add_argument("--heartbeat-source", required=True)
    parser.add_argument("--wrapper-template", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    parser_path = absolute(args.fleet_parser_source)
    fleet_parser = load_parser(parser_path)
    registry_path = absolute(args.registry)
    fleet_path = absolute(args.fleet_env)
    systemd_dir = absolute(args.systemd_dir)
    scaffold_source = absolute(args.scaffold_source)
    fleet_library_source = read_regular_file(
        absolute(args.fleet_library_source), "canonical fleet loader"
    )
    role_library_source = read_regular_file(
        absolute(args.role_library_source), "canonical role library"
    )
    heartbeat_source = read_regular_file(
        absolute(args.heartbeat_source), "canonical heartbeat"
    )
    wrapper_source = read_regular_file(
        absolute(args.wrapper_template), "canonical Hermes launcher"
    )
    try:
        wrapper_template = wrapper_source.content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BackfillError("canonical Hermes launcher must be valid UTF-8") from error
    if "{{ agent_id }}" not in wrapper_template or "load_fleet_environment" not in wrapper_template:
        raise BackfillError("canonical Hermes launcher has an unsupported contract")

    registry_snapshot, registry = load_registry(registry_path)
    updated_registry = copy.deepcopy(registry)
    agents = updated_registry["agents"]
    plan = BatchPlan(fleet_parser._exchange_paths)
    wrapper_count = 0
    unit_count = 0
    scaffold_file_count = 0

    if systemd_dir.exists() or systemd_dir.is_symlink():
        require_real_directory(systemd_dir, "systemd user directory")

    for agent_id, config in agents.items():
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
            raise BackfillError("registry contains an unsafe agent id")
        if not isinstance(config, dict):
            raise BackfillError("registry agent entry must contain a mapping")
        role_dir_value = config.get("role_dir")
        project_path_value = config.get("project_path")
        if not isinstance(role_dir_value, str) or not role_dir_value or not Path(role_dir_value).is_absolute():
            raise BackfillError("registry agent role_dir must be an absolute path")
        if not isinstance(project_path_value, str) or not project_path_value or not Path(project_path_value).is_absolute():
            raise BackfillError("registry agent project_path must be an absolute path")
        project_path = absolute(project_path_value)
        role_path = absolute(role_dir_value)
        require_real_directory(project_path, "registry project path")
        require_real_directory(role_path, "registry role path")
        if not contained_by(project_path, role_path):
            raise BackfillError("registry role path escapes its project")
        role_name = str(config.get("role") or "pm")
        if not ROLE_NAME.fullmatch(role_name):
            raise BackfillError("registry role name is unsafe")
        alternate = project_path / "_agents" / "hermes" / role_name
        if not (role_path / "hermes").exists() and (alternate / "hermes").exists():
            require_real_directory(alternate, "alternate registry role path")
            if not contained_by(project_path, alternate):
                raise BackfillError("alternate registry role path escapes its project")
            role_path = alternate
            config["role_dir"] = str(alternate)

        scripts_dir = role_path / ".scripts"
        role_lib_dir = scripts_dir / "lib"
        for directory, label in ((scripts_dir, "role script directory"), (role_lib_dir, "role library directory")):
            if directory.exists() or directory.is_symlink():
                require_real_directory(directory, label)
            else:
                plan.plan_directory(directory)

        scaffold_file_count += source_tree(
            plan,
            scaffold_source,
            role_path / ".runtime-scaffold",
        )
        plan.plan_file(
            role_lib_dir / "fleet-env.sh",
            fleet_library_source.content,
            mode=fleet_library_source.mode,
        )
        parser_source = read_regular_file(parser_path, "canonical fleet parser")
        plan.plan_file(
            role_lib_dir / "parse-fleet-env.py",
            parser_source.content,
            mode=parser_source.mode,
        )
        plan.plan_file(
            scripts_dir / "_lib.sh",
            role_library_source.content,
            mode=role_library_source.mode,
        )
        plan.plan_file(
            scripts_dir / "heartbeat.sh",
            heartbeat_source.content,
            mode=heartbeat_source.mode,
        )

        profile_name = str(config.get("profile_name") or agent_id)
        if not AGENT_ID.fullmatch(profile_name):
            raise BackfillError("registry profile name is unsafe")
        wrapper_text = wrapper_template.replace("{{ agent_id }}", agent_id)
        wrapper_text = wrapper_text.replace(
            f'PROFILE_NAME="${{HERMES_PROFILE_NAME:-{agent_id}}}"',
            f'PROFILE_NAME="${{HERMES_PROFILE_NAME:-{profile_name}}}"',
        )
        wrapper_path = role_path / "hermes"
        wrapper_metadata = lstat_optional(wrapper_path)
        wrapper_mode = 0o755
        if wrapper_metadata is not None:
            wrapper_mode = stat.S_IMODE(wrapper_metadata.st_mode) | 0o111
        plan.plan_file(wrapper_path, wrapper_text.encode("utf-8"), mode=wrapper_mode)
        wrapper_count += 1

        if systemd_dir.exists():
            for suffix in ("gateway", "consumer"):
                unit = systemd_dir / f"hermes-{agent_id}-{suffix}.service"
                if not unit.exists() and not unit.is_symlink():
                    continue
                original_unit = read_regular_file(unit, "Hermes systemd unit")
                updated_unit = patch_unit(
                    original_unit.content,
                    args.oauth_file,
                    args.codex_home,
                )
                plan.plan_file(unit, updated_unit, mode=original_unit.mode)
                if updated_unit != original_unit.content:
                    unit_count += 1

        config["hermes"] = {
            "bin": args.fleet_bin,
            "repo": args.fleet_repo,
            "fleet_env": str(fleet_path),
            "oauth_file": args.oauth_file,
            "codex_home": args.codex_home,
        }

    try:
        registry_content = yaml.safe_dump(updated_registry, sort_keys=False).encode("utf-8")
    except (UnicodeError, yaml.YAMLError) as error:
        raise BackfillError("fleet registry cannot be serialized") from error
    plan.plan_file(
        registry_path,
        registry_content,
        mode=registry_snapshot.mode,
        parent_mode=0o700,
    )

    try:
        fleet_text, fleet_metadata = fleet_parser.read_regular_document(
            fleet_path,
            allow_missing=True,
        )
        if fleet_metadata is None:
            fleet_updates = (
                ("HERMES_FLEET_BIN", args.fleet_bin),
                ("HERMES_FLEET_REPO", args.fleet_repo),
                ("HERMES_FLEET_REGISTRY_FILE", str(registry_path)),
                ("HERMES_FLEET_OAUTH_FILE", args.oauth_file),
                ("HERMES_FLEET_CODEX_HOME", args.codex_home),
            )
            fleet_mode = 0o600
        else:
            fleet_updates = (
                ("HERMES_FLEET_OAUTH_FILE", args.oauth_file),
                ("HERMES_FLEET_CODEX_HOME", args.codex_home),
            )
            fleet_mode = stat.S_IMODE(fleet_metadata.st_mode)
        for key, value in fleet_updates:
            fleet_text = fleet_parser.render_upsert(
                fleet_text,
                key,
                value,
                os.environ,
            )
        fleet_parser.parse_document(fleet_text, os.environ)
    except (OSError, UnicodeError, ValueError) as error:
        raise BackfillError("fleet environment cannot be planned") from error
    plan.plan_file(
        fleet_path,
        fleet_text.encode("utf-8"),
        mode=fleet_mode,
        parent_mode=0o700,
    )

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(
        f"backfill-fleet-sot: {mode} agents={len(agents)} "
        f"files={len(plan.files)} directories={len(plan.directories)} "
        f"wrappers={wrapper_count} scaffold_files={scaffold_file_count} units={unit_count}"
    )
    if not args.dry_run:
        plan.apply()
        print("backfill-fleet-sot: apply complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackfillError as error:
        print(f"backfill-fleet-sot: {error}", file=sys.stderr)
        raise SystemExit(2)
