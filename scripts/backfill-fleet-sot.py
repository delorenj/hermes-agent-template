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
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
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
    size: int
    mtime_ns: int


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
    prepared_snapshot: FileSnapshot
    installed_identity: tuple[int, int] | None = None
    installed_snapshot: FileSnapshot | None = None


def absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def contained_by(parent: Path, child: Path) -> bool:
    try:
        child.resolve(strict=True).relative_to(parent.resolve(strict=True))
    except (OSError, ValueError):
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
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    # Compare both the still-open descriptor and the final path against the
    # complete pre-read identity/version metadata. A same-inode writer cannot
    # make a hybrid byte stream look like the attested snapshot.
    def stable(actual: os.stat_result) -> bool:
        return (
            actual.st_dev == metadata.st_dev
            and actual.st_ino == metadata.st_ino
            and actual.st_mode == metadata.st_mode
            and actual.st_uid == metadata.st_uid
            and actual.st_gid == metadata.st_gid
            and actual.st_size == metadata.st_size
            and actual.st_mtime_ns == metadata.st_mtime_ns
            and actual.st_ctime_ns == metadata.st_ctime_ns
        )

    if not stable(after) or not stable(current):
        raise BackfillError(f"{label} changed while it was read")
    return FileSnapshot(
        path=path,
        content=b"".join(chunks),
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def load_parser(path: Path) -> ModuleType:
    snapshot = read_regular_file(path, "canonical fleet parser")
    if path.is_symlink():
        raise BackfillError("canonical fleet parser must not be a symlink")
    try:
        source = snapshot.content.decode("utf-8")
        code = compile(source, "<attested fleet parser>", "exec", dont_inherit=True)
        module = ModuleType("pjangler_fleet_env_parser")
        module.__file__ = "<attested fleet parser>"
        exec(code, module.__dict__)
    except (SyntaxError, UnicodeError) as error:
        raise BackfillError("canonical fleet parser cannot be loaded") from error
    for name in (
        "INITIAL_DOCUMENT",
        "parse_document",
        "read_regular_document",
        "render_upsert",
        "serialize_systemd_environment",
        "_exchange_paths",
    ):
        if not hasattr(module, name):
            raise BackfillError("canonical fleet parser has an unsupported contract")
    return module


class TransactionLock:
    """A stable, nonblocking interprocess lease on the registry directory."""

    def __init__(self, path: Path) -> None:
        self.path = absolute(path)
        self.descriptor = -1

    def __enter__(self) -> "TransactionLock":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(self.path, flags)
            metadata = os.fstat(self.descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
            ):
                raise BackfillError("backfill transaction lock metadata is unsafe")
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BackfillError("another backfill transaction is active") from error
            return self
        except BaseException:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self.descriptor >= 0:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1


class BatchPlan:
    def __init__(self, exchange_paths: Any, journal_path: Path | None = None) -> None:
        self.exchange_paths = exchange_paths
        self.journal_path = absolute(journal_path) if journal_path is not None else None
        self.directories: dict[Path, int] = {}
        self.files: dict[Path, PlannedFile] = {}

    @staticmethod
    def _snapshot_record(snapshot: FileSnapshot) -> dict[str, object]:
        return {
            "device": snapshot.device,
            "inode": snapshot.inode,
            "mode": snapshot.mode,
            "uid": snapshot.uid,
            "gid": snapshot.gid,
            "size": len(snapshot.content),
            "mtime_ns": snapshot.mtime_ns,
            "sha256": hashlib.sha256(snapshot.content).hexdigest(),
        }

    @staticmethod
    def _matches_record(snapshot: FileSnapshot, record: dict[str, object]) -> bool:
        expected = {
            "device": snapshot.device,
            "inode": snapshot.inode,
            "mode": snapshot.mode,
            "uid": snapshot.uid,
            "gid": snapshot.gid,
            "size": len(snapshot.content),
            "mtime_ns": snapshot.mtime_ns,
            "sha256": hashlib.sha256(snapshot.content).hexdigest(),
        }
        return expected == record

    def _effective_journal_path(self) -> Path | None:
        if self.journal_path is not None:
            return self.journal_path
        first = next(iter(self.files.values()), None)
        if first is None:
            return None
        return first.path.parent / ".pjangler-backfill-transaction.json"

    @staticmethod
    def _sync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_journal(path: Path) -> dict[str, object]:
        snapshot = read_regular_file(path, "backfill transaction journal")
        if snapshot.uid != os.geteuid() or snapshot.mode & 0o177:
            raise BackfillError("backfill transaction journal metadata is unsafe")
        try:
            payload = json.loads(snapshot.content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BackfillError("backfill transaction journal is invalid") from error
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise BackfillError("backfill transaction journal has an unsupported contract")
        if payload.get("phase") not in {"committing", "committed"}:
            raise BackfillError("backfill transaction journal has an invalid phase")
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise BackfillError("backfill transaction journal has no entries")
        return payload

    @classmethod
    def _write_journal_document(
        cls,
        path: Path,
        payload: dict[str, object],
        exchange_paths: Any,
        *,
        create: bool,
    ) -> None:
        if path.exists() or path.is_symlink():
            if create:
                raise BackfillError("an unrecovered backfill transaction already exists")
            cls._read_journal(path)
        elif not create:
            raise BackfillError("backfill transaction journal disappeared")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.stage-",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if create:
                os.link(temporary, path, follow_symlinks=False)
                temporary.unlink()
            else:
                exchange_paths(temporary, path)
                temporary.unlink()
            cls._sync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def recover_pending(cls, journal_path: Path, exchange_paths: Any) -> None:
        journal_path = absolute(journal_path)
        if not journal_path.exists() and not journal_path.is_symlink():
            return
        payload = cls._read_journal(journal_path)
        phase = payload["phase"]
        raw_entries = payload["entries"]
        assert isinstance(raw_entries, list)
        touched: set[Path] = {journal_path.parent}

        def snapshot(path: Path, label: str) -> FileSnapshot | None:
            try:
                return read_regular_file(path, label)
            except BackfillError:
                if not path.exists() and not path.is_symlink():
                    return None
                raise

        for raw in reversed(raw_entries):
            if not isinstance(raw, dict):
                raise BackfillError("backfill transaction journal entry is invalid")
            destination_value = raw.get("destination")
            temporary_value = raw.get("temporary")
            prepared_record = raw.get("prepared")
            original_record = raw.get("original")
            if (
                not isinstance(destination_value, str)
                or not Path(destination_value).is_absolute()
                or not isinstance(temporary_value, str)
                or not Path(temporary_value).is_absolute()
                or not isinstance(prepared_record, dict)
                or (original_record is not None and not isinstance(original_record, dict))
            ):
                raise BackfillError("backfill transaction journal entry is invalid")
            destination = absolute(destination_value)
            temporary = absolute(temporary_value)
            if temporary.parent != destination.parent or not temporary.name.startswith(
                f".{destination.name}.pjangler-backfill-"
            ):
                raise BackfillError("backfill transaction temporary path is unsafe")
            touched.add(destination.parent)
            destination_snapshot = snapshot(destination, "backfill recovery destination")
            temporary_snapshot = snapshot(temporary, "backfill recovery temporary")
            destination_is_prepared = (
                destination_snapshot is not None
                and cls._matches_record(destination_snapshot, prepared_record)
            )
            temporary_is_prepared = (
                temporary_snapshot is not None
                and cls._matches_record(temporary_snapshot, prepared_record)
            )
            destination_is_original = (
                isinstance(original_record, dict)
                and destination_snapshot is not None
                and cls._matches_record(destination_snapshot, original_record)
            )
            temporary_is_original = (
                isinstance(original_record, dict)
                and temporary_snapshot is not None
                and cls._matches_record(temporary_snapshot, original_record)
            )

            if phase == "committed":
                if not destination_is_prepared:
                    raise BackfillError("committed backfill destination changed before recovery")
                if temporary_snapshot is not None:
                    if not (temporary_is_original or temporary_is_prepared):
                        raise BackfillError("committed backfill recovery data changed")
                    temporary.unlink()
                    cls._sync_directory(destination.parent)
                continue

            if isinstance(original_record, dict):
                if destination_is_prepared and temporary_is_original:
                    exchange_paths(temporary, destination)
                    cls._sync_directory(destination.parent)
                    destination_snapshot = snapshot(destination, "restored backfill destination")
                    temporary_snapshot = snapshot(temporary, "rolled-back prepared destination")
                    if (
                        destination_snapshot is None
                        or temporary_snapshot is None
                        or not cls._matches_record(destination_snapshot, original_record)
                        or not cls._matches_record(temporary_snapshot, prepared_record)
                    ):
                        raise BackfillError("backfill transaction rollback could not be reattested")
                    temporary.unlink()
                    cls._sync_directory(destination.parent)
                elif destination_is_original and temporary_is_prepared:
                    temporary.unlink()
                    cls._sync_directory(destination.parent)
                elif destination_is_original and temporary_snapshot is None:
                    # A prior recovery completed this entry and died before it
                    # could durably remove the journal. Repeating is a no-op.
                    pass
                else:
                    raise BackfillError("backfill transaction state changed before recovery")
            else:
                if destination_is_prepared and temporary_is_prepared:
                    destination.unlink()
                    cls._sync_directory(destination.parent)
                    temporary.unlink()
                    cls._sync_directory(destination.parent)
                elif destination_snapshot is None and temporary_is_prepared:
                    temporary.unlink()
                    cls._sync_directory(destination.parent)
                elif destination_snapshot is None and temporary_snapshot is None:
                    # Terminal rollback state after an interrupted recovery.
                    pass
                else:
                    raise BackfillError("new backfill destination changed before recovery")

        cls._sync_directories(touched)
        journal_path.unlink()
        cls._sync_directory(journal_path.parent)

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
        selected_mode = stat.S_IMODE(selected_mode)
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
            if plan.original is not None:
                os.fchown(descriptor, plan.original.uid, plan.original.gid)
            view = memoryview(plan.content)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            # fchown and content writes may clear set-ID bits. Restore the exact
            # planned mode last, then attest every security-relevant field.
            os.fchmod(descriptor, plan.mode)
            prepared_metadata = os.fstat(descriptor)
            if stat.S_IMODE(prepared_metadata.st_mode) != plan.mode:
                raise BackfillError("prepared destination mode could not be preserved")
            if plan.original is not None and (
                prepared_metadata.st_uid != plan.original.uid
                or prepared_metadata.st_gid != plan.original.gid
            ):
                raise BackfillError("prepared destination ownership could not be preserved")
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        os.close(descriptor)
        prepared_snapshot = read_regular_file(temporary, "prepared backfill destination")
        if prepared_snapshot.content != plan.content or prepared_snapshot.mode != plan.mode:
            temporary.unlink()
            raise BackfillError("prepared destination failed exact reattestation")
        if plan.original is not None and (
            prepared_snapshot.uid != plan.original.uid
            or prepared_snapshot.gid != plan.original.gid
        ):
            temporary.unlink()
            raise BackfillError("prepared destination ownership drifted")
        return PreparedFile(plan, temporary, prepared_snapshot)

    @staticmethod
    def _same_snapshot(expected: FileSnapshot, actual: FileSnapshot) -> bool:
        return (
            expected.content == actual.content
            and expected.mode == actual.mode
            and expected.uid == actual.uid
            and expected.gid == actual.gid
            and expected.device == actual.device
            and expected.inode == actual.inode
            and expected.size == actual.size
            and expected.mtime_ns == actual.mtime_ns
        )

    def _commit_one(self, prepared: PreparedFile) -> None:
        plan = prepared.plan
        current_prepared = read_regular_file(prepared.temporary, "prepared backfill destination")
        if not self._same_snapshot(prepared.prepared_snapshot, current_prepared):
            raise BackfillError("prepared destination changed before backfill commit")
        if plan.original is None:
            if lstat_optional(plan.path) is not None:
                raise BackfillError("destination appeared after backfill preflight")
            os.link(prepared.temporary, plan.path, follow_symlinks=False)
            installed = read_regular_file(plan.path, "installed backfill destination")
            if not self._same_snapshot(prepared.prepared_snapshot, installed):
                plan.path.unlink()
                raise BackfillError("installed destination failed exact reattestation")
            prepared.installed_snapshot = installed
            prepared.installed_identity = (installed.device, installed.inode)
            # Keep the second hard link until the fsynced journal is marked
            # committed.  A process death between link(2) and journal cleanup
            # can then distinguish and roll back a newly-created destination.
            return

        current = read_regular_file(plan.path, f"destination {plan.path.name}")
        if not self._same_snapshot(plan.original, current):
            raise BackfillError("destination changed after backfill preflight")
        self.exchange_paths(prepared.temporary, plan.path)
        displaced = read_regular_file(prepared.temporary, "displaced backfill destination")
        if not self._same_snapshot(plan.original, displaced):
            self.exchange_paths(prepared.temporary, plan.path)
            raise BackfillError("destination changed at backfill commit boundary")
        installed = read_regular_file(plan.path, "installed backfill destination")
        if not self._same_snapshot(prepared.prepared_snapshot, installed):
            self.exchange_paths(prepared.temporary, plan.path)
            raise BackfillError("installed destination failed exact reattestation")
        prepared.installed_snapshot = installed
        prepared.installed_identity = (installed.device, installed.inode)

    def _rollback(self, committed: list[PreparedFile]) -> list[Path]:
        preserved: list[Path] = []
        for prepared in reversed(committed):
            plan = prepared.plan
            try:
                current = read_regular_file(plan.path, "installed backfill destination")
                if prepared.installed_snapshot is None or not self._same_snapshot(
                    prepared.installed_snapshot,
                    current,
                ):
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
        journal_path = self._effective_journal_path()
        journal_written = False
        previous_sigterm: Any = None
        signal_handler_installed = False
        try:
            if journal_path is not None:
                self.recover_pending(journal_path, self.exchange_paths)
            for path, mode in sorted(self.directories.items(), key=lambda item: len(item[0].parts)):
                if lstat_optional(path) is not None:
                    require_real_directory(path, "planned destination directory")
                    continue
                path.mkdir(mode=mode)
                created_directories.append(path)
                self._sync_directory(path.parent)
            for plan in self.files.values():
                prepared.append(self._write_prepared(plan))
                self._sync_directory(prepared[-1].temporary.parent)
            if prepared and journal_path is not None:
                journal_payload: dict[str, object] = {
                    "version": 1,
                    "phase": "committing",
                    "entries": [
                        {
                            "destination": str(item.plan.path),
                            "temporary": str(item.temporary),
                            "original": (
                                self._snapshot_record(item.plan.original)
                                if item.plan.original is not None
                                else None
                            ),
                            "prepared": self._snapshot_record(item.prepared_snapshot),
                        }
                        for item in prepared
                    ],
                }
                self._write_journal_document(
                    journal_path,
                    journal_payload,
                    self.exchange_paths,
                    create=True,
                )
                journal_written = True

                def interrupt_backfill(signum: int, _frame: object) -> None:
                    raise BackfillError(f"backfill transaction interrupted by signal {signum}")

                try:
                    previous_sigterm = signal.getsignal(signal.SIGTERM)
                    signal.signal(signal.SIGTERM, interrupt_backfill)
                    signal_handler_installed = True
                except ValueError:
                    pass
            for item in prepared:
                self._commit_one(item)
                committed.append(item)
                self._sync_directory(item.plan.path.parent)
            self._sync_directories({plan.path.parent for plan in self.files.values()})
            if prepared and journal_path is not None:
                journal_payload["phase"] = "committed"
                self._write_journal_document(
                    journal_path,
                    journal_payload,
                    self.exchange_paths,
                    create=False,
                )
        except BaseException as error:
            if journal_written and journal_path is not None:
                try:
                    self.recover_pending(journal_path, self.exchange_paths)
                    preserved: list[Path] = []
                except BaseException:
                    preserved = [item.temporary for item in prepared if item.temporary.exists()]
            else:
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
            cleaned_parents: set[Path] = set()
            for item in committed:
                if item.temporary.exists():
                    item.temporary.unlink()
                    cleaned_parents.add(item.temporary.parent)
            self._sync_directories(cleaned_parents)
            if journal_path is not None and journal_path.exists():
                journal_path.unlink()
                self._sync_directory(journal_path.parent)
        finally:
            if signal_handler_installed:
                signal.signal(signal.SIGTERM, previous_sigterm)


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


def patch_unit(
    original: bytes,
    oauth_file: str,
    codex_home: str,
    serialize_environment: Any,
) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BackfillError("systemd unit must be valid UTF-8") from error
    lines = text.splitlines()
    def environment_name(line: str) -> str | None:
        match = re.match(r'^Environment="?([A-Za-z_][A-Za-z0-9_]*)=', line)
        return match.group(1) if match else None

    home_lines = [line for line in lines if environment_name(line) == "HERMES_HOME"]
    if len(home_lines) != 1:
        raise BackfillError("systemd unit must contain exactly one HERMES_HOME environment line")
    rendered: list[str] = []
    for line in lines:
        name = environment_name(line)
        if name in {"HERMES_OAUTH_FILE", "CODEX_HOME"}:
            continue
        rendered.append(line)
        if name == "HERMES_HOME":
            rendered.append(serialize_environment("HERMES_OAUTH_FILE", oauth_file))
            rendered.append(serialize_environment("CODEX_HOME", codex_home))
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


def run_backfill(
    args: argparse.Namespace,
    fleet_parser: ModuleType,
    journal_path: Path,
) -> int:
    parser_path = absolute(args.fleet_parser_source)
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
    plan = BatchPlan(fleet_parser._exchange_paths, journal_path)
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
            for suffix in ("gateway", "heartbeat", "consumer"):
                unit = systemd_dir / f"hermes-{agent_id}-{suffix}.service"
                if not unit.exists() and not unit.is_symlink():
                    continue
                original_unit = read_regular_file(unit, "Hermes systemd unit")
                updated_unit = patch_unit(
                    original_unit.content,
                    args.oauth_file,
                    args.codex_home,
                    fleet_parser.serialize_systemd_environment,
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


def main() -> int:
    args = parse_args()
    parser_path = absolute(args.fleet_parser_source)
    fleet_parser = load_parser(parser_path)
    registry_path = absolute(args.registry)
    require_real_directory(registry_path.parent, "fleet registry parent")
    journal_path = registry_path.parent / ".pjangler-backfill-transaction.json"
    with TransactionLock(registry_path.parent):
        if journal_path.exists() or journal_path.is_symlink():
            if args.dry_run:
                # Dry-run is a strict zero-mutation mode. Validate enough to
                # diagnose, but leave recovery to an apply invocation.
                BatchPlan._read_journal(journal_path)
                raise BackfillError("a pending backfill transaction requires an apply recovery")
            BatchPlan.recover_pending(journal_path, fleet_parser._exchange_paths)
        return run_backfill(args, fleet_parser, journal_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackfillError as error:
        print(f"backfill-fleet-sot: {error}", file=sys.stderr)
        raise SystemExit(2)
