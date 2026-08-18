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
TRANSACTION_JOURNAL_VERSION = 2


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
    ctime_ns: int


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: bytes
    mode: int
    original: FileSnapshot | None


@dataclass(frozen=True)
class DirectoryIdentity:
    path: Path
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    real_path: Path


@dataclass(frozen=True)
class PlannedDirectory:
    path: Path
    mode: int
    parent: Path


@dataclass
class PreparedFile:
    plan: PlannedFile
    temporary: Path
    prepared_snapshot: FileSnapshot
    parent_descriptor: int
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


def directory_identity(path: Path, label: str) -> DirectoryIdentity:
    path = absolute(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BackfillError(f"{label} must be a real directory") from error
    try:
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise BackfillError(f"{label} changed while it was opened")
        real_path = Path(os.path.realpath(f"/proc/self/fd/{descriptor}"))
        return DirectoryIdentity(
            path=path,
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            real_path=real_path,
        )
    finally:
        os.close(descriptor)


def open_attested_directory(identity: DirectoryIdentity, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity.path, flags)
    except OSError as error:
        raise BackfillError(f"{label} changed after preflight") from error
    metadata = os.fstat(descriptor)
    current = identity.path.lstat()
    real_path = Path(os.path.realpath(f"/proc/self/fd/{descriptor}"))
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode)
        or (current.st_dev, current.st_ino) != (identity.device, identity.inode)
        or stat.S_IMODE(metadata.st_mode) != identity.mode
        or metadata.st_uid != identity.uid
        or metadata.st_gid != identity.gid
        or real_path != identity.real_path
    ):
        os.close(descriptor)
        raise BackfillError(f"{label} changed after preflight")
    return descriptor


def directory_identity_at(
    parent_descriptor: int,
    name: str,
    path: Path,
    label: str,
) -> tuple[DirectoryIdentity, int]:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise BackfillError(f"{label} has an unsafe name")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise BackfillError(f"{label} must be a real directory") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise BackfillError(f"{label} must be a real directory")
    return (
        DirectoryIdentity(
            path=absolute(path),
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            real_path=Path(os.path.realpath(f"/proc/self/fd/{descriptor}")),
        ),
        descriptor,
    )


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
        ctime_ns=after.st_ctime_ns,
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
        self.directories: dict[Path, PlannedDirectory] = {}
        self.directory_identities: dict[Path, DirectoryIdentity] = {}
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

    @staticmethod
    def _directory_record(identity: DirectoryIdentity) -> dict[str, object]:
        return {
            "device": identity.device,
            "inode": identity.inode,
            "mode": identity.mode,
            "uid": identity.uid,
            "gid": identity.gid,
            "real_path": str(identity.real_path),
        }

    @staticmethod
    def _matches_directory_record(identity: DirectoryIdentity, record: dict[str, object]) -> bool:
        return BatchPlan._directory_record(identity) == record

    @staticmethod
    def _identity_from_record(
        path: Path,
        record: dict[str, object],
        label: str,
    ) -> DirectoryIdentity:
        expected_keys = {"device", "inode", "mode", "uid", "gid", "real_path"}
        if set(record) != expected_keys:
            raise BackfillError(f"{label} identity is invalid")
        numeric = [record[key] for key in ("device", "inode", "mode", "uid", "gid")]
        real_path = record["real_path"]
        if (
            any(not isinstance(value, int) or isinstance(value, bool) for value in numeric)
            or not isinstance(real_path, str)
            or not Path(real_path).is_absolute()
        ):
            raise BackfillError(f"{label} identity is invalid")
        return DirectoryIdentity(
            path=absolute(path),
            device=int(record["device"]),
            inode=int(record["inode"]),
            mode=int(record["mode"]),
            uid=int(record["uid"]),
            gid=int(record["gid"]),
            real_path=absolute(real_path),
        )

    @staticmethod
    def _path_at(descriptor: int, name: str) -> Path:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise BackfillError("backfill transaction path component is unsafe")
        return Path(f"/proc/self/fd/{descriptor}") / name

    @classmethod
    def _snapshot_at(
        cls,
        parent: DirectoryIdentity,
        name: str,
        label: str,
    ) -> FileSnapshot | None:
        descriptor = open_attested_directory(parent, label + " parent")
        try:
            try:
                return read_regular_file(cls._path_at(descriptor, name), label)
            except BackfillError:
                try:
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                raise
        finally:
            os.close(descriptor)

    @classmethod
    def _unlink_at(cls, parent: DirectoryIdentity, name: str, label: str) -> None:
        descriptor = open_attested_directory(parent, label + " parent")
        try:
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _exchange_at(
        cls,
        parent: DirectoryIdentity,
        left: str,
        right: str,
        exchange_paths: Any,
        label: str,
    ) -> None:
        descriptor = open_attested_directory(parent, label + " parent")
        try:
            exchange_paths(cls._path_at(descriptor, left), cls._path_at(descriptor, right))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _remove_directory_at(
        cls,
        parent: DirectoryIdentity,
        name: str,
        expected: dict[str, object],
    ) -> None:
        descriptor = open_attested_directory(parent, "backfill recovery directory parent")
        try:
            identity, child_descriptor = directory_identity_at(
                descriptor,
                name,
                parent.path / name,
                "backfill recovery directory",
            )
            try:
                if not cls._matches_directory_record(identity, expected):
                    raise BackfillError("backfill recovery directory changed after creation")
            finally:
                os.close(child_descriptor)
            try:
                os.rmdir(name, dir_fd=descriptor)
            except OSError as error:
                raise BackfillError("backfill recovery directory is not empty") from error
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _effective_journal_path(self) -> Path | None:
        if self.journal_path is not None:
            return self.journal_path
        first = next(iter(self.files.values()), None)
        if first is None:
            return None
        return first.path.parent / ".pjangler-backfill-transaction.json"

    @staticmethod
    def _read_journal(path: Path) -> dict[str, object]:
        snapshot = read_regular_file(path, "backfill transaction journal")
        if snapshot.uid != os.geteuid() or snapshot.mode & 0o177:
            raise BackfillError("backfill transaction journal metadata is unsafe")
        try:
            payload = json.loads(snapshot.content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BackfillError("backfill transaction journal is invalid") from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != TRANSACTION_JOURNAL_VERSION
        ):
            raise BackfillError("backfill transaction journal has an unsupported contract")
        if payload.get("phase") not in {"committing", "committed"}:
            raise BackfillError("backfill transaction journal has an invalid phase")
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise BackfillError("backfill transaction journal has no entries")
        directories = payload.get("directories")
        if not isinstance(directories, list):
            raise BackfillError("backfill transaction journal has invalid directories")
        return payload

    @classmethod
    def _write_journal_document(
        cls,
        path: Path,
        payload: dict[str, object],
        exchange_paths: Any,
        *,
        create: bool,
        parent_descriptor: int | None = None,
    ) -> None:
        path = absolute(path)
        if parent_descriptor is None:
            parent_identity = directory_identity(path.parent, "backfill journal parent")
            parent_descriptor = open_attested_directory(
                parent_identity,
                "backfill journal parent",
            )
        else:
            parent_descriptor = os.dup(parent_descriptor)
        journal_at = cls._path_at(parent_descriptor, path.name)

        def journal_exists() -> bool:
            try:
                os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False

        exists = journal_exists()
        if exists:
            if create:
                os.close(parent_descriptor)
                raise BackfillError("an unrecovered backfill transaction already exists")
            try:
                cls._read_journal(journal_at)
            except BaseException:
                os.close(parent_descriptor)
                raise
        elif not create:
            os.close(parent_descriptor)
            raise BackfillError("backfill transaction journal disappeared")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = -1
        temporary_name = ""
        published = False
        try:
            descriptor, temporary_value = tempfile.mkstemp(
                prefix=f".{path.name}.stage-",
                dir=Path(f"/proc/self/fd/{parent_descriptor}"),
            )
            temporary_name = Path(temporary_value).name
            temporary_at = cls._path_at(parent_descriptor, temporary_name)
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if create:
                os.link(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            else:
                exchange_paths(temporary_at, journal_at)
            published = True
            # The journal itself must be durable before its staging link is
            # removed.  If cleanup fails, leave that link intact rather than
            # deleting data referenced by a surviving journal.
            os.fsync(parent_descriptor)
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name and not published:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                except FileNotFoundError:
                    pass
            os.close(parent_descriptor)

    @classmethod
    def recover_pending(cls, journal_path: Path, exchange_paths: Any) -> None:
        journal_path = absolute(journal_path)
        journal_parent = directory_identity(
            journal_path.parent,
            "backfill journal parent",
        )
        if cls._snapshot_at(
            journal_parent,
            journal_path.name,
            "backfill transaction journal",
        ) is None:
            return
        journal_parent_descriptor = open_attested_directory(
            journal_parent,
            "backfill journal parent",
        )
        try:
            payload = cls._read_journal(
                cls._path_at(journal_parent_descriptor, journal_path.name)
            )
        finally:
            os.close(journal_parent_descriptor)
        phase = payload["phase"]
        raw_entries = payload["entries"]
        assert isinstance(raw_entries, list)
        for raw in reversed(raw_entries):
            if not isinstance(raw, dict):
                raise BackfillError("backfill transaction journal entry is invalid")
            destination_value = raw.get("destination")
            temporary_value = raw.get("temporary")
            prepared_record = raw.get("prepared")
            original_record = raw.get("original")
            parent_record = raw.get("parent")
            if (
                not isinstance(destination_value, str)
                or not Path(destination_value).is_absolute()
                or not isinstance(temporary_value, str)
                or not Path(temporary_value).is_absolute()
                or not isinstance(prepared_record, dict)
                or (original_record is not None and not isinstance(original_record, dict))
                or not isinstance(parent_record, dict)
            ):
                raise BackfillError("backfill transaction journal entry is invalid")
            destination = absolute(destination_value)
            temporary = absolute(temporary_value)
            if temporary.parent != destination.parent or not temporary.name.startswith(
                f".{destination.name}.pjangler-backfill-"
            ):
                raise BackfillError("backfill transaction temporary path is unsafe")
            current_parent = cls._identity_from_record(
                destination.parent,
                parent_record,
                "backfill recovery parent",
            )
            destination_snapshot = cls._snapshot_at(
                current_parent,
                destination.name,
                "backfill recovery destination",
            )
            temporary_snapshot = cls._snapshot_at(
                current_parent,
                temporary.name,
                "backfill recovery temporary",
            )
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
                    cls._unlink_at(
                        current_parent,
                        temporary.name,
                        "committed backfill recovery",
                    )
                continue

            if isinstance(original_record, dict):
                if destination_is_prepared and temporary_is_original:
                    cls._exchange_at(
                        current_parent,
                        temporary.name,
                        destination.name,
                        exchange_paths,
                        "backfill rollback",
                    )
                    destination_snapshot = cls._snapshot_at(
                        current_parent,
                        destination.name,
                        "restored backfill destination",
                    )
                    temporary_snapshot = cls._snapshot_at(
                        current_parent,
                        temporary.name,
                        "rolled-back prepared destination",
                    )
                    if (
                        destination_snapshot is None
                        or temporary_snapshot is None
                        or not cls._matches_record(destination_snapshot, original_record)
                        or not cls._matches_record(temporary_snapshot, prepared_record)
                    ):
                        raise BackfillError("backfill transaction rollback could not be reattested")
                    cls._unlink_at(
                        current_parent,
                        temporary.name,
                        "backfill rollback cleanup",
                    )
                elif destination_is_original and temporary_is_prepared:
                    cls._unlink_at(
                        current_parent,
                        temporary.name,
                        "backfill rollback cleanup",
                    )
                elif destination_is_original and temporary_snapshot is None:
                    # A prior recovery completed this entry and died before it
                    # could durably remove the journal. Repeating is a no-op.
                    pass
                else:
                    raise BackfillError("backfill transaction state changed before recovery")
            else:
                if destination_is_prepared and temporary_is_prepared:
                    cls._unlink_at(
                        current_parent,
                        destination.name,
                        "backfill rollback new destination",
                    )
                    cls._unlink_at(
                        current_parent,
                        temporary.name,
                        "backfill rollback new temporary",
                    )
                elif destination_snapshot is None and temporary_is_prepared:
                    cls._unlink_at(
                        current_parent,
                        temporary.name,
                        "backfill rollback new temporary",
                    )
                elif destination_snapshot is None and temporary_snapshot is None:
                    # Terminal rollback state after an interrupted recovery.
                    pass
                else:
                    raise BackfillError("new backfill destination changed before recovery")

        raw_directories = payload["directories"]
        assert isinstance(raw_directories, list)
        if phase == "committing":
            for raw_directory in reversed(raw_directories):
                if not isinstance(raw_directory, dict):
                    raise BackfillError("backfill transaction directory entry is invalid")
                path_value = raw_directory.get("path")
                identity_record = raw_directory.get("identity")
                parent_record = raw_directory.get("parent")
                if (
                    not isinstance(path_value, str)
                    or not Path(path_value).is_absolute()
                    or not isinstance(identity_record, dict)
                    or not isinstance(parent_record, dict)
                ):
                    raise BackfillError("backfill transaction directory entry is invalid")
                path = absolute(path_value)
                try:
                    parent_identity = cls._identity_from_record(
                        path.parent,
                        parent_record,
                        "backfill recovery directory parent",
                    )
                    cls._remove_directory_at(
                        parent_identity,
                        path.name,
                        identity_record,
                    )
                except BackfillError:
                    parent_identity = cls._identity_from_record(
                        path.parent,
                        parent_record,
                        "backfill recovery directory parent",
                    )
                    parent_descriptor = open_attested_directory(
                        parent_identity,
                        "backfill recovery directory parent",
                    )
                    try:
                        try:
                            os.stat(
                                path.name,
                                dir_fd=parent_descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                    finally:
                        os.close(parent_descriptor)
                    raise

        cls._unlink_at(
            journal_parent,
            journal_path.name,
            "backfill journal cleanup",
        )

    def plan_directory(self, path: Path, mode: int = 0o755) -> None:
        path = absolute(path)
        missing: list[Path] = []
        cursor = path
        while True:
            metadata = lstat_optional(cursor)
            if metadata is not None:
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise BackfillError(f"destination directory is unsafe: {cursor}")
                self.directory_identities.setdefault(
                    cursor,
                    directory_identity(cursor, "destination directory"),
                )
                break
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise BackfillError("destination has no existing directory ancestor")
            cursor = parent
        for directory in reversed(missing):
            self.directories.setdefault(
                directory,
                PlannedDirectory(directory, stat.S_IMODE(mode), directory.parent),
            )

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

    def _write_prepared(self, plan: PlannedFile) -> PreparedFile:
        parent_identity = self.directory_identities.get(plan.path.parent)
        if parent_identity is None:
            raise BackfillError("destination parent has no stable identity")
        parent_descriptor = open_attested_directory(parent_identity, "destination parent")
        parent_handle = Path(f"/proc/self/fd/{parent_descriptor}")
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{plan.path.name}.pjangler-backfill-",
                dir=parent_handle,
            )
        except BaseException:
            os.close(parent_descriptor)
            raise
        temporary_at = Path(temporary_name)
        temporary = plan.path.parent / temporary_at.name
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
                os.unlink(temporary.name, dir_fd=parent_descriptor)
            except OSError:
                pass
            os.close(parent_descriptor)
            raise
        os.close(descriptor)
        try:
            prepared_snapshot = read_regular_file(
                temporary_at,
                "prepared backfill destination",
            )
            if prepared_snapshot.content != plan.content or prepared_snapshot.mode != plan.mode:
                raise BackfillError("prepared destination failed exact reattestation")
            if plan.original is not None and (
                prepared_snapshot.uid != plan.original.uid
                or prepared_snapshot.gid != plan.original.gid
            ):
                raise BackfillError("prepared destination ownership drifted")
            current_parent = directory_identity(plan.path.parent, "destination parent")
            if current_parent != parent_identity:
                raise BackfillError("destination parent changed during preparation")
            return PreparedFile(plan, temporary, prepared_snapshot, parent_descriptor)
        except BaseException:
            try:
                os.unlink(temporary.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError:
                pass
            os.close(parent_descriptor)
            raise

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
        parent_identity = self.directory_identities[plan.path.parent]
        if directory_identity(plan.path.parent, "destination parent") != parent_identity:
            raise BackfillError("destination parent changed before backfill commit")
        parent_handle = Path(f"/proc/self/fd/{prepared.parent_descriptor}")
        temporary_at = parent_handle / prepared.temporary.name
        destination_at = parent_handle / plan.path.name
        current_prepared = read_regular_file(temporary_at, "prepared backfill destination")
        if not self._same_snapshot(prepared.prepared_snapshot, current_prepared):
            raise BackfillError("prepared destination changed before backfill commit")
        if plan.original is None:
            try:
                os.stat(plan.path.name, dir_fd=prepared.parent_descriptor, follow_symlinks=False)
                destination_exists = True
            except FileNotFoundError:
                destination_exists = False
            if destination_exists:
                raise BackfillError("destination appeared after backfill preflight")
            os.link(
                prepared.temporary.name,
                plan.path.name,
                src_dir_fd=prepared.parent_descriptor,
                dst_dir_fd=prepared.parent_descriptor,
                follow_symlinks=False,
            )
            installed = read_regular_file(destination_at, "installed backfill destination")
            if not self._same_snapshot(prepared.prepared_snapshot, installed):
                os.unlink(plan.path.name, dir_fd=prepared.parent_descriptor)
                raise BackfillError("installed destination failed exact reattestation")
            prepared.installed_snapshot = installed
            prepared.installed_identity = (installed.device, installed.inode)
            # Keep the second hard link until the fsynced journal is marked
            # committed.  A process death between link(2) and journal cleanup
            # can then distinguish and roll back a newly-created destination.
            os.fsync(prepared.parent_descriptor)
            if directory_identity(plan.path.parent, "destination parent") != parent_identity:
                raise BackfillError("destination parent changed during backfill commit")
            return

        current = read_regular_file(destination_at, f"destination {plan.path.name}")
        if not self._same_snapshot(plan.original, current):
            raise BackfillError("destination changed after backfill preflight")
        self.exchange_paths(temporary_at, destination_at)
        displaced = read_regular_file(temporary_at, "displaced backfill destination")
        if not self._same_snapshot(plan.original, displaced):
            self.exchange_paths(temporary_at, destination_at)
            raise BackfillError("destination changed at backfill commit boundary")
        installed = read_regular_file(destination_at, "installed backfill destination")
        if not self._same_snapshot(prepared.prepared_snapshot, installed):
            self.exchange_paths(temporary_at, destination_at)
            raise BackfillError("installed destination failed exact reattestation")
        prepared.installed_snapshot = installed
        prepared.installed_identity = (installed.device, installed.inode)
        os.fsync(prepared.parent_descriptor)
        if directory_identity(plan.path.parent, "destination parent") != parent_identity:
            raise BackfillError("destination parent changed during backfill commit")

    def _rollback(self, committed: list[PreparedFile]) -> list[Path]:
        preserved: list[Path] = []
        for prepared in reversed(committed):
            plan = prepared.plan
            try:
                parent_handle = Path(f"/proc/self/fd/{prepared.parent_descriptor}")
                destination_at = parent_handle / plan.path.name
                temporary_at = parent_handle / prepared.temporary.name
                current = read_regular_file(destination_at, "installed backfill destination")
                if prepared.installed_snapshot is None or not self._same_snapshot(
                    prepared.installed_snapshot,
                    current,
                ):
                    preserved.append(prepared.temporary)
                    continue
                if plan.original is None:
                    os.unlink(plan.path.name, dir_fd=prepared.parent_descriptor)
                    os.unlink(prepared.temporary.name, dir_fd=prepared.parent_descriptor)
                else:
                    self.exchange_paths(temporary_at, destination_at)
                    os.unlink(prepared.temporary.name, dir_fd=prepared.parent_descriptor)
                os.fsync(prepared.parent_descriptor)
            except OSError:
                try:
                    os.stat(prepared.temporary.name, dir_fd=prepared.parent_descriptor, follow_symlinks=False)
                    preserved.append(prepared.temporary)
                except FileNotFoundError:
                    pass
        return preserved

    def apply(self) -> None:
        created_directories: list[Path] = []
        prepared: list[PreparedFile] = []
        committed: list[PreparedFile] = []
        journal_path = self._effective_journal_path()
        journal_parent: DirectoryIdentity | None = None
        journal_parent_descriptor = -1
        previous_sigterm: Any = None
        signal_handler_installed = False
        try:
            if journal_path is not None:
                self.recover_pending(journal_path, self.exchange_paths)
                journal_parent = directory_identity(
                    journal_path.parent,
                    "backfill journal parent",
                )
                journal_parent_descriptor = open_attested_directory(
                    journal_parent,
                    "backfill journal parent",
                )
            for path, planned_directory in sorted(self.directories.items(), key=lambda item: len(item[0].parts)):
                if lstat_optional(path) is not None:
                    actual = directory_identity(path, "planned destination directory")
                    known = self.directory_identities.get(path)
                    if known is not None and actual != known:
                        raise BackfillError("planned destination directory changed after preflight")
                    self.directory_identities[path] = actual
                    continue
                parent_identity = self.directory_identities.get(planned_directory.parent)
                if parent_identity is None:
                    raise BackfillError("planned destination parent has no stable identity")
                parent_descriptor = open_attested_directory(
                    parent_identity,
                    "planned destination parent",
                )
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
                    created_directories.append(path)
                    child_descriptor = os.open(
                        path.name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_descriptor,
                    )
                    try:
                        os.fchmod(child_descriptor, planned_directory.mode)
                        child_metadata = os.fstat(child_descriptor)
                        if (
                            not stat.S_ISDIR(child_metadata.st_mode)
                            or stat.S_IMODE(child_metadata.st_mode) != planned_directory.mode
                            or child_metadata.st_uid != os.geteuid()
                        ):
                            raise BackfillError("created destination directory metadata drifted")
                        os.fsync(child_descriptor)
                    finally:
                        os.close(child_descriptor)
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
                self.directory_identities[path] = directory_identity(
                    path,
                    "created destination directory",
                )
            for plan in self.files.values():
                prepared.append(self._write_prepared(plan))
                os.fsync(prepared[-1].parent_descriptor)
            if prepared and journal_path is not None:
                journal_payload: dict[str, object] = {
                    "version": TRANSACTION_JOURNAL_VERSION,
                    "phase": "committing",
                    "directories": [
                        {
                            "path": str(path),
                            "identity": self._directory_record(self.directory_identities[path]),
                            "parent": self._directory_record(
                                self.directory_identities[path.parent]
                            ),
                        }
                        for path in created_directories
                    ],
                    "entries": [
                        {
                            "destination": str(item.plan.path),
                            "temporary": str(item.temporary),
                            "parent": self._directory_record(
                                self.directory_identities[item.plan.path.parent]
                            ),
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
                    parent_descriptor=journal_parent_descriptor,
                )
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
                os.fsync(item.parent_descriptor)
            if prepared and journal_path is not None:
                journal_payload["phase"] = "committed"
                self._write_journal_document(
                    journal_path,
                    journal_payload,
                    self.exchange_paths,
                    create=False,
                    parent_descriptor=journal_parent_descriptor,
                )
        except BaseException as error:
            journal_present = False
            if journal_path is not None and journal_parent_descriptor >= 0:
                try:
                    os.stat(
                        journal_path.name,
                        dir_fd=journal_parent_descriptor,
                        follow_symlinks=False,
                    )
                    journal_present = True
                except FileNotFoundError:
                    journal_present = False
            if journal_present and journal_path is not None:
                try:
                    self.recover_pending(journal_path, self.exchange_paths)
                    preserved: list[Path] = []
                except BaseException:
                    preserved = self._rollback(committed)
                    if not preserved and journal_parent_descriptor >= 0:
                        try:
                            os.unlink(
                                journal_path.name,
                                dir_fd=journal_parent_descriptor,
                            )
                            os.fsync(journal_parent_descriptor)
                            journal_present = False
                        except OSError:
                            pass
                    for item in prepared:
                        try:
                            os.stat(
                                item.temporary.name,
                                dir_fd=item.parent_descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        if item.temporary in preserved:
                            continue
                        try:
                            os.unlink(
                                item.temporary.name,
                                dir_fd=item.parent_descriptor,
                            )
                            os.fsync(item.parent_descriptor)
                        except OSError:
                            preserved.append(item.temporary)
            else:
                preserved = self._rollback(committed)
            for item in prepared:
                if journal_present or item in committed or item.temporary in preserved:
                    continue
                try:
                    os.unlink(item.temporary.name, dir_fd=item.parent_descriptor)
                    os.fsync(item.parent_descriptor)
                except FileNotFoundError:
                    pass
            if not journal_present:
                for directory in reversed(created_directories):
                    try:
                        identity = self.directory_identities[directory]
                        parent = self.directory_identities[directory.parent]
                        self._remove_directory_at(
                            parent,
                            directory.name,
                            self._directory_record(identity),
                        )
                    except (BackfillError, KeyError):
                        pass
            if preserved:
                raise BackfillError("backfill apply failed; rollback data was preserved") from error
            raise
        else:
            for item in committed:
                try:
                    os.unlink(item.temporary.name, dir_fd=item.parent_descriptor)
                    os.fsync(item.parent_descriptor)
                except FileNotFoundError:
                    pass
            if journal_path is not None and journal_parent_descriptor >= 0:
                try:
                    os.unlink(journal_path.name, dir_fd=journal_parent_descriptor)
                    os.fsync(journal_parent_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            if signal_handler_installed:
                signal.signal(signal.SIGTERM, previous_sigterm)
            for item in prepared:
                try:
                    os.close(item.parent_descriptor)
                except OSError:
                    pass
            if journal_parent_descriptor >= 0:
                os.close(journal_parent_descriptor)


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
