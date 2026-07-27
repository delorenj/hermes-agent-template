#!/usr/bin/env python3
"""Durable, idempotent state machine for one Hermes sentinel post-loop retro."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Iterator


SCHEMA_NAME = "hermes.run-retro"
SCHEMA_VERSION = 8
COMMENT_FINGERPRINT_VERSION = 6
ARTIFACT_DOMAIN = "hermes.run-retro.artifact"
COMMENT_DOMAIN = "hermes.run-retro.comment"
FINAL_STATUSES = {"posted", "already_present", "failed", "no_target_issue"}
SUCCESS_STATUSES = {"posted", "already_present"}
CHECKPOINT_STATUSES = {*SUCCESS_STATUSES, "no_target_issue"}
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
    "run_id",
    "correlation_id",
    "repo",
    "provider",
    "source_issue",
    "local_tracking_reference",
    "decisions",
    "protected_evidence_refs",
    "sanitization",
    "operator_action_required",
)
INPUT_DERIVED_IMMUTABLE_FIELDS = IMMUTABLE_FIELDS
ROOT_FIELDS = {*IMMUTABLE_FIELDS, "routing"}
ROUTING_FIELDS = {"status", "error_category", "updated_at_epoch_us", "proof"}
DELIVERY_PROOF_FIELDS = {"status", "transition_id"}
BINDING_FIELDS = {
    "schema",
    "schema_version",
    "immutable_sha256",
    "final_document_sha256",
    "transition_id",
}
BINDING_SCHEMA = "hermes.run-retro.binding"
BINDING_SCHEMA_VERSION = 1
SAFE_REPO_RE = re.compile(
    r"^(?![a-z0-9._-]*(?:(?:xox[a-z]?)-|(?:sk|pk|rk)_(?:live|test)_|"
    r"aiza|(?:akia|asia)[a-z0-9]|(?:gh[pousr])_))"
    r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$(?![\s\S])"
)
RFC_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12}$(?![\s\S])"
)
TRELLO_ID_RE = re.compile(r"^[0-9a-f]{24}$(?![\s\S])")
SAFE_REF_RE = re.compile(
    r"^evidence:[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$(?![\s\S])"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$(?![\s\S])")
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
    + r")$(?![\s\S])"
)
ERROR_SUMMARY_BY_CATEGORY = {
    "lookup_failed": "comment lookup failed; no post attempted",
    "post_failed": "comment post failed; retry ensure_comment",
    "response_unknown": "provider response not confirmed",
    "serialization_failed": "provider adapter unavailable",
}
RETRO_PARTS = ("_bmad-output", "implementation-artifacts", "run-retros")
MAX_INPUT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 64 * 1024
PROVIDER_TIMEOUT_SECONDS = 120.0
LOCK_ACQUISITION_TIMEOUT_SECONDS = 5.0
LOCK_ACQUISITION_MAX_SECONDS = 300.0
LOCK_RETRY_SECONDS = 0.01
SUPERVISOR_SHUTDOWN_SECONDS = 2.0
MAX_EPOCH_US = 253402300799999999
GLOBAL_COMMENT_LOCK_NAMESPACE = b"\0hermes.run-retro.comment-lock.v1."
LINUX_CONTAINMENT_CANDIDATES = (Path("/usr/bin/bwrap"), Path("/bin/bwrap"))


class RetroError(Exception):
    """Safe-to-report retro state error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _bounded_seconds(
    value: Any,
    *,
    category: str,
    maximum: float,
) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RetroError(category) from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > maximum:
        raise RetroError(category)
    return seconds


def _lock_timeout_seconds() -> float:
    return _bounded_seconds(
        os.environ.get(
            "HERMES_LOCK_TIMEOUT_SECONDS",
            str(LOCK_ACQUISITION_TIMEOUT_SECONDS),
        ),
        category="lock_timeout",
        maximum=LOCK_ACQUISITION_MAX_SECONDS,
    )


def _acquire_flock(descriptor: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RetroError("lock_timeout") from None
            time.sleep(min(LOCK_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))


def _comment_lock_name(marker: str) -> bytes:
    match = re.fullmatch(r"\[run-retro-comment:([0-9a-f]{64})\]", marker)
    if match is None:
        raise RetroError("invalid_artifact")
    name = GLOBAL_COMMENT_LOCK_NAMESPACE + match.group(1).encode("ascii")
    if len(name) > 107:
        raise RetroError("invalid_artifact")
    return name


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _epoch_us_now() -> int:
    return time.time_ns() // 1_000


def _read_bounded(handle: Any, limit: int, *, overflow_category: str) -> bytes:
    payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise RetroError(overflow_category)
    return payload


def _normalized_scalar(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise RetroError("invalid_intent")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > max_length:
        raise RetroError("invalid_intent")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise RetroError("invalid_intent")
    return normalized


def _read_utf8_json(
    path: Path,
    *,
    category: str,
    max_bytes: int = MAX_INPUT_BYTES,
    overflow_category: str = "input_too_large",
) -> Any:
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
            payload = _read_bounded(
                handle,
                max_bytes,
                overflow_category=overflow_category,
            )
        return json.loads(payload.decode("utf-8"))
    except RetroError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        raise RetroError(category) from None
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _canonical_repo_from_project(document: Any) -> str:
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


def _canonical_provider_from_project(document: Any) -> str:
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
    normalized = _normalized_scalar(value, max_length=36).casefold()
    if not RFC_UUID_RE.fullmatch(normalized):
        raise RetroError("invalid_intent")
    try:
        canonical = str(uuid.UUID(normalized))
    except (ValueError, AttributeError):
        raise RetroError("invalid_intent") from None
    if canonical != normalized:
        raise RetroError("invalid_intent")
    return canonical


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
        ref = _normalized_scalar(item, max_length=45).casefold()
        if not SAFE_REF_RE.fullmatch(ref):
            raise RetroError("unsafe_evidence_reference")
        canonical = str(uuid.UUID(ref.removeprefix("evidence:")))
        if ref != f"evidence:{canonical}":
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


def _document_fingerprint(document: dict[str, Any]) -> str:
    return artifact_fingerprint(document["repo"], document["run_id"])


def _document_comment_fingerprint(document: dict[str, Any]) -> str:
    return comment_fingerprint(
        document["repo"],
        document["provider"],
        document["source_issue"],
        document["decisions"],
        document["operator_action_required"],
    )


def comment_marker(document: dict[str, Any]) -> str:
    return f"[run-retro-comment:{_document_comment_fingerprint(document)}]"


def _build_prepared(
    repo: str,
    provider: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
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
    run_id = canonical_invocation_id(raw["run_id"])
    correlation_id = canonical_invocation_id(raw["correlation_id"])
    source_issue = canonical_issue_id(provider, raw["source_issue"])
    local_ref = (
        None
        if raw["local_tracking_reference"] is None
        else canonical_issue_id(provider, raw["local_tracking_reference"])
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
    operator_required = source_issue is None or fix_scope != "repo-local"
    now = _epoch_us_now()
    document = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "correlation_id": correlation_id,
        "repo": repo,
        "provider": provider,
        "source_issue": source_issue,
        "local_tracking_reference": local_ref,
        "decisions": decisions,
        "protected_evidence_refs": _protected_refs(raw["protected_evidence_refs"]),
        "sanitization": _sanitization(raw["sanitization"]),
        "operator_action_required": operator_required,
        "routing": {
            "status": "prepared",
            "error_category": None,
            "updated_at_epoch_us": now,
            "proof": {
                "status": "unverified",
                "transition_id": None,
            },
        },
    }
    validate_document(document)
    return document


def _valid_epoch_us(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= MAX_EPOCH_US
    return (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        and 0 <= value <= MAX_EPOCH_US
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
    if source != document["source_issue"]:
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
    if document["local_tracking_reference"] is not None:
        if (
            canonical_issue_id(provider, document["local_tracking_reference"])
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
    routing = document["routing"]
    if not isinstance(routing, dict) or set(routing) != ROUTING_FIELDS:
        raise RetroError("invalid_artifact")
    status_value = routing["status"]
    if (
        not isinstance(status_value, str)
        or status_value not in ALL_STATUSES
        or (require_final and status_value not in CHECKPOINT_STATUSES)
    ):
        raise RetroError("invalid_artifact")
    if not _valid_epoch_us(routing["updated_at_epoch_us"]):
        raise RetroError("invalid_artifact")
    error_category = routing["error_category"]
    if status_value == "failed":
        if (
            not isinstance(error_category, str)
            or error_category not in FAILURE_CATEGORIES
        ):
            raise RetroError("invalid_artifact")
    elif error_category is not None:
        raise RetroError("invalid_artifact")
    proof = routing["proof"]
    if not isinstance(proof, dict) or set(proof) != DELIVERY_PROOF_FIELDS:
        raise RetroError("invalid_artifact")
    if status_value in CHECKPOINT_STATUSES:
        if proof["status"] != "verified":
            raise RetroError("invalid_artifact")
        transition_id = canonical_invocation_id(proof["transition_id"])
        if transition_id != proof["transition_id"]:
            raise RetroError("invalid_artifact")
    elif proof != {"status": "unverified", "transition_id": None}:
        raise RetroError("invalid_artifact")
    if source is None:
        if status_value not in {"prepared", "no_target_issue"}:
            raise RetroError("invalid_artifact")
    elif status_value == "no_target_issue":
        raise RetroError("invalid_artifact")
    if path is not None and path.name != f"{_document_fingerprint(document)}.json":
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
class RepositorySession:
    repo_root: Path
    parent_fd: int
    repo_name: str
    repo_fd: int
    repo_identity: tuple[int, int]
    repo: str
    provider: str
    provider_config: dict[str, Any]


@dataclass
class RetroStore:
    repo_root: Path
    parent_fd: int
    repo_name: str
    repo_identity: tuple[int, int]
    repo_fd: int
    retro_fd: int
    retro_identity: tuple[int, int]

    def artifact_name(self, fingerprint: str) -> str:
        if not SHA256_RE.fullmatch(fingerprint):
            raise RetroError("invalid_artifact")
        return f"{fingerprint}.json"

    def relative_path(self, fingerprint: str) -> str:
        return "/".join((*RETRO_PARTS, self.artifact_name(fingerprint)))

    def repo_relative(self, name: str) -> str:
        if (
            not name
            or name in {".", ".."}
            or name.startswith("/")
            or any(part in {".", ".."} for part in Path(name).parts)
        ):
            raise RetroError("unsafe_artifact_path")
        return "/".join((*RETRO_PARTS, name))


@contextlib.contextmanager
def _retro_store(
    repository: RepositorySession, *, create: bool
) -> Iterator[RetroStore]:
    retro_fd = -1
    try:
        retro_fd = _walk_directories(
            repository.repo_fd,
            RETRO_PARTS,
            create=create,
        )
        metadata = os.fstat(retro_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RetroError("unsafe_artifact_path")
        yield RetroStore(
            repo_root=repository.repo_root,
            parent_fd=repository.parent_fd,
            repo_name=repository.repo_name,
            repo_identity=repository.repo_identity,
            repo_fd=repository.repo_fd,
            retro_fd=retro_fd,
            retro_identity=(metadata.st_dev, metadata.st_ino),
        )
    finally:
        if retro_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(retro_fd)


def _assert_repository_path(repository: RepositorySession | RetroStore) -> None:
    current = -1
    try:
        current = os.open(
            repository.repo_name,
            _directory_flags(),
            dir_fd=repository.parent_fd,
        )
        metadata = os.fstat(current)
        if (metadata.st_dev, metadata.st_ino) != repository.repo_identity:
            raise RetroError("unsafe_artifact_path")
    except RetroError:
        raise
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    finally:
        if current >= 0:
            os.close(current)


def _assert_store_path(store: RetroStore) -> None:
    _assert_repository_path(store)
    current = _walk_directories(store.repo_fd, RETRO_PARTS, create=False)
    try:
        metadata = os.fstat(current)
        if (metadata.st_dev, metadata.st_ino) != store.retro_identity:
            raise RetroError("unsafe_artifact_path")
    finally:
        os.close(current)


def _read_utf8_json_at(
    directory_fd: int,
    name: str,
    *,
    category: str,
    missing_ok: bool = False,
    max_bytes: int = MAX_ARTIFACT_BYTES,
    overflow_category: str = "artifact_too_large",
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
            payload = _read_bounded(
                handle,
                max_bytes,
                overflow_category=overflow_category,
            )
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


@contextlib.contextmanager
def _repository(repo_root: Path) -> Iterator[RepositorySession]:
    repo_root = Path(os.path.abspath(repo_root))
    parent_fd = -1
    repo_fd = -1
    try:
        parent_fd = os.open(repo_root.parent, _directory_flags())
        repo_fd = os.open(repo_root.name, _directory_flags(), dir_fd=parent_fd)
    except OSError:
        if parent_fd >= 0:
            os.close(parent_fd)
        raise RetroError("unsafe_artifact_path") from None
    try:
        metadata = os.fstat(repo_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RetroError("unsafe_artifact_path")
        project = _read_utf8_json_at(
            repo_fd,
            ".project.json",
            category="invalid_repository_identity",
            max_bytes=MAX_INPUT_BYTES,
            overflow_category="configuration_too_large",
        )
        try:
            provider_config = project["ticket_provider"]
        except (KeyError, TypeError):
            raise RetroError("invalid_provider") from None
        if not isinstance(provider_config, dict):
            raise RetroError("invalid_provider")
        yield RepositorySession(
            repo_root=repo_root,
            parent_fd=parent_fd,
            repo_name=repo_root.name,
            repo_fd=repo_fd,
            repo_identity=(metadata.st_dev, metadata.st_ino),
            repo=_canonical_repo_from_project(project),
            provider=_canonical_provider_from_project(project),
            provider_config=provider_config,
        )
    finally:
        if repo_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(repo_fd)
        if parent_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(parent_fd)


def canonical_repo_identity(repo_root: Path) -> str:
    with _repository(repo_root) as repository:
        return repository.repo


def canonical_provider(repo_root: Path) -> str:
    with _repository(repo_root) as repository:
        return repository.provider


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
    _validate_binding(
        store,
        fingerprint,
        document,
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
        fingerprint = path.name.removesuffix(".json")
        bindings_fd = _walk_directories(directory_fd, (".bindings",), create=False)
        try:
            _validate_binding_at(
                bindings_fd,
                fingerprint,
                document,
                require_final=require_final,
            )
        finally:
            os.close(bindings_fd)
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


def _immutable_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(_immutable_view(document, IMMUTABLE_FIELDS))
    ).hexdigest()


def _document_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def _prepared_binding(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": BINDING_SCHEMA,
        "schema_version": BINDING_SCHEMA_VERSION,
        "immutable_sha256": _immutable_digest(document),
        "final_document_sha256": None,
        "transition_id": None,
    }


def _final_binding(document: dict[str, Any]) -> dict[str, Any]:
    transition_id = document["routing"]["proof"]["transition_id"]
    if not isinstance(transition_id, str):
        raise RetroError("invalid_artifact")
    return {
        "schema": BINDING_SCHEMA,
        "schema_version": BINDING_SCHEMA_VERSION,
        "immutable_sha256": _immutable_digest(document),
        "final_document_sha256": _document_digest(document),
        "transition_id": transition_id,
    }


def _validate_binding_value(
    value: Any,
    document: dict[str, Any],
    *,
    require_final: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise RetroError("invalid_artifact")
    if (
        value["schema"] != BINDING_SCHEMA
        or value["schema_version"] != BINDING_SCHEMA_VERSION
        or not isinstance(value["immutable_sha256"], str)
        or not SHA256_RE.fullmatch(value["immutable_sha256"])
        or value["immutable_sha256"] != _immutable_digest(document)
    ):
        raise RetroError("immutable_intent_mismatch")
    final_digest = value["final_document_sha256"]
    transition_id = value["transition_id"]
    if final_digest is None and transition_id is None:
        if require_final:
            raise RetroError("invalid_artifact")
        return value
    if (
        not isinstance(final_digest, str)
        or not SHA256_RE.fullmatch(final_digest)
        or not isinstance(transition_id, str)
    ):
        raise RetroError("invalid_artifact")
    canonical_transition = canonical_invocation_id(transition_id)
    if (
        canonical_transition != transition_id
        or document["routing"]["proof"]
        != {"status": "verified", "transition_id": transition_id}
        or final_digest != _document_digest(document)
    ):
        raise RetroError("invalid_artifact")
    return value


def _validate_binding_at(
    bindings_fd: int,
    fingerprint: str,
    document: dict[str, Any],
    *,
    require_final: bool = False,
) -> None:
    name = f"{fingerprint}.sha256"
    value = _read_utf8_json_at(
        bindings_fd,
        name,
        category="invalid_artifact",
        max_bytes=1024,
        overflow_category="invalid_artifact",
    )
    _validate_binding_value(value, document, require_final=require_final)


def _validate_binding(
    store: RetroStore,
    fingerprint: str,
    document: dict[str, Any],
    *,
    require_final: bool = False,
) -> None:
    _assert_store_path(store)
    bindings_fd = _walk_directories(store.retro_fd, (".bindings",), create=False)
    try:
        _validate_binding_at(
            bindings_fd,
            fingerprint,
            document,
            require_final=require_final,
        )
    finally:
        os.close(bindings_fd)


def _ensure_binding(
    store: RetroStore,
    fingerprint: str,
    document: dict[str, Any],
) -> None:
    _assert_store_path(store)
    bindings_fd = _walk_directories(store.retro_fd, (".bindings",), create=True)
    name = f"{fingerprint}.sha256"
    temporary = _unique_temp_name(name)
    payload = _canonical_json(_prepared_binding(document))
    descriptor = -1
    try:
        _assert_store_path(store)
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=bindings_fd,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        value = _read_utf8_json_at(
            bindings_fd,
            temporary,
            category="invalid_artifact",
            max_bytes=1024,
            overflow_category="invalid_artifact",
        )
        _validate_binding_value(value, document, require_final=False)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=bindings_fd,
                dst_dir_fd=bindings_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _validate_binding_at(bindings_fd, fingerprint, document)
            return
        _fsync_file(bindings_fd, name)
        _fsync_directory(bindings_fd)
        _validate_binding_at(bindings_fd, fingerprint, document)
        _assert_store_path(store)
    except RetroError:
        raise
    except OSError:
        raise RetroError("durability_failed") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=bindings_fd)
        os.close(bindings_fd)


def _finalize_binding(
    store: RetroStore,
    fingerprint: str,
    document: dict[str, Any],
) -> None:
    _assert_store_path(store)
    bindings_fd = _walk_directories(store.retro_fd, (".bindings",), create=False)
    name = f"{fingerprint}.sha256"
    temporary = _unique_temp_name(name)
    descriptor = -1
    try:
        current = _read_utf8_json_at(
            bindings_fd,
            name,
            category="invalid_artifact",
            max_bytes=1024,
            overflow_category="invalid_artifact",
        )
        _validate_binding_value(current, document, require_final=False)
        if current["final_document_sha256"] is not None:
            _validate_binding_value(current, document, require_final=True)
            return
        final_value = _final_binding(document)
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=bindings_fd,
        )
        _assert_store_path(store)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(_canonical_json(final_value))
            handle.flush()
            os.fsync(handle.fileno())
        value = _read_utf8_json_at(
            bindings_fd,
            temporary,
            category="invalid_artifact",
            max_bytes=1024,
            overflow_category="invalid_artifact",
        )
        _validate_binding_value(value, document, require_final=True)
        _assert_store_path(store)
        os.replace(
            temporary,
            name,
            src_dir_fd=bindings_fd,
            dst_dir_fd=bindings_fd,
        )
        _fsync_file(bindings_fd, name)
        _fsync_directory(bindings_fd)
        _validate_binding_at(
            bindings_fd,
            fingerprint,
            document,
            require_final=True,
        )
        _assert_store_path(store)
    except RetroError:
        raise
    except OSError:
        raise RetroError("unsafe_artifact_path") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=bindings_fd)
        os.close(bindings_fd)


def _write_exclusive_temp(
    store: RetroStore, final_name: str, document: dict[str, Any]
) -> str:
    _assert_store_path(store)
    directory_fd = store.retro_fd
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
        descriptor = os.open(
            temporary,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        _assert_store_path(store)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(_canonical_json(document))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise RetroError("unsafe_artifact_path") from None
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
    temporary = _write_exclusive_temp(store, name, document)
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
        except OSError:
            raise RetroError("unsafe_artifact_path") from None
        _fsync_file(store.retro_fd, name)
        _fsync_directory(store.retro_fd)
        os.unlink(temporary, dir_fd=store.retro_fd)
        _fsync_directory(store.retro_fd)
        _read_artifact_at(store, fingerprint)
        _assert_store_path(store)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=store.retro_fd)


def _durable_replace(
    store: RetroStore, fingerprint: str, document: dict[str, Any]
) -> None:
    name = store.artifact_name(fingerprint)
    _assert_store_path(store)
    _read_artifact_at(store, fingerprint)
    temporary = _write_exclusive_temp(store, name, document)
    try:
        _validate_temp(store.retro_fd, temporary, name)
        _assert_store_path(store)
        try:
            os.replace(
                temporary,
                name,
                src_dir_fd=store.retro_fd,
                dst_dir_fd=store.retro_fd,
            )
        except OSError:
            raise RetroError("unsafe_artifact_path") from None
        _fsync_file(store.retro_fd, name)
        _fsync_directory(store.retro_fd)
        _read_artifact_at(store, fingerprint)
        _assert_store_path(store)
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
        _acquire_flock(descriptor, timeout_seconds=_lock_timeout_seconds())
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _artifact_lock(store: RetroStore, fingerprint: str) -> Iterator[None]:
    _assert_store_path(store)
    locks = _walk_directories(store.retro_fd, (".locks", "artifacts"), create=True)
    try:
        _assert_store_path(store)
        with _safe_lock(locks, f"{fingerprint}.lock"):
            _assert_store_path(store)
            yield
    finally:
        os.close(locks)


@contextlib.contextmanager
def _global_comment_lock(
    marker: str,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[socket.socket]:
    lock_socket: socket.socket | None = None
    name = _comment_lock_name(marker)
    try:
        if sys.platform != "linux":
            raise RetroError("provider_containment_unavailable")
        lock_socket = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
        )
        lock_socket.set_inheritable(False)
        deadline = time.monotonic() + (
            timeout_seconds if timeout_seconds is not None else _lock_timeout_seconds()
        )
        while True:
            try:
                lock_socket.bind(name)
                break
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise RetroError("provider_containment_unavailable") from None
                if time.monotonic() >= deadline:
                    raise RetroError("lock_timeout") from None
                time.sleep(
                    min(LOCK_RETRY_SECONDS, max(0.0, deadline - time.monotonic()))
                )
        yield lock_socket
    except RetroError:
        raise
    except OSError:
        raise RetroError("provider_containment_unavailable") from None
    finally:
        if lock_socket is not None:
            lock_socket.close()


def _immutable_view(
    document: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, Any]:
    return {field: document[field] for field in fields}


def prepare(repo_root: Path, intent: dict[str, Any]) -> dict[str, Any]:
    with _repository(repo_root) as repository:
        proposed = _build_prepared(repository.repo, repository.provider, intent)
        fingerprint = _document_fingerprint(proposed)
        with _retro_store(repository, create=True) as store:
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
                    _ensure_binding(store, fingerprint, proposed)
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
        "comment_fingerprint_marker": comment_marker(stored),
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
    if target != result["target_issue"] or target != stored["source_issue"]:
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


@dataclass(frozen=True)
class _TrustedTransition:
    artifact_fingerprint: str
    immutable_sha256: str
    transition_id: str
    result_json: bytes
    seal: str


_TRANSITION_KEY = os.urandom(32)
_ACTIVE_TRANSITIONS: dict[str, str] = {}


def _transition_preimage(
    fingerprint: str,
    immutable_sha256: str,
    transition_id: str,
    result_json: bytes,
) -> bytes:
    return b"\n".join(
        (
            b"hermes.run-retro.trusted-transition.v1",
            fingerprint.encode("ascii"),
            immutable_sha256.encode("ascii"),
            transition_id.encode("ascii"),
            result_json,
            b"",
        )
    )


def _issue_trusted_transition(
    stored: dict[str, Any],
    fingerprint: str,
    result: dict[str, Any],
) -> _TrustedTransition:
    if not SHA256_RE.fullmatch(fingerprint):
        raise RetroError("invalid_artifact")
    normalized = _validated_result(stored, result)
    immutable_sha256 = _immutable_digest(stored)
    transition_id = str(uuid.uuid4())
    result_json = _canonical_json(normalized)
    seal = hmac.new(
        _TRANSITION_KEY,
        _transition_preimage(
            fingerprint,
            immutable_sha256,
            transition_id,
            result_json,
        ),
        hashlib.sha256,
    ).hexdigest()
    _ACTIVE_TRANSITIONS[transition_id] = seal
    return _TrustedTransition(
        artifact_fingerprint=fingerprint,
        immutable_sha256=immutable_sha256,
        transition_id=transition_id,
        result_json=result_json,
        seal=seal,
    )


def _consume_trusted_transition(
    stored: dict[str, Any],
    fingerprint: str,
    transition: _TrustedTransition,
) -> tuple[dict[str, Any], str]:
    if type(transition) is not _TrustedTransition:
        raise RetroError("untrusted_finalization")
    try:
        transition_id = canonical_invocation_id(transition.transition_id)
        result = json.loads(transition.result_json.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RetroError("untrusted_finalization") from None
    normalized = _validated_result(stored, result)
    immutable_sha256 = _immutable_digest(stored)
    expected_seal = hmac.new(
        _TRANSITION_KEY,
        _transition_preimage(
            fingerprint,
            immutable_sha256,
            transition_id,
            _canonical_json(normalized),
        ),
        hashlib.sha256,
    ).hexdigest()
    if (
        transition.artifact_fingerprint != fingerprint
        or transition.immutable_sha256 != immutable_sha256
        or transition.transition_id != transition_id
        or transition.result_json != _canonical_json(normalized)
        or not hmac.compare_digest(transition.seal, expected_seal)
        or not hmac.compare_digest(
            _ACTIVE_TRANSITIONS.get(transition_id, ""),
            expected_seal,
        )
    ):
        raise RetroError("untrusted_finalization")
    del _ACTIVE_TRANSITIONS[transition_id]
    return normalized, transition_id


def _finalize_at(
    store: RetroStore,
    fingerprint: str,
    transition: _TrustedTransition,
) -> dict[str, Any]:
    with _artifact_lock(store, fingerprint):
        _assert_store_path(store)
        stored = _read_artifact_at(store, fingerprint)
        if stored is None:
            raise RetroError("invalid_artifact")
        normalized, transition_id = _consume_trusted_transition(
            stored,
            fingerprint,
            transition,
        )
        stored_status = stored["routing"]["status"]
        incoming_status = normalized["status"]
        if stored_status in SUCCESS_STATUSES or stored_status == "no_target_issue":
            try:
                _validate_binding(
                    store,
                    fingerprint,
                    stored,
                    require_final=True,
                )
            except RetroError as error:
                if error.category != "invalid_artifact":
                    raise
            else:
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
            "updated_at_epoch_us": _epoch_us_now(),
            "proof": (
                {
                    "status": "verified",
                    "transition_id": transition_id,
                }
                if incoming_status in CHECKPOINT_STATUSES
                else {
                    "status": "unverified",
                    "transition_id": None,
                }
            ),
        }
        if _immutable_view(updated, IMMUTABLE_FIELDS) != _immutable_view(
            stored, IMMUTABLE_FIELDS
        ):
            raise RetroError("immutable_intent_mismatch")
        validate_document(
            updated,
            path=Path(store.artifact_name(fingerprint)),
        )
        _durable_replace(store, fingerprint, updated)
        if incoming_status in CHECKPOINT_STATUSES:
            _finalize_binding(store, fingerprint, updated)
            _read_artifact_at(store, fingerprint, require_final=True)
        else:
            _read_artifact_at(store, fingerprint)
    return {
        "status": incoming_status,
        "artifact_fingerprint": fingerprint,
        "artifact_path": store.relative_path(fingerprint),
        "transition": "updated",
    }


def finalize(
    repo_root: Path,
    fingerprint: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del repo_root, fingerprint, result
    raise RetroError("untrusted_finalization")


def comment_body(document: dict[str, Any]) -> str:
    validate_document(document)
    marker = comment_marker(document)
    return _comment_body(
        document["decisions"],
        document["operator_action_required"],
        marker,
    )


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


@contextlib.contextmanager
def _provider_script_fd(
    repository: RepositorySession,
    providers_dir: Path,
    provider: str,
) -> Iterator[int]:
    directory_fd = -1
    script_fd = -1
    try:
        absolute_providers = Path(os.path.abspath(providers_dir))
        try:
            relative = absolute_providers.relative_to(repository.repo_root)
        except ValueError:
            raise RetroError("provider_unavailable") from None
        if not relative.parts:
            raise RetroError("provider_unavailable")
        directory_fd = _walk_directories(
            repository.repo_fd,
            tuple(relative.parts),
            create=False,
        )
        script_fd = os.open(
            f"{provider}.sh",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(script_fd).st_mode):
            raise OSError
        _assert_repository_path(repository)
        yield script_fd
    except RetroError:
        raise
    except OSError:
        raise RetroError("provider_unavailable") from None
    finally:
        if script_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(script_fd)
        if directory_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(directory_fd)


def _trusted_executable(path: Path) -> bool:
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and path_metadata.st_uid == 0
        and metadata.st_uid == 0
        and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
        and stat.S_IMODE(metadata.st_mode) & 0o111 != 0
        and (metadata.st_dev, metadata.st_ino)
        == (path_metadata.st_dev, path_metadata.st_ino)
    )


def _containment_executable() -> Path:
    if sys.platform != "linux":
        raise RetroError("provider_containment_unavailable")
    for candidate in LINUX_CONTAINMENT_CANDIDATES:
        if _trusted_executable(candidate):
            return candidate
    raise RetroError("provider_containment_unavailable")


def _contained_provider_command(
    provider: str,
    source_issue: str,
    marker: str,
    body: str,
    *,
    info_fd: int,
    block_fd: int,
) -> list[str]:
    executable = _containment_executable()
    return [
        str(executable),
        "--unshare-pid",
        "--as-pid-1",
        "--die-with-parent",
        "--info-fd",
        str(info_fd),
        "--block-fd",
        str(block_fd),
        "--dev-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--",
        "sh",
        "-s",
        "ensure_comment",
        source_issue,
        marker,
        body,
    ]


def _terminate_supervisor(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=SUPERVISOR_SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            process.wait(timeout=SUPERVISOR_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            raise RetroError("provider_containment_unavailable") from None


def _read_containment_info(descriptor: int) -> int:
    selector = selectors.DefaultSelector()
    buffer = bytearray()

    def parsed_child_pid() -> int | None:
        try:
            text = bytes(buffer).decode("utf-8")
            data, end = json.JSONDecoder().raw_decode(text)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if text[end:].strip():
            raise RetroError("provider_containment_unavailable")
        child_pid = data.get("child-pid") if isinstance(data, dict) else None
        pid_namespace = data.get("pid-namespace") if isinstance(data, dict) else None
        if (
            not isinstance(child_pid, int)
            or isinstance(child_pid, bool)
            or child_pid <= 1
            or not isinstance(pid_namespace, int)
            or isinstance(pid_namespace, bool)
            or pid_namespace <= 0
        ):
            raise RetroError("provider_containment_unavailable")
        return child_pid

    try:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + SUPERVISOR_SHUTDOWN_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetroError("provider_containment_unavailable")
            if not selector.select(min(remaining, 0.05)):
                continue
            try:
                chunk = os.read(descriptor, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > 4096:
                raise RetroError("provider_containment_unavailable")
            child_pid = parsed_child_pid()
            if child_pid is not None:
                return child_pid
        child_pid = parsed_child_pid()
        if child_pid is None:
            raise RetroError("provider_containment_unavailable")
        return child_pid
    except RetroError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RetroError("provider_containment_unavailable") from None
    finally:
        selector.close()


def _open_pidfd(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        try:
            return os.pidfd_open(pid, 0)
        except OSError:
            raise RetroError("provider_containment_unavailable") from None
    if sys.platform != "linux" or os.uname().machine not in {
        "aarch64",
        "arm64",
        "ppc64le",
        "x86_64",
    }:
        raise RetroError("provider_containment_unavailable")
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        descriptor = libc.syscall(434, pid, 0)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "pidfd_open")
        return int(descriptor)
    except (AttributeError, OSError):
        raise RetroError("provider_containment_unavailable") from None


def _signal_pidfd(pidfd: int, signum: signal.Signals) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(pidfd, signum)
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(424, pidfd, int(signum), 0, 0)
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                raise ProcessLookupError(error, "pidfd_send_signal")
            raise OSError(error, "pidfd_send_signal")
    except ProcessLookupError:
        raise
    except (AttributeError, OSError):
        raise RetroError("provider_containment_unavailable") from None


def _terminate_contained_provider(
    process: subprocess.Popen[bytes],
    pidfd: int,
) -> None:
    group_fallback = process.poll() is None and pidfd < 0
    if process.poll() is None and pidfd >= 0:
        try:
            _signal_pidfd(pidfd, signal.SIGKILL)
        except (ProcessLookupError, OSError, RetroError):
            group_fallback = True
    if group_fallback and process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=SUPERVISOR_SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=SUPERVISOR_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            try:
                process.wait(timeout=SUPERVISOR_SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:
                raise RetroError("provider_containment_unavailable") from None


def _bounded_provider_output(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    streams = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    selector = selectors.DefaultSelector()
    try:
        for stream in streams:
            if stream is None:
                raise RetroError("provider_io_failed")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetroError("provider_timeout")
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                events = [
                    (key, selectors.EVENT_READ)
                    for key in list(selector.get_map().values())
                ]
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffer = streams[stream]
                buffer.extend(chunk)
                if len(buffer) > MAX_PROVIDER_OUTPUT_BYTES:
                    raise RetroError("provider_output_too_large")
        remaining = max(0.0, deadline - time.monotonic())
        process.wait(timeout=remaining)
        return bytes(streams[process.stdout]), bytes(streams[process.stderr])
    except (OSError, subprocess.TimeoutExpired, RetroError):
        raise RetroError("provider_io_failed") from None
    finally:
        selector.close()
        for stream in streams:
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def _provider_supervisor_payload(
    stored: dict[str, Any],
    provider_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "provider": stored["provider"],
        "source_issue": stored["source_issue"],
        "marker": comment_marker(stored),
        "body": comment_body(stored),
        "provider_config": provider_config,
        "provider_timeout_seconds": _bounded_seconds(
            PROVIDER_TIMEOUT_SECONDS,
            category="provider_timeout",
            maximum=LOCK_ACQUISITION_MAX_SECONDS,
        ),
        "lock_timeout_seconds": _lock_timeout_seconds(),
    }


def _validated_supervisor_payload(value: Any) -> dict[str, Any]:
    expected = {
        "provider",
        "source_issue",
        "marker",
        "body",
        "provider_config",
        "provider_timeout_seconds",
        "lock_timeout_seconds",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise RetroError("invalid_input")
    provider = value["provider"]
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise RetroError("invalid_input")
    source_issue = canonical_issue_id(provider, value["source_issue"])
    if source_issue != value["source_issue"]:
        raise RetroError("invalid_input")
    marker = value["marker"]
    _comment_lock_name(marker)
    body = value["body"]
    if not isinstance(body, str) or body.count(marker) != 1:
        raise RetroError("invalid_input")
    provider_config = value["provider_config"]
    if not isinstance(provider_config, dict):
        raise RetroError("invalid_input")
    provider_timeout = _bounded_seconds(
        value["provider_timeout_seconds"],
        category="provider_timeout",
        maximum=LOCK_ACQUISITION_MAX_SECONDS,
    )
    lock_timeout = _bounded_seconds(
        value["lock_timeout_seconds"],
        category="lock_timeout",
        maximum=LOCK_ACQUISITION_MAX_SECONDS,
    )
    return {
        "provider": provider,
        "source_issue": source_issue,
        "marker": marker,
        "body": body,
        "provider_config": provider_config,
        "provider_timeout_seconds": provider_timeout,
        "lock_timeout_seconds": lock_timeout,
    }


def _read_supervisor_payload(descriptor: int) -> dict[str, Any]:
    try:
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            payload = _read_bounded(
                handle,
                MAX_INPUT_BYTES,
                overflow_category="input_too_large",
            )
        return _validated_supervisor_payload(json.loads(payload.decode("utf-8")))
    except RetroError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RetroError("invalid_input") from None


def _run_contained_provider(
    payload: dict[str, Any],
    script_fd: int,
) -> dict[str, Any]:
    provider = payload["provider"]
    source_issue = payload["source_issue"]
    environment = dict(os.environ)
    environment["TICKET_PROVIDER"] = provider
    environment["HERMES_BOUND_PROVIDER_CONFIG"] = "1"
    environment["HERMES_BOUND_TICKET_PROVIDER_JSON"] = json.dumps(
        payload["provider_config"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    stored_identity = {"provider": provider, "source_issue": source_issue}
    process: subprocess.Popen[bytes] | None = None
    info_read_fd = -1
    info_write_fd = -1
    block_read_fd = -1
    block_write_fd = -1
    pidfd = -1
    try:
        os.lseek(script_fd, 0, os.SEEK_SET)
        with tempfile.TemporaryDirectory(
            prefix="hermes-provider-",
            dir=os.environ.get("TMPDIR"),
        ) as provider_tmp:
            environment["TMPDIR"] = provider_tmp
            info_read_fd, info_write_fd = os.pipe()
            block_read_fd, block_write_fd = os.pipe()
            process = subprocess.Popen(
                _contained_provider_command(
                    provider,
                    source_issue,
                    payload["marker"],
                    payload["body"],
                    info_fd=info_write_fd,
                    block_fd=block_read_fd,
                ),
                stdin=script_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
                pass_fds=(info_write_fd, block_read_fd),
            )
            os.close(info_write_fd)
            info_write_fd = -1
            os.close(block_read_fd)
            block_read_fd = -1
            try:
                child_pid = _read_containment_info(info_read_fd)
                pidfd = _open_pidfd(child_pid)
                os.write(block_write_fd, b"1")
                os.close(block_write_fd)
                block_write_fd = -1
                stdout, _ = _bounded_provider_output(
                    process,
                    timeout_seconds=payload["provider_timeout_seconds"],
                )
            finally:
                _terminate_contained_provider(process, pidfd)
    except (OSError, RetroError):
        return _provider_failure(
            stored_identity,
            "response_unknown",
            "provider response not confirmed",
        )
    finally:
        for descriptor in (
            info_read_fd,
            info_write_fd,
            block_read_fd,
            block_write_fd,
            pidfd,
        ):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
    if process is None or process.returncode != 0:
        return _provider_failure(
            stored_identity,
            "response_unknown",
            "provider response not confirmed",
        )
    try:
        result = json.loads(stdout.decode("utf-8"))
        return _validated_result(stored_identity, result)
    except (UnicodeError, json.JSONDecodeError, RetroError, TypeError, ValueError):
        return _provider_failure(
            stored_identity,
            "response_unknown",
            "provider response not confirmed",
        )


def _supervise_provider(script_fd: int, payload_fd: int) -> dict[str, Any]:
    try:
        if not stat.S_ISREG(os.fstat(script_fd).st_mode):
            raise RetroError("provider_unavailable")
    except OSError:
        raise RetroError("provider_unavailable") from None
    payload = _read_supervisor_payload(payload_fd)
    with _global_comment_lock(
        payload["marker"],
        timeout_seconds=payload["lock_timeout_seconds"],
    ):
        return _run_contained_provider(payload, script_fd)


def _controller_source_fd() -> int:
    path = Path(__file__)
    descriptor = -1
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or (
            metadata.st_dev,
            metadata.st_ino,
        ) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise RetroError("provider_containment_unavailable")
        return descriptor
    except RetroError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise RetroError("provider_containment_unavailable") from None


def _controller_timeout_seconds() -> float:
    return (
        _lock_timeout_seconds()
        + PROVIDER_TIMEOUT_SECONDS
        + (4 * SUPERVISOR_SHUTDOWN_SECONDS)
        + 0.5
    )


def _invoke_provider(
    repository: RepositorySession,
    stored: dict[str, Any],
    fingerprint: str,
    script_fd: int,
    provider_config: dict[str, Any],
) -> _TrustedTransition:
    _assert_repository_path(repository)
    try:
        _containment_executable()
    except RetroError as error:
        if error.category != "provider_containment_unavailable":
            raise
        return _issue_trusted_transition(
            stored,
            fingerprint,
            _provider_failure(
                stored,
                "response_unknown",
                "provider response not confirmed",
            ),
        )
    payload = _canonical_json(_provider_supervisor_payload(stored, provider_config))
    source_fd = -1
    payload_read_fd = -1
    payload_write_fd = -1
    process: subprocess.Popen[bytes] | None = None
    try:
        source_fd = _controller_source_fd()
        payload_read_fd, payload_write_fd = os.pipe()
        process = subprocess.Popen(
            [
                sys.executable,
                "-",
                "_supervise-provider",
                "--script-fd",
                str(script_fd),
                "--payload-fd",
                str(payload_read_fd),
            ],
            stdin=source_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(script_fd, payload_read_fd),
            start_new_session=True,
            env=dict(os.environ),
        )
        os.close(payload_read_fd)
        payload_read_fd = -1
        with os.fdopen(payload_write_fd, "wb", closefd=True) as handle:
            payload_write_fd = -1
            handle.write(payload)
        stdout, _ = _bounded_provider_output(
            process,
            timeout_seconds=_controller_timeout_seconds(),
        )
    except (OSError, RetroError):
        return _issue_trusted_transition(
            stored,
            fingerprint,
            _provider_failure(
                stored,
                "response_unknown",
                "provider response not confirmed",
            ),
        )
    finally:
        if process is not None:
            with contextlib.suppress(RetroError):
                _terminate_supervisor(process)
        for descriptor in (source_fd, payload_read_fd, payload_write_fd):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
    if process is None or process.returncode != 0:
        return _issue_trusted_transition(
            stored,
            fingerprint,
            _provider_failure(
                stored,
                "response_unknown",
                "provider response not confirmed",
            ),
        )
    try:
        result = json.loads(stdout.decode("utf-8"))
        return _issue_trusted_transition(stored, fingerprint, result)
    except (UnicodeError, json.JSONDecodeError, RetroError, TypeError, ValueError):
        return _issue_trusted_transition(
            stored,
            fingerprint,
            _provider_failure(
                stored,
                "response_unknown",
                "provider response not confirmed",
            ),
        )


def deliver(
    repo_root: Path, fingerprint: str, *, providers_dir: Path | None = None
) -> dict[str, Any]:
    """Deliver only the immutable provider/source/body bound by prepare."""
    if not SHA256_RE.fullmatch(fingerprint):
        raise RetroError("invalid_artifact")
    with _repository(repo_root) as repository:
        with _retro_store(repository, create=False) as store:
            stored = _read_artifact_at(store, fingerprint)
            if stored is None:
                raise RetroError("invalid_artifact")
            if (
                stored["repo"] != repository.repo
                or stored["provider"] != repository.provider
            ):
                raise RetroError("immutable_intent_mismatch")
            if stored["source_issue"] is None:
                return _finalize_at(
                    store,
                    fingerprint,
                    _issue_trusted_transition(
                        stored,
                        fingerprint,
                        {
                            "provider": stored["provider"],
                            "status": "no_target_issue",
                            "target_issue": None,
                            "error_category": None,
                            "error_summary": None,
                        },
                    ),
                )
            try:
                provider_context = _provider_script_fd(
                    repository,
                    providers_dir or _default_providers_dir(),
                    stored["provider"],
                )
                with provider_context as script_fd:
                    _assert_repository_path(repository)
                    _assert_store_path(store)
                    current = _read_artifact_at(store, fingerprint)
                    if current is None:
                        raise RetroError("invalid_artifact")
                    if _immutable_view(current, IMMUTABLE_FIELDS) != _immutable_view(
                        stored, IMMUTABLE_FIELDS
                    ):
                        raise RetroError("immutable_intent_mismatch")
                    if current["routing"]["status"] in SUCCESS_STATUSES:
                        try:
                            _validate_binding(
                                store,
                                fingerprint,
                                current,
                                require_final=True,
                            )
                        except RetroError as error:
                            if error.category != "invalid_artifact":
                                raise
                        else:
                            return {
                                "status": current["routing"]["status"],
                                "artifact_fingerprint": fingerprint,
                                "artifact_path": store.relative_path(fingerprint),
                                "transition": "preserved_terminal",
                            }
                    result = _invoke_provider(
                        repository,
                        current,
                        fingerprint,
                        script_fd,
                        repository.provider_config,
                    )
                    _assert_repository_path(repository)
                    _assert_store_path(store)
                    return _finalize_at(store, fingerprint, result)
            except RetroError as error:
                if error.category != "provider_unavailable":
                    raise
                return _finalize_at(
                    store,
                    fingerprint,
                    _issue_trusted_transition(
                        stored,
                        fingerprint,
                        _provider_failure(
                            stored,
                            "serialization_failed",
                            "provider adapter unavailable",
                        ),
                    ),
                )


def _load_input(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            payload = _read_bounded(
                sys.stdin.buffer,
                MAX_INPUT_BYTES,
                overflow_category="input_too_large",
            )
            value = json.loads(payload.decode("utf-8"))
        else:
            value = _read_utf8_json(
                Path(path),
                category="invalid_input",
                max_bytes=MAX_INPUT_BYTES,
                overflow_category="input_too_large",
            )
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
    with _repository(repo_root) as repository:
        with _retro_store(repository, create=False) as store:
            document = _read_artifact_at(
                store,
                fingerprint,
                require_final=require_final,
            )
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
    supervisor_parser = subparsers.add_parser("_supervise-provider")
    supervisor_parser.add_argument("--script-fd", required=True, type=int)
    supervisor_parser.add_argument("--payload-fd", required=True, type=int)
    return parser


def _supervisor_main(script_fd: int, payload_fd: int) -> int:
    try:
        result = _supervise_provider(script_fd, payload_fd)
        output = _canonical_json(result)
        return_code = 0
    except RetroError as error:
        output = _canonical_json(
            {"status": "stalled", "error_category": error.category}
        )
        return_code = 3
    except (OSError, UnicodeError, ValueError, TypeError):
        output = _canonical_json(
            {"status": "stalled", "error_category": "provider_containment_unavailable"}
        )
        return_code = 3
    try:
        os.write(sys.stdout.fileno(), output)
    except OSError:
        pass
    return return_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "_supervise-provider":
        return _supervisor_main(args.script_fd, args.payload_fd)
    try:
        repo_root = args.repo_root.resolve()
        if args.command == "prepare":
            result = prepare(repo_root, _load_input(args.intent))
            print(json.dumps(result, sort_keys=True))
        elif args.command == "finalize":
            result = finalize(
                repo_root,
                args.artifact_fingerprint,
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
                        "artifact_fingerprint": _document_fingerprint(document),
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
