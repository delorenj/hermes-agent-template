#!/usr/bin/env python3
"""Durable, idempotent state machine for one Hermes sentinel post-loop retro."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Iterator


SCHEMA_NAME = "hermes.run-retro"
SCHEMA_VERSION = 6
COMMENT_FINGERPRINT_VERSION = 5
ARTIFACT_DOMAIN = "hermes.run-retro.artifact"
COMMENT_DOMAIN = "hermes.run-retro.comment"
FINAL_STATUSES = {"posted", "already_present", "failed", "no_target_issue"}
SUCCESS_STATUSES = {"posted", "already_present"}
ALL_STATUSES = {"prepared", *FINAL_STATUSES}
FAILURE_CATEGORIES = {
    "lookup_failed",
    "post_failed",
    "response_unknown",
    "serialization_failed",
}
FIX_SCOPES = {"repo-local", "external", "template", "fleet"}
PROVIDERS = {"linear", "plane", "trello"}
SUMMARY_CATEGORIES = {
    "process",
    "tooling",
    "dependency",
    "review",
    "testing",
    "coordination",
    "environment",
    "documentation",
    "automation",
    "external_dependency",
    "other",
}
OMITTED_CATEGORIES = {
    "credentials",
    "tokens",
    "raw_logs",
    "customer_or_pii",
    "private_paths",
    "other_protected",
}
IMMUTABLE_FIELDS = (
    "schema",
    "schema_version",
    "artifact_fingerprint",
    "comment_fingerprint",
    "run_id",
    "correlation_id",
    "repo",
    "provider",
    "source_issue",
    "target_issue",
    "local_tracking_reference",
    "decisions",
    "protected_evidence_refs",
    "sanitization",
    "operator_action_required",
    "comment_fingerprint_marker",
    "comment_body",
    "recorded_at",
)
INPUT_DERIVED_IMMUTABLE_FIELDS = tuple(
    field for field in IMMUTABLE_FIELDS if field != "recorded_at"
)
ROOT_FIELDS = {*IMMUTABLE_FIELDS, "routing"}
ROUTING_FIELDS = {"status", "error_category", "error_summary", "updated_at"}
SAFE_REPO_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
INVOCATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
RFC_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}$"
)
TRELLO_ID_RE = re.compile(r"^[0-9a-f]{24}$")
SAFE_REF_RE = re.compile(
    r"^(?!\.\.(?:/|$))(?!.*\/\.\.(?:/|$))"
    r"(?:evidence:[A-Za-z0-9._:-]+|[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)$"
)
LOCAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MARKER_RE = re.compile(r"^\[run-retro-comment:[0-9a-f]{64}\]$")
SAFE_SIGNALS = (
    "slow_feedback",
    "manual_rework",
    "flaky_validation",
    "unclear_contract",
    "missing_capability",
    "dependency_delay",
    "coordination_gap",
    "environment_drift",
    "documentation_gap",
    "review_rework",
    "no_material_friction",
    "other_process_friction",
)
SAFE_ACTIONS = (
    "automate_check",
    "clarify_contract",
    "add_test",
    "improve_tooling",
    "update_documentation",
    "isolate_dependency",
    "tighten_review",
    "improve_coordination",
    "stabilize_environment",
    "retain_current_process",
    "operator_followup",
)
SAFE_SUMMARY_RE = re.compile(
    r"^signal=(?:"
    + "|".join(SAFE_SIGNALS)
    + r"); action=(?:"
    + "|".join(SAFE_ACTIONS)
    + r")$"
)
UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{6}Z$"
)
ERROR_SUMMARY_BY_CATEGORY = {
    "lookup_failed": "comment lookup failed; no post attempted",
    "post_failed": "comment post failed; retry ensure_comment",
    "response_unknown": "provider response not confirmed",
    "serialization_failed": "provider adapter unavailable",
}
RETRO_PARTS = ("_bmad-output", "implementation-artifacts", "run-retros")


class RetroError(Exception):
    """Safe-to-report retro state error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalized_scalar(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise RetroError("invalid_intent")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > max_length:
        raise RetroError("invalid_intent")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise RetroError("invalid_intent")
    return normalized


def _read_utf8_json(path: Path, *, category: str) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        path_metadata = os.lstat(path)
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(
            path_metadata.st_mode
        ):
            raise RetroError(category)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise RetroError(category)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
        return json.loads(payload.decode("utf-8"))
    except RetroError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise RetroError(category) from None
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def canonical_repo_identity(repo_root: Path) -> str:
    """Read only .project.json.project_name and make one exact ASCII identity."""
    document = _read_utf8_json(
        repo_root / ".project.json", category="invalid_repository_identity"
    )
    try:
        raw = document["project_name"]
    except (KeyError, TypeError):
        raise RetroError("invalid_repository_identity") from None
    if not isinstance(raw, str):
        raise RetroError("invalid_repository_identity")
    normalized = unicodedata.normalize("NFKC", raw).strip().casefold()
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError:
        raise RetroError("invalid_repository_identity") from None
    if not SAFE_REPO_RE.fullmatch(normalized):
        raise RetroError("invalid_repository_identity")
    return normalized


def canonical_provider(repo_root: Path) -> str:
    document = _read_utf8_json(repo_root / ".project.json", category="invalid_provider")
    try:
        raw = document["ticket_provider"]["type"]
    except (KeyError, TypeError):
        raise RetroError("invalid_provider") from None
    if not isinstance(raw, str):
        raise RetroError("invalid_provider")
    provider = unicodedata.normalize("NFKC", raw).strip().casefold()
    if provider not in PROVIDERS:
        raise RetroError("invalid_provider")
    return provider


def canonical_invocation_id(value: Any) -> str:
    normalized = _normalized_scalar(value, max_length=200)
    if not INVOCATION_RE.fullmatch(normalized):
        raise RetroError("invalid_intent")
    return normalized


def canonical_issue_id(provider: str, value: Any) -> str | None:
    if value is None:
        return None
    normalized = _normalized_scalar(value, max_length=64).casefold()
    if provider in {"plane", "linear"}:
        if not RFC_UUID_RE.fullmatch(normalized):
            raise RetroError("invalid_issue_identity")
        try:
            canonical = str(uuid.UUID(normalized))
        except (ValueError, AttributeError):
            raise RetroError("invalid_issue_identity") from None
        if canonical != normalized:
            raise RetroError("invalid_issue_identity")
        return canonical
    if provider == "trello" and TRELLO_ID_RE.fullmatch(normalized):
        return normalized
    raise RetroError("invalid_issue_identity")


def _safe_summary(value: Any) -> str:
    try:
        text = _normalized_scalar(value, max_length=200)
    except RetroError:
        raise RetroError("unsafe_summary") from None
    if not SAFE_SUMMARY_RE.fullmatch(text):
        raise RetroError("unsafe_summary")
    return text


def _local_tracking_reference(value: Any) -> str:
    text = _normalized_scalar(value, max_length=128)
    if not LOCAL_REF_RE.fullmatch(text):
        raise RetroError("invalid_intent")
    return text


def _categorized_summary(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"category", "summary"}:
        raise RetroError("invalid_intent")
    category = _normalized_scalar(value["category"], max_length=40).casefold()
    if category not in SUMMARY_CATEGORIES:
        raise RetroError("invalid_intent")
    return {"category": category, "summary": _safe_summary(value["summary"])}


def _protected_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise RetroError("invalid_intent")
    refs: list[str] = []
    for item in value:
        ref = _normalized_scalar(item, max_length=300)
        if not SAFE_REF_RE.fullmatch(ref):
            raise RetroError("unsafe_evidence_reference")
        refs.append(ref)
    if len(set(refs)) != len(refs):
        raise RetroError("invalid_intent")
    return refs


def _sanitization(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"status", "omitted_categories"}:
        raise RetroError("invalid_intent")
    if (
        value["status"] != "sanitized"
        or not isinstance(value["omitted_categories"], list)
        or len(value["omitted_categories"]) > len(OMITTED_CATEGORIES)
    ):
        raise RetroError("invalid_intent")
    omitted = []
    for category in value["omitted_categories"]:
        normalized = _normalized_scalar(category, max_length=40).casefold()
        if normalized not in OMITTED_CATEGORIES:
            raise RetroError("invalid_intent")
        omitted.append(normalized)
    if len(set(omitted)) != len(omitted):
        raise RetroError("invalid_intent")
    return {"status": "sanitized", "omitted_categories": omitted}


def _sha256_lines(lines: list[str]) -> str:
    preimage = "".join(f"{line}\n" for line in lines).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def artifact_fingerprint(repo: str, run_id: str) -> str:
    return _sha256_lines([ARTIFACT_DOMAIN, str(SCHEMA_VERSION), repo, run_id])


def comment_fingerprint(
    repo: str,
    provider: str,
    source_issue: str | None,
    decisions: dict[str, Any],
    operator_action_required: bool,
) -> str:
    hurt = decisions["what_hurt"]
    change = decisions["what_should_change"]
    return _sha256_lines(
        [
            COMMENT_DOMAIN,
            str(COMMENT_FINGERPRINT_VERSION),
            repo,
            provider,
            source_issue or "no_target_issue",
            hurt["category"],
            hurt["summary"],
            change["category"],
            change["summary"],
            decisions["fix_scope"],
            "true" if operator_action_required else "false",
        ]
    )


def _comment_body(
    decisions: dict[str, Any], operator_action_required: bool, marker: str
) -> str:
    operator = "yes" if operator_action_required else "no"
    return "\n".join(
        [
            "Post-loop improvement",
            (
                f"What hurt [{decisions['what_hurt']['category']}]: "
                f"{decisions['what_hurt']['summary']}"
            ),
            (
                f"What should change [{decisions['what_should_change']['category']}]: "
                f"{decisions['what_should_change']['summary']}"
            ),
            f"Fix scope: {decisions['fix_scope']}",
            f"Operator action required: {operator}",
            marker,
        ]
    )


def _build_prepared(repo_root: Path, raw: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "run_id",
        "correlation_id",
        "source_issue",
        "local_tracking_reference",
        "decisions",
        "protected_evidence_refs",
        "sanitization",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise RetroError("invalid_intent")
    repo = canonical_repo_identity(repo_root)
    provider = canonical_provider(repo_root)
    run_id = canonical_invocation_id(raw["run_id"])
    correlation_id = canonical_invocation_id(raw["correlation_id"])
    source_issue = canonical_issue_id(provider, raw["source_issue"])
    local_ref = (
        None
        if raw["local_tracking_reference"] is None
        else _local_tracking_reference(raw["local_tracking_reference"])
    )
    decisions_raw = raw["decisions"]
    if not isinstance(decisions_raw, dict) or set(decisions_raw) != {
        "what_hurt",
        "what_should_change",
        "fix_scope",
    }:
        raise RetroError("invalid_intent")
    fix_scope = _normalized_scalar(decisions_raw["fix_scope"], max_length=20).casefold()
    if fix_scope not in FIX_SCOPES:
        raise RetroError("invalid_intent")
    decisions = {
        "what_hurt": _categorized_summary(decisions_raw["what_hurt"]),
        "what_should_change": _categorized_summary(decisions_raw["what_should_change"]),
        "fix_scope": fix_scope,
    }
    fingerprint = artifact_fingerprint(repo, run_id)
    operator_required = source_issue is None or fix_scope != "repo-local"
    comment_hash = comment_fingerprint(
        repo, provider, source_issue, decisions, operator_required
    )
    marker = f"[run-retro-comment:{comment_hash}]"
    now = _utc_now()
    document = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "artifact_fingerprint": fingerprint,
        "comment_fingerprint": comment_hash,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "repo": repo,
        "provider": provider,
        "source_issue": source_issue,
        "target_issue": source_issue,
        "local_tracking_reference": local_ref,
        "decisions": decisions,
        "protected_evidence_refs": _protected_refs(raw["protected_evidence_refs"]),
        "sanitization": _sanitization(raw["sanitization"]),
        "operator_action_required": operator_required,
        "comment_fingerprint_marker": marker,
        "comment_body": _comment_body(decisions, operator_required, marker),
        "recorded_at": now,
        "routing": {
            "status": "prepared",
            "error_category": None,
            "error_summary": None,
            "updated_at": now,
        },
    }
    validate_document(document)
    return document


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, OverflowError):
        return False
    return (
        parsed.astimezone(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        == value
    )


def validate_document(
    document: Any, *, path: Path | None = None, require_final: bool = False
) -> None:
    if not isinstance(document, dict) or set(document) != ROOT_FIELDS:
        raise RetroError("invalid_artifact")
    if (
        document["schema"] != SCHEMA_NAME
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise RetroError("invalid_artifact")
    for key in ("artifact_fingerprint", "comment_fingerprint"):
        if not isinstance(document[key], str) or not SHA256_RE.fullmatch(document[key]):
            raise RetroError("invalid_artifact")
    repo = document["repo"]
    if not isinstance(repo, str) or not SAFE_REPO_RE.fullmatch(repo):
        raise RetroError("invalid_artifact")
    provider = document["provider"]
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise RetroError("invalid_artifact")
    run_id = canonical_invocation_id(document["run_id"])
    if run_id != document["run_id"]:
        raise RetroError("invalid_artifact")
    correlation_id = canonical_invocation_id(document["correlation_id"])
    if correlation_id != document["correlation_id"]:
        raise RetroError("invalid_artifact")
    source = canonical_issue_id(provider, document["source_issue"])
    target = canonical_issue_id(provider, document["target_issue"])
    if source != document["source_issue"] or target != document["target_issue"]:
        raise RetroError("invalid_artifact")
    if target != source:
        raise RetroError("wrong_comment_target")
    if document["artifact_fingerprint"] != artifact_fingerprint(repo, run_id):
        raise RetroError("invalid_artifact")
    decisions = document["decisions"]
    if not isinstance(decisions, dict) or set(decisions) != {
        "what_hurt",
        "what_should_change",
        "fix_scope",
    }:
        raise RetroError("invalid_artifact")
    normalized_decisions = {
        "what_hurt": _categorized_summary(decisions["what_hurt"]),
        "what_should_change": _categorized_summary(decisions["what_should_change"]),
        "fix_scope": decisions["fix_scope"],
    }
    if (
        normalized_decisions != decisions
        or not isinstance(decisions["fix_scope"], str)
        or decisions["fix_scope"] not in FIX_SCOPES
    ):
        raise RetroError("invalid_artifact")
    operator = document["operator_action_required"]
    if not isinstance(operator, bool):
        raise RetroError("invalid_artifact")
    expected_operator = source is None or decisions["fix_scope"] != "repo-local"
    if operator != expected_operator:
        raise RetroError("invalid_artifact")
    expected_comment = comment_fingerprint(
        repo, provider, source, decisions, expected_operator
    )
    marker = f"[run-retro-comment:{expected_comment}]"
    if (
        document["comment_fingerprint"] != expected_comment
        or document["comment_fingerprint_marker"] != marker
        or not MARKER_RE.fullmatch(document["comment_fingerprint_marker"])
    ):
        raise RetroError("invalid_artifact")
    expected_body = _comment_body(decisions, expected_operator, marker)
    if document["comment_body"] != expected_body or expected_body.count(marker) != 1:
        raise RetroError("invalid_artifact")
    if document["local_tracking_reference"] is not None:
        if (
            _local_tracking_reference(document["local_tracking_reference"])
            != document["local_tracking_reference"]
        ):
            raise RetroError("invalid_artifact")
    if (
        _protected_refs(document["protected_evidence_refs"])
        != document["protected_evidence_refs"]
    ):
        raise RetroError("invalid_artifact")
    if _sanitization(document["sanitization"]) != document["sanitization"]:
        raise RetroError("invalid_artifact")
    if not _valid_timestamp(document["recorded_at"]):
        raise RetroError("invalid_artifact")
    routing = document["routing"]
    if not isinstance(routing, dict) or set(routing) != ROUTING_FIELDS:
        raise RetroError("invalid_artifact")
    status_value = routing["status"]
    if (
        not isinstance(status_value, str)
        or status_value not in ALL_STATUSES
        or (require_final and status_value not in FINAL_STATUSES)
    ):
        raise RetroError("invalid_artifact")
    if not _valid_timestamp(routing["updated_at"]):
        raise RetroError("invalid_artifact")
    error_category = routing["error_category"]
    error_summary = routing["error_summary"]
    if status_value == "failed":
        if (
            not isinstance(error_category, str)
            or error_category not in FAILURE_CATEGORIES
        ):
            raise RetroError("invalid_artifact")
        if error_summary != ERROR_SUMMARY_BY_CATEGORY[error_category]:
            raise RetroError("invalid_artifact")
    elif error_category is not None or error_summary is not None:
        raise RetroError("invalid_artifact")
    if source is None:
        if status_value not in {"prepared", "no_target_issue"}:
            raise RetroError("invalid_artifact")
    elif status_value == "no_target_issue":
        raise RetroError("invalid_artifact")
    if path is not None and path.name != f"{document['artifact_fingerprint']}.json":
        raise RetroError("invalid_artifact")


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_directory_component(parent_fd: int, name: str, *, create: bool) -> int:
    if not name or "/" in name or name in {".", ".."}:
        raise RetroError("unsafe_artifact_path")
    created = False
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise RetroError("unsafe_artifact_path") from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError:
            raise RetroError("durability_failed") from None
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    if created:
        os.fsync(parent_fd)
    return descriptor


def _walk_directories(parent_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current = os.dup(parent_fd)
    try:
        for part in parts:
            child = _open_directory_component(current, part, create=create)
            os.close(current)
            current = child
        return current
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(current)
        raise


@dataclass
class RetroStore:
    repo_root: Path
    repo_fd: int
    retro_fd: int
    retro_identity: tuple[int, int]

    def artifact_name(self, fingerprint: str) -> str:
        if not SHA256_RE.fullmatch(fingerprint):
            raise RetroError("invalid_artifact")
        return f"{fingerprint}.json"

    def relative_path(self, fingerprint: str) -> str:
        return "/".join((*RETRO_PARTS, self.artifact_name(fingerprint)))


@contextlib.contextmanager
def _retro_store(repo_root: Path, *, create: bool) -> Iterator[RetroStore]:
    try:
        repo_fd = os.open(repo_root, _directory_flags())
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    retro_fd = -1
    try:
        retro_fd = _walk_directories(repo_fd, RETRO_PARTS, create=create)
        metadata = os.fstat(retro_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RetroError("unsafe_artifact_path")
        yield RetroStore(
            repo_root=repo_root,
            repo_fd=repo_fd,
            retro_fd=retro_fd,
            retro_identity=(metadata.st_dev, metadata.st_ino),
        )
    finally:
        if retro_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(retro_fd)
        with contextlib.suppress(OSError):
            os.close(repo_fd)


def _assert_store_path(store: RetroStore) -> None:
    current = _walk_directories(store.repo_fd, RETRO_PARTS, create=False)
    try:
        metadata = os.fstat(current)
        if (metadata.st_dev, metadata.st_ino) != store.retro_identity:
            raise RetroError("unsafe_artifact_path")
    finally:
        os.close(current)


def _read_utf8_json_at(
    directory_fd: int, name: str, *, category: str, missing_ok: bool = False
) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetroError("unsafe_artifact_path")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
        return json.loads(payload.decode("utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RetroError(category) from None
    except RetroError:
        raise
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise RetroError("unsafe_artifact_path") from None
        raise RetroError(category) from None
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise RetroError(category) from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _read_artifact_at(
    store: RetroStore,
    fingerprint: str,
    *,
    require_final: bool = False,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    name = store.artifact_name(fingerprint)
    document = _read_utf8_json_at(
        store.retro_fd,
        name,
        category="invalid_artifact",
        missing_ok=missing_ok,
    )
    if document is None:
        return None
    validate_document(
        document,
        path=Path(name),
        require_final=require_final,
    )
    return document


def read_artifact(path: Path, *, require_final: bool = False) -> dict[str, Any]:
    try:
        directory_fd = os.open(path.parent, _directory_flags())
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    try:
        document = _read_utf8_json_at(
            directory_fd, path.name, category="invalid_artifact"
        )
        validate_document(document, path=Path(path.name), require_final=require_final)
        return document
    finally:
        os.close(directory_fd)


def _fsync_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise RetroError("unsafe_artifact_path")
    os.fsync(descriptor)


def _fsync_file(directory_fd: int, name: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RetroError("unsafe_artifact_path")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_temp_name(final_name: str) -> str:
    return f".{final_name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"


def _write_exclusive_temp(
    directory_fd: int, final_name: str, document: dict[str, Any]
) -> str:
    temporary = _unique_temp_name(final_name)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(_canonical_json(document))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    return temporary


def _validate_temp(directory_fd: int, temporary: str, final_name: str) -> None:
    document = _read_utf8_json_at(directory_fd, temporary, category="invalid_artifact")
    validate_document(document, path=Path(final_name))


def _durable_create(
    store: RetroStore, fingerprint: str, document: dict[str, Any]
) -> None:
    name = store.artifact_name(fingerprint)
    _assert_store_path(store)
    temporary = _write_exclusive_temp(store.retro_fd, name, document)
    try:
        _validate_temp(store.retro_fd, temporary, name)
        _assert_store_path(store)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=store.retro_fd,
                dst_dir_fd=store.retro_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise RetroError("artifact_conflict") from None
        _fsync_file(store.retro_fd, name)
        _fsync_directory(store.retro_fd)
        os.unlink(temporary, dir_fd=store.retro_fd)
        _fsync_directory(store.retro_fd)
        _read_artifact_at(store, fingerprint)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=store.retro_fd)


def _durable_replace(
    store: RetroStore, fingerprint: str, document: dict[str, Any]
) -> None:
    name = store.artifact_name(fingerprint)
    _assert_store_path(store)
    _read_artifact_at(store, fingerprint)
    temporary = _write_exclusive_temp(store.retro_fd, name, document)
    try:
        _validate_temp(store.retro_fd, temporary, name)
        _assert_store_path(store)
        os.replace(
            temporary,
            name,
            src_dir_fd=store.retro_fd,
            dst_dir_fd=store.retro_fd,
        )
        _fsync_file(store.retro_fd, name)
        _fsync_directory(store.retro_fd)
        _read_artifact_at(store, fingerprint)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=store.retro_fd)


@contextlib.contextmanager
def _safe_lock(directory_fd: int, name: str) -> Iterator[int]:
    if not re.fullmatch(r"[0-9a-f]{64}\.lock", name):
        raise RetroError("unsafe_artifact_path")
    base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(
            name,
            base_flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(name, base_flags, dir_fd=directory_fd)
        except OSError:
            raise RetroError("unsafe_artifact_path") from None
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RetroError("unsafe_artifact_path")
        if created:
            os.fsync(descriptor)
            _fsync_directory(directory_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _artifact_lock(store: RetroStore, fingerprint: str) -> Iterator[None]:
    locks = _walk_directories(store.retro_fd, (".locks", "artifacts"), create=True)
    try:
        with _safe_lock(locks, f"{fingerprint}.lock"):
            yield
    finally:
        os.close(locks)


@contextlib.contextmanager
def _comment_lock(store: RetroStore, document: dict[str, Any]) -> Iterator[int]:
    locks = _walk_directories(store.retro_fd, (".locks", "comments"), create=True)
    key = _sha256_lines(
        [
            document["provider"],
            document["source_issue"] or "no_target_issue",
            document["comment_fingerprint_marker"],
        ]
    )
    try:
        with _safe_lock(locks, f"{key}.lock") as descriptor:
            yield descriptor
    finally:
        os.close(locks)


def _immutable_view(
    document: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, Any]:
    return {field: document[field] for field in fields}


def prepare(repo_root: Path, intent: dict[str, Any]) -> dict[str, Any]:
    proposed = _build_prepared(repo_root, intent)
    fingerprint = proposed["artifact_fingerprint"]
    with _retro_store(repo_root, create=True) as store:
        with _artifact_lock(store, fingerprint):
            _assert_store_path(store)
            stored = _read_artifact_at(store, fingerprint, missing_ok=True)
            if stored is not None:
                if _immutable_view(
                    stored, INPUT_DERIVED_IMMUTABLE_FIELDS
                ) != _immutable_view(proposed, INPUT_DERIVED_IMMUTABLE_FIELDS):
                    raise RetroError("immutable_intent_mismatch")
                outcome = "reused"
            else:
                _durable_create(store, fingerprint, proposed)
                stored = _read_artifact_at(store, fingerprint)
                outcome = "prepared"
        artifact_path = store.relative_path(fingerprint)
    if stored is None:
        raise RetroError("invalid_artifact")
    return {
        "status": outcome,
        "artifact_fingerprint": fingerprint,
        "artifact_path": artifact_path,
        "comment_fingerprint_marker": stored["comment_fingerprint_marker"],
        "routing_status": stored["routing"]["status"],
    }


def _validated_result(stored: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {
        "provider",
        "status",
        "target_issue",
        "error_category",
        "error_summary",
    }:
        raise RetroError("invalid_routing_result")
    if result["provider"] != stored["provider"]:
        raise RetroError("wrong_comment_provider")
    target = canonical_issue_id(stored["provider"], result["target_issue"])
    if (
        target != result["target_issue"]
        or target != stored["source_issue"]
        or target != stored["target_issue"]
    ):
        raise RetroError("wrong_comment_target")
    status_value = result["status"]
    if not isinstance(status_value, str) or status_value not in FINAL_STATUSES:
        raise RetroError("invalid_routing_result")
    error_category = result["error_category"]
    error_summary = result["error_summary"]
    if status_value == "failed":
        if (
            not isinstance(error_category, str)
            or error_category not in FAILURE_CATEGORIES
        ):
            raise RetroError("invalid_routing_result")
        if error_summary is not None and not isinstance(error_summary, str):
            raise RetroError("invalid_routing_result")
        error_summary = ERROR_SUMMARY_BY_CATEGORY[error_category]
    elif error_category is not None or error_summary is not None:
        raise RetroError("invalid_routing_result")
    if stored["source_issue"] is None and status_value != "no_target_issue":
        raise RetroError("wrong_comment_target")
    if stored["source_issue"] is not None and status_value == "no_target_issue":
        raise RetroError("wrong_comment_target")
    return {
        "provider": stored["provider"],
        "status": status_value,
        "target_issue": target,
        "error_category": error_category,
        "error_summary": error_summary,
    }


def finalize(
    repo_root: Path, fingerprint: str, result: dict[str, Any]
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(fingerprint):
        raise RetroError("invalid_artifact")
    with _retro_store(repo_root, create=False) as store:
        with _artifact_lock(store, fingerprint):
            _assert_store_path(store)
            stored = _read_artifact_at(store, fingerprint)
            if stored is None:
                raise RetroError("invalid_artifact")
            normalized = _validated_result(stored, result)
            stored_status = stored["routing"]["status"]
            incoming_status = normalized["status"]
            if stored_status in SUCCESS_STATUSES or stored_status == "no_target_issue":
                return {
                    "status": stored_status,
                    "artifact_fingerprint": fingerprint,
                    "artifact_path": store.relative_path(fingerprint),
                    "transition": "preserved_terminal",
                }
            updated = dict(stored)
            updated["routing"] = {
                "status": incoming_status,
                "error_category": normalized["error_category"],
                "error_summary": normalized["error_summary"],
                "updated_at": _utc_now(),
            }
            if _immutable_view(updated, IMMUTABLE_FIELDS) != _immutable_view(
                stored, IMMUTABLE_FIELDS
            ):
                raise RetroError("immutable_intent_mismatch")
            validate_document(
                updated,
                path=Path(store.artifact_name(fingerprint)),
                require_final=True,
            )
            _durable_replace(store, fingerprint, updated)
            _read_artifact_at(store, fingerprint, require_final=True)
        artifact_path = store.relative_path(fingerprint)
    return {
        "status": incoming_status,
        "artifact_fingerprint": fingerprint,
        "artifact_path": artifact_path,
        "transition": "updated",
    }


def comment_body(document: dict[str, Any]) -> str:
    validate_document(document)
    return document["comment_body"]


def _default_providers_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "providers"


def _provider_failure(
    stored: dict[str, Any], category: str, summary: str
) -> dict[str, Any]:
    return {
        "provider": stored["provider"],
        "status": "failed",
        "target_issue": stored["source_issue"],
        "error_category": category,
        "error_summary": summary,
    }


def _invoke_provider(
    stored: dict[str, Any], providers_dir: Path, lock_descriptor: int
) -> dict[str, Any]:
    provider = stored["provider"]
    implementation = providers_dir / f"{provider}.sh"
    try:
        metadata = os.stat(implementation, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
    except OSError:
        return _provider_failure(
            stored, "serialization_failed", "provider adapter unavailable"
        )
    environment = dict(os.environ)
    environment["TICKET_PROVIDER"] = provider
    try:
        completed = subprocess.run(
            [
                "sh",
                str(implementation),
                "ensure_comment",
                stored["source_issue"],
                stored["comment_fingerprint_marker"],
                stored["comment_body"],
            ],
            check=False,
            capture_output=True,
            text=False,
            env=environment,
            timeout=120,
            pass_fds=(lock_descriptor,),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _provider_failure(
            stored, "response_unknown", "provider response not confirmed"
        )
    if completed.returncode != 0:
        return _provider_failure(
            stored, "response_unknown", "provider response not confirmed"
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
        return _validated_result(stored, result)
    except (UnicodeError, json.JSONDecodeError, RetroError, TypeError, ValueError):
        return _provider_failure(
            stored, "response_unknown", "provider response not confirmed"
        )


def deliver(
    repo_root: Path, fingerprint: str, *, providers_dir: Path | None = None
) -> dict[str, Any]:
    """Deliver only the immutable provider/source/body bound by prepare."""
    if not SHA256_RE.fullmatch(fingerprint):
        raise RetroError("invalid_artifact")
    with _retro_store(repo_root, create=False) as store:
        stored = _read_artifact_at(store, fingerprint)
        if stored is None:
            raise RetroError("invalid_artifact")
        if stored["source_issue"] is None:
            no_target = {
                "provider": stored["provider"],
                "status": "no_target_issue",
                "target_issue": None,
                "error_category": None,
                "error_summary": None,
            }
        else:
            no_target = None
        if no_target is None:
            with _comment_lock(store, stored) as lock_descriptor:
                _assert_store_path(store)
                current = _read_artifact_at(store, fingerprint)
                if current is None:
                    raise RetroError("invalid_artifact")
                if _immutable_view(current, IMMUTABLE_FIELDS) != _immutable_view(
                    stored, IMMUTABLE_FIELDS
                ):
                    raise RetroError("immutable_intent_mismatch")
                if current["routing"]["status"] in SUCCESS_STATUSES:
                    return {
                        "status": current["routing"]["status"],
                        "artifact_fingerprint": fingerprint,
                        "artifact_path": store.relative_path(fingerprint),
                        "transition": "preserved_terminal",
                    }
                result = _invoke_provider(
                    current,
                    providers_dir or _default_providers_dir(),
                    lock_descriptor,
                )
                return finalize(repo_root, fingerprint, result)
    return finalize(repo_root, fingerprint, no_target)


def _load_input(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            payload = sys.stdin.buffer.read()
            value = json.loads(payload.decode("utf-8"))
        else:
            value = _read_utf8_json(Path(path), category="invalid_input")
    except RetroError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RetroError("invalid_input") from None
    if not isinstance(value, dict):
        raise RetroError("invalid_input")
    return value


def _read_cli_artifact(
    repo_root: Path, fingerprint: str, *, require_final: bool = False
) -> dict[str, Any]:
    with _retro_store(repo_root, create=False) as store:
        document = _read_artifact_at(store, fingerprint, require_final=require_final)
        if document is None:
            raise RetroError("invalid_artifact")
        return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--repo-root", required=True, type=Path)
    prepare_parser.add_argument("--intent", required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--repo-root", required=True, type=Path)
    finalize_parser.add_argument("--artifact-fingerprint", required=True)
    finalize_parser.add_argument("--result", required=True)
    deliver_parser = subparsers.add_parser("deliver")
    deliver_parser.add_argument("--repo-root", required=True, type=Path)
    deliver_parser.add_argument("--artifact-fingerprint", required=True)
    deliver_parser.add_argument("--providers-dir", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--repo-root", required=True, type=Path)
    validate_parser.add_argument("--artifact-fingerprint", required=True)
    validate_parser.add_argument("--final", action="store_true")
    comment_parser = subparsers.add_parser("comment-body")
    comment_parser.add_argument("--repo-root", required=True, type=Path)
    comment_parser.add_argument("--artifact-fingerprint", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo_root = args.repo_root.resolve()
        if args.command == "prepare":
            result = prepare(repo_root, _load_input(args.intent))
            print(json.dumps(result, sort_keys=True))
        elif args.command == "finalize":
            result = finalize(
                repo_root,
                args.artifact_fingerprint,
                _load_input(args.result),
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "deliver":
            result = deliver(
                repo_root,
                args.artifact_fingerprint,
                providers_dir=args.providers_dir,
            )
            print(json.dumps(result, sort_keys=True))
        elif args.command == "validate":
            document = _read_cli_artifact(
                repo_root,
                args.artifact_fingerprint,
                require_final=args.final,
            )
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "artifact_fingerprint": document["artifact_fingerprint"],
                        "routing_status": document["routing"]["status"],
                    },
                    sort_keys=True,
                )
            )
        else:
            print(
                comment_body(_read_cli_artifact(repo_root, args.artifact_fingerprint))
            )
    except RetroError as error:
        print(
            json.dumps(
                {"status": "stalled", "error_category": error.category},
                sort_keys=True,
            )
        )
        return 3
    except (OSError, UnicodeError, ValueError, TypeError):
        print(
            json.dumps(
                {"status": "stalled", "error_category": "durability_failed"},
                sort_keys=True,
            )
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
