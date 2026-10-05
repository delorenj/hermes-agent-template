#!/usr/bin/env python3
"""Portable employee projection. Flume owns the definition, this template owns rendering.

The registry caller holds its lock first; this adapter takes the established
profile lock before inspecting ANY mutable profile state. No credential values
are read. Owned files/values are checked before publication; foreign edits are
refused rather than adopted. --check is a read-only projection/audit preview.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile


spec = importlib.util.spec_from_file_location("specialist_renderer", Path(__file__).with_name("hermes-profile-config.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def safe(path: Path, directory=False):
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise ValueError(f"unsafe symlink: {parent}")
    if path.exists() and (not path.is_dir() if directory else not path.is_file()):
        raise ValueError(f"unsafe {'directory' if directory else 'file'}: {path}")


def mapping(path: Path):
    safe(path)
    data = r.load_yaml(path)
    if not isinstance(data, dict):
        raise ValueError(f"expected mapping: {path}")
    return data


def digest(text: str):
    return hashlib.sha256(text.encode()).hexdigest()


def refuse_credentials(value, path="config"):
    """Generated projections may carry references, never inherited raw secrets."""
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"(?:^|_)(?:api_key|password|passwd|token|secret|credential)$", str(key), re.I) and isinstance(child, str) and child and not child.startswith("op://"):
                raise ValueError(f"raw credential cannot be projected: {path}.{key}")
            refuse_credentials(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            refuse_credentials(child, path)


def atomic(path: Path, text: str):
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


def project(request, check):
    employee = request["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,57}", employee):
        raise ValueError("unsafe specialist identity")
    if request["memory"]["write_bank"] != f"agent-{employee}":
        raise ValueError("specialist write bank must belong to its identity")
    profile = r.PROFILES / employee
    safe(r.PROFILES, True)
    safe(profile, True)
    if not check:
        r.PROFILES.mkdir(parents=True, exist_ok=True)
    # A nonexistent profile parent has no mutable profile state to snapshot.
    lock = r.PROFILE_LOCK.ProfileConfigLock(profile) if r.PROFILES.exists() else contextlib.nullcontext()
    with lock:
        safe(profile, True)
        state_path = profile / "specialist-projection.json"
        safe(state_path)
        old = json.loads(state_path.read_text()) if state_path.exists() else {}
        if old and (old.get("id") != employee or old.get("definition") != request["definition"]):
            raise ValueError("profile ownership conflict")
        if profile.exists() and not old and list(profile.iterdir()):
            raise ValueError("occupied profile has no specialist ownership receipt")
        # Read config ONLY after locking. A concurrent config writer cannot be lost.
        delta_path = profile / "config.delta.yaml"
        delta = mapping(delta_path)
        metadata = mapping(profile / "profile.yaml")
        memory_path = profile / "hindsight/config.json"
        safe(memory_path)
        memory = json.loads(memory_path.read_text()) if memory_path.exists() else {}
        if not isinstance(memory, dict):
            raise ValueError("hindsight config must be a mapping")
        r.PROFILE_LOCK.test_snapshot_barrier("specialist")
        recall = list(dict.fromkeys([f"agent-{employee}", *request["memory"].get("recall_banks", [])]))
        soul = (f"# {request['display_name']}\n\n{request['charter']['purpose']}\n\n"
                + "\n".join(f"- {line}" for line in request["charter"].get("directives", []))
                + f"\n\nTone: {request['charter'].get('tone', 'direct')}\n\n"
                + f"Personal memory: write only to agent-{employee}.\n"
                + "Recall scope (only these banks): " + ", ".join(recall) + ".\n"
                + "Your working directory is supplied by the caller; it does not change your identity or bank.\n")
        # Inference is pinned independently of the fleet's credentials and channels.
        desired = {
            "model.provider": "automaticai",
            "providers.automaticai.api": "https://api.automaticai.io/v1",
            "providers.automaticai.base_url": "https://api.automaticai.io/v1",
            "providers.automaticai.api_key": "",
            "providers.automaticai.key_cmd": "",
            "providers.automaticai.key_env": "AUTOMATICAI_GATEWAY_KEY",
            "secrets.onepassword.env.AUTOMATICAI_GATEWAY_KEY": f"op://DeLoSecrets/hermes-{employee}/credential",
            "skills.external_dirs": [],
            "skills.project_discovery": False,
            "memory.provider": "hindsight", "memory.memory_enabled": True,
            "memory.user_profile_enabled": False,
            "fallback_providers": [],
            "delegation.provider": "automaticai",
        }
        managed = {}
        for key, wanted in desired.items():
            node = delta
            parts = key.split(".")
            for part in parts[:-1]:
                if part not in node:
                    node[part] = {}
                if not isinstance(node[part], dict):
                    raise ValueError(f"ownership conflict: config {key}")
                node = node[part]
            leaf = parts[-1]
            if leaf in node and node[leaf] != old.get("config", {}).get(key, wanted):
                raise ValueError(f"ownership conflict: config {key}")
            node[leaf] = wanted
            managed[key] = wanted
        # Profile metadata and provider pin preserve unrelated handwritten keys.
        owned_meta = {"name": employee, "display_name": request["display_name"],
                      "flume.employment": "portable-specialist", "flume.definition": request["definition"],
                      "config.inherit_from": "default", "config.save_mode": "delta",
                      "hindsight.write_bank": f"agent-{employee}", "hindsight.recall_banks": recall}
        for key, wanted in owned_meta.items():
            node = metadata
            parts = key.split(".")
            for part in parts[:-1]:
                node.setdefault(part, {})
                if not isinstance(node[part], dict):
                    raise ValueError(f"ownership conflict: profile {key}")
                node = node[part]
            leaf = parts[-1]
            if leaf in node and node[leaf] != old.get("metadata", {}).get(key, wanted):
                raise ValueError(f"ownership conflict: profile {key}")
            node[leaf] = wanted
        if "bank_id" in memory and memory["bank_id"] != f"agent-{employee}":
            raise ValueError("ownership conflict: memory bank")
        memory.update({"bank_id": f"agent-{employee}", "api_url": "https://api.hs.delo.sh", "bank_id_template": ""})
        base = mapping(r.BASE)
        merged = r.deep_merge(base, delta)
        refuse_credentials(merged)
        current_config = profile / "config.yaml"
        safe(current_config)
        if current_config.exists() and digest(current_config.read_text()) != old.get("files", {}).get("config.yaml") and mapping(current_config) != merged:
            raise ValueError("ownership conflict: handwritten config.yaml; retain changes in config.delta.yaml")
        # Inherited list patches cannot re-enable external discovery.
        if merged.get("skills", {}).get("external_dirs") != []:
            raise ValueError("strict specialist refuses external skill discovery patches")
        if not merged.get("model", {}).get("default"):
            raise ValueError("deployment dependency: fleet base declares no default model")
        files = {
            "SOUL.md": soul,
            "config.delta.yaml": r.dump_yaml(delta),
            "config.yaml": r.GENERATED_HEADER + r.dump_yaml(merged),
            "profile.yaml": r.dump_yaml(metadata),
            "hindsight/config.json": json.dumps(memory, indent=2, sort_keys=True) + "\n",
            ".skillex-only": "",
        }
        for name in files:
            path = profile / name
            safe(path)
            if name == "SOUL.md" and path.exists() and digest(path.read_text()) != old.get("files", {}).get(name):
                raise ValueError(f"ownership conflict: {name}")
        safe(profile / ".agents", True)
        if (profile / ".agents").exists():
            raise ValueError("strict specialist refuses profile .agents discovery root")
        receipt = {"id": employee, "definition": request["definition"], "config": managed,
                   "metadata": owned_meta, "files": {k: digest(v) for k, v in files.items()}}
        files["specialist-projection.json"] = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        changes = [name for name, text in files.items() if not (profile / name).exists() or (profile / name).read_text() != text]
        if not check:
            for name, text in files.items():
                atomic(profile / name, text)
        return {"profile": str(profile), "changed": changes, "write_bank": f"agent-{employee}", "recall_banks": recall}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(project(json.load(sys.stdin), args.check)))
    except (ValueError, OSError, r.PROFILE_LOCK.ProfileConfigLockError) as exc:
        sys.exit(f"specialist projection refused: {exc}")
