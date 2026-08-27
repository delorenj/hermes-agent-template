#!/usr/bin/env python3
"""Commit one verified channel wiring as a recoverable local transaction.

The caller must hold the fleet registry lock and must have already completed
read-only ownership checks plus 1Password staging/verification.  Raw channel
credentials are never accepted.  Original file bytes remain only in memory;
any failed write restores every touched file byte-for-byte before returning.
"""

from __future__ import annotations

import argparse
import copy
import errno
import importlib.util
import json
import os
import pathlib
import re
import stat
import tempfile
from dataclasses import dataclass

import yaml


def load_profile_lock_module():
    source = pathlib.Path(__file__).parent / "lib" / "profile-config-lock.py"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"trusted profile config lock helper is unavailable: {source}")
    spec = importlib.util.spec_from_file_location(
        "pjangler_profile_config_lock", source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load profile config lock helper: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROFILE_LOCK = load_profile_lock_module()


LIST_PATCH_KEY = "x-pjangler-merge"
CHANNEL_FIELDS = {
    "telegram": ("provisioning_status", "bot_username", "bot_id"),
    "slack": (
        "provisioning_status",
        "team_id",
        "team_name",
        "bot_user_id",
        "bot_id",
        "bot_username",
    ),
}
CHANNEL_REFERENCE_KEYS = {
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
}
CHANNEL_ALLOWED_KEYS = {
    "telegram": "TELEGRAM_ALLOWED_USERS",
    "slack": "SLACK_ALLOWED_USERS",
}


@dataclass(frozen=True)
class Snapshot:
    existed: bool
    content: bytes
    mode: int


def fail(message: str) -> "None":
    raise SystemExit(f"channel transaction failed: {message}")


def fsync_parent(path: pathlib.Path) -> None:
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), errno.ENOSYS}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(path.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_fd)


def snapshot(path: pathlib.Path, default_mode: int) -> Snapshot:
    if path.is_symlink():
        fail(f"refusing symlinked transaction path: {path}")
    if not path.exists():
        return Snapshot(False, b"", default_mode)
    if not path.is_file():
        fail(f"transaction path is not a regular file: {path}")
    return Snapshot(True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def atomic_write(path: pathlib.Path, content: bytes, mode: int) -> None:
    if path.is_symlink():
        fail(f"refusing symlinked transaction path: {path}")
    if path.is_file() and path.read_bytes() == content and stat.S_IMODE(path.stat().st_mode) == mode:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.channel-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_parent(path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def restore(paths: dict[pathlib.Path, Snapshot]) -> None:
    errors: list[str] = []
    for path, original in reversed(tuple(paths.items())):
        try:
            if original.existed:
                atomic_write(path, original.content, original.mode)
            elif path.is_symlink() or path.is_file():
                path.unlink()
                fsync_parent(path)
            elif path.exists():
                raise RuntimeError("rollback target became a non-file")
        except BaseException as exc:  # preserve every other snapshot too
            errors.append(f"{path}: {type(exc).__name__}")
    if errors:
        raise RuntimeError("rollback incomplete: " + ", ".join(errors))


def load_mapping(path: pathlib.Path, *, required: bool = True) -> dict:
    if path.is_symlink() or (required and not path.is_file()):
        fail(f"required mapping is unavailable: {path}")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        fail(f"mapping root required: {path}")
    return data


def plain_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = plain_merge(result[key], value)
        elif key in result and isinstance(result[key], dict) and value is None:
            continue
        else:
            result[key] = copy.deepcopy(value)
    return result


def apply_list_patches(result: dict, directive: object) -> None:
    if directive is None:
        return
    if not isinstance(directive, dict) or not isinstance(directive.get("list_patches", {}), dict):
        fail(f"{LIST_PATCH_KEY}.list_patches must be a mapping")
    for dotted, rule in directive.get("list_patches", {}).items():
        if not isinstance(dotted, str) or not dotted or not isinstance(rule, dict):
            fail("invalid list patch")
        additions = rule.get("add", []) or []
        removals = rule.get("remove", []) or []
        if not isinstance(additions, list) or not isinstance(removals, list) or not all(
            isinstance(item, str) for item in [*additions, *removals]
        ):
            fail(f"list patch for {dotted} must contain string lists")
        cursor = result
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            if not isinstance(child, dict):
                fail(f"list patch parent for {dotted} is not a mapping")
            cursor = child
        current = cursor.get(parts[-1], []) or []
        if not isinstance(current, list):
            fail(f"list patch target {dotted} is not a list")
        removed = set(removals)
        merged = [item for item in current if item not in removed]
        for item in additions:
            if item not in merged:
                merged.append(item)
        cursor[parts[-1]] = merged


def merge(base: dict, delta: dict) -> dict:
    ordinary = {key: value for key, value in delta.items() if key != LIST_PATCH_KEY}
    result = plain_merge(base, ordinary)
    apply_list_patches(result, delta.get(LIST_PATCH_KEY))
    return result


def delta_comments(original: bytes) -> list[str]:
    comments: list[str] = []
    for line in original.decode("utf-8").splitlines() if original else []:
        if line.lstrip().startswith("#") and line not in comments:
            comments.append(line)
    standard = [
        "# Override-only delta for this Hermes profile.",
        "# Contains configuration and secret references only; secret values remain in 1Password.",
    ]
    return [*standard, *(line for line in comments if line not in standard)]


def render_delta(delta: dict, original: bytes) -> bytes:
    return (
        "\n".join(delta_comments(original))
        + "\n"
        + yaml.safe_dump(delta, sort_keys=False)
    ).encode("utf-8")


def render_generated(base: dict, delta: dict) -> bytes:
    header = (
        "# GENERATED FILE -- DO NOT EDIT.\n"
        "# source: fleet config.yaml + profile config.delta.yaml\n"
    )
    return (header + yaml.safe_dump(merge(base, delta), sort_keys=False)).encode("utf-8")


def update_role(original: bytes, channel: str, metadata: dict[str, str]) -> bytes:
    text = original.decode("utf-8")
    match = re.search(
        rf"(?ms)^{re.escape(channel)}:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", text
    )
    if not match:
        fail(f"{channel} metadata block missing from role.yaml")
    body = match.group("body")
    for key in CHANNEL_FIELDS[channel]:
        value = metadata[key]
        replacement = f"  {key}: {json.dumps(value)}"
        body, count = re.subn(
            rf"(?m)^\s+{re.escape(key)}:\s*.*$", lambda _: replacement, body, count=1
        )
        if count == 0:
            if body and not body.endswith("\n"):
                body += "\n"
            body += replacement + "\n"
    return (text[: match.start("body")] + body + text[match.end("body") :]).encode(
        "utf-8"
    )


def update_runtime_env(original: bytes, channel: str, allowed_value: str) -> bytes:
    text = original.decode("utf-8") if original else ""
    keys = [*CHANNEL_REFERENCE_KEYS[channel], CHANNEL_ALLOWED_KEYS[channel]]
    for key in keys:
        text = re.sub(
            rf"(?m)^\s*(?:export\s+)?#?\s*{re.escape(key)}\s*=.*(?:\n|$)",
            "",
            text,
        )
    text = text.rstrip("\n")
    if text:
        text += "\n"
    text += f"{CHANNEL_ALLOWED_KEYS[channel]}={json.dumps(allowed_value)}\n"
    return text.encode("utf-8")


def merge_managed(current: dict, update: dict) -> dict:
    result = copy.deepcopy(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_managed(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def update_registry(
    original: bytes,
    channel: str,
    agent_id: str,
    role_dir: str,
    profile_name: str,
    metadata: dict[str, str],
) -> bytes:
    try:
        data = yaml.safe_load(original.decode("utf-8")) if original else None
    except (UnicodeError, yaml.YAMLError) as exc:
        fail(f"cannot safely inspect {channel} ownership registry: {type(exc).__name__}")
    data = data or {"schema_version": 1, "agents": {}}
    if not isinstance(data, dict) or not isinstance(data.get("agents", {}), dict):
        fail(f"cannot safely inspect {channel} ownership registry: invalid agents mapping")
    agents = data.setdefault("agents", {})
    for other_id, entry in agents.items():
        if other_id == agent_id or not isinstance(entry, dict):
            continue
        claim = entry.get(channel) or {}
        if not isinstance(claim, dict):
            continue
        if channel == "telegram" and str(claim.get("bot_id") or "") == metadata["bot_id"]:
            fail(f"Telegram bot identity is already assigned to agent {other_id}")
        if channel == "slack":
            same_bot = claim.get("bot_id") == metadata["bot_id"]
            same_team_user = (
                claim.get("team_id") == metadata["team_id"]
                and claim.get("bot_user_id") == metadata["bot_user_id"]
            )
            if same_bot or same_team_user:
                fail(f"Slack bot identity is already assigned to agent {other_id}")
    existing = agents.get(agent_id, {})
    if not isinstance(existing, dict):
        fail(f"registry entry for {agent_id} is not a mapping")
    managed = {
        "role_dir": role_dir,
        "profile_name": profile_name,
        channel: metadata,
    }
    updated = merge_managed(existing, managed)
    if updated == existing:
        return original
    agents[agent_id] = updated
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def parse_pairs(values: list[list[str]], expected: tuple[str, ...], label: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for key, value in values:
        if key in pairs:
            fail(f"duplicate {label} key: {key}")
        pairs[key] = value
    if tuple(pairs) != expected:
        fail(f"{label} keys must be exactly: {', '.join(expected)}")
    return pairs


def commit_locked(args: argparse.Namespace) -> None:
    channel = args.channel
    profile = pathlib.Path(args.profile)
    if profile.is_symlink() or not profile.is_dir():
        fail(f"profile root must be a real directory: {profile}")
    delta_path = profile / "config.delta.yaml"
    generated_path = profile / "config.yaml"
    base_path = profile.parent.parent / "config.yaml"
    role_path = pathlib.Path(args.role_yaml)
    registry_path = pathlib.Path(args.registry)
    env_path = pathlib.Path(args.runtime_env)
    marker_path = pathlib.Path(args.done_marker)
    references = parse_pairs(args.reference, CHANNEL_REFERENCE_KEYS[channel], "reference")
    metadata = parse_pairs(args.metadata, CHANNEL_FIELDS[channel], "metadata")
    if metadata["provisioning_status"] != "verified" or any(
        not metadata[key] for key in CHANNEL_FIELDS[channel] if key != "team_name"
    ):
        fail(f"{channel} verified metadata is incomplete")
    for reference in references.values():
        if not reference.startswith("op://") or any(ch in reference for ch in "\r\n\0"):
            fail("invalid 1Password reference")

    modes = {
        delta_path: 0o600,
        generated_path: 0o600,
        role_path: 0o644,
        registry_path: 0o600,
        env_path: 0o600,
        marker_path: 0o600,
    }
    originals = {path: snapshot(path, mode) for path, mode in modes.items()}
    PROFILE_LOCK.test_snapshot_barrier(f"channel:{channel}")
    base = load_mapping(base_path)
    delta = load_mapping(delta_path, required=False)
    onepassword = delta.setdefault("secrets", {}).setdefault("onepassword", {})
    if not isinstance(onepassword, dict):
        fail("secrets.onepassword delta must be a mapping")
    onepassword["enabled"] = True
    secret_env = onepassword.setdefault("env", {})
    if not isinstance(secret_env, dict):
        fail("secrets.onepassword.env delta must be a mapping")
    secret_env.update(references)
    platforms = delta.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        fail("platforms delta must be a mapping")
    platform = platforms.setdefault(channel, {})
    if not isinstance(platform, dict):
        fail(f"platforms.{channel} delta must be a mapping")
    platform["enabled"] = False

    disabled_delta = render_delta(delta, originals[delta_path].content)
    disabled_generated = render_generated(base, delta)
    role_content = update_role(originals[role_path].content, channel, metadata)
    env_content = update_runtime_env(originals[env_path].content, channel, args.allowed_value)
    registry_content = update_registry(
        originals[registry_path].content,
        channel,
        args.agent_id,
        args.role_dir,
        args.profile_name,
        metadata,
    )
    platform["enabled"] = True
    enabled_delta = render_delta(delta, originals[delta_path].content)
    enabled_generated = render_generated(base, delta)

    try:
        # Phase one is durable but explicitly disabled.
        atomic_write(delta_path, disabled_delta, modes[delta_path])
        atomic_write(generated_path, disabled_generated, modes[generated_path])
        atomic_write(env_path, env_content, modes[env_path])
        atomic_write(role_path, role_content, originals[role_path].mode)
        atomic_write(registry_path, registry_content, modes[registry_path])
        # Activation and completion are the final writes.
        atomic_write(delta_path, enabled_delta, modes[delta_path])
        atomic_write(generated_path, enabled_generated, modes[generated_path])
        atomic_write(marker_path, b"", originals[marker_path].mode)
    except BaseException as exc:
        try:
            restore(originals)
        except BaseException as rollback_exc:
            fail(
                f"{type(exc).__name__}; byte-exact rollback also failed: "
                f"{type(rollback_exc).__name__}"
            )
        fail(f"{type(exc).__name__}; all local channel files restored")


def commit(args: argparse.Namespace) -> None:
    # The channel step holds REGISTRY_FILE.lock before invoking this helper.
    # The global order is registry lock -> profile lock; config-only writers
    # take only the profile lock and no path may invert that order.
    profile = pathlib.Path(args.profile)
    try:
        with PROFILE_LOCK.ProfileConfigLock(profile):
            commit_locked(args)
    except PROFILE_LOCK.ProfileConfigLockError as exc:
        fail(str(exc))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--channel", choices=tuple(CHANNEL_FIELDS), required=True)
    result.add_argument("--profile", required=True)
    result.add_argument("--role-yaml", required=True)
    result.add_argument("--registry", required=True)
    result.add_argument("--runtime-env", required=True)
    result.add_argument("--done-marker", required=True)
    result.add_argument("--agent-id", required=True)
    result.add_argument("--role-dir", required=True)
    result.add_argument("--profile-name", required=True)
    result.add_argument("--allowed-value", default="")
    result.add_argument("--reference", nargs=2, action="append", default=[])
    result.add_argument("--metadata", nargs=2, action="append", default=[])
    return result


if __name__ == "__main__":
    commit(parser().parse_args())
