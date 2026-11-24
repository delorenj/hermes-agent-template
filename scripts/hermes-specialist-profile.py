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
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
import yaml

PENDING = ".specialist-pending.json"
MANAGED_FILES = {"SOUL.md", "config.delta.yaml", "config.yaml", "profile.yaml",
                 "hindsight/config.json", ".skillex-only", ".no-bundled-skills", "specialist-projection.json"}
LEGACY_MANAGED_FILES = MANAGED_FILES - {".no-bundled-skills"}


spec = importlib.util.spec_from_file_location("specialist_renderer", Path(__file__).with_name("hermes-profile-config.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def load_voice_config_module():
    """THE shared voice contract transform (template/.scripts/lib/voice-config.py).

    The same reconcile_delta the PM path (20-runtime-repo.sh -> set_voice.sh /
    voice-config CLI) uses; never a private copy. Trusted path only.
    """
    source = Path(__file__).parent.parent / "template/.scripts/lib/voice-config.py"
    safe(source)
    if not source.is_file():
        raise ValueError(f"trusted voice config helper is unavailable: {source}")
    spec = importlib.util.spec_from_file_location("specialist_voice_config", source)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve cls.__module__ through sys.modules on Python 3.14+
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def template_config():
    """The template TOML (~/.config/hermes-agent-template/config.toml) PM scripts read
    through config_get; the specialist adapter resolves the same fleet.vox_* keys."""
    path = Path(os.environ.get(
        "HERMES_TEMPLATE_CONFIG",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hermes-agent-template/config.toml"))
    safe(path)
    if not path.exists():
        return {}
    try:
        import tomllib
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid template config: {path}: {exc}") from exc
    fleet = data.get("fleet", {})
    if not isinstance(fleet, dict):
        raise ValueError(f"template config fleet must be a mapping: {path}")
    return fleet


def voice_override(delta, plugin, old):
    """Keep a profile override, including either leaf edited by the voice writer."""
    tts = delta.get("tts", {})
    if not isinstance(tts, dict) or not isinstance(tts.get(plugin, {}), dict):
        raise ValueError("voice contract refused: TTS delta must be a mapping")
    provider_voice = tts.get(plugin, {}).get("voice")
    generic_voice = tts.get("voice")
    if provider_voice is not None and generic_voice is not None and provider_voice != generic_voice:
        previous = old.get("voice", {}).get("voice")
        if previous == provider_voice:
            return generic_voice
        if previous == generic_voice:
            return provider_voice
        raise ValueError("ownership conflict: conflicting profile voice overrides")
    return provider_voice if provider_voice is not None else generic_voice


def plugin_state(profile, name, target):
    """The sole allowed leaf symlink; all ancestors must be real directories."""
    safe(profile / "plugins", True)
    safe(profile / "plugins/tts", True)
    path = profile / name
    if path.is_symlink():
        if os.readlink(path) != target:
            raise ValueError(f"ownership conflict: {path}")
        return target
    if path.exists():
        raise ValueError(f"ownership conflict: {path}")
    return None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def same_projection(path: Path, text: str) -> bool:
    """Bytes, or for structured files the parsed value: Hermes re-dumps YAML in its own style."""
    if not path.exists():
        return False
    current = path.read_text()
    if current == text:
        return True
    try:
        if path.suffix == ".yaml":
            return yaml.safe_load(current) == yaml.safe_load(text)
        if path.suffix == ".json":
            return json.loads(current) == json.loads(text)
    except (yaml.YAMLError, ValueError):
        return False
    return False


def digest(text: str):
    return hashlib.sha256(text.encode()).hexdigest()


def receipt_digest(name: str, text: str):
    """Receipt fingerprint: the parsed value for structured files, so key order and quoting don't count."""
    try:
        if name.endswith(".yaml"):
            return digest(json.dumps(yaml.safe_load(text), sort_keys=True, default=str))
        if name.endswith(".json"):
            return digest(json.dumps(json.loads(text), sort_keys=True))
    except (yaml.YAMLError, ValueError):
        pass
    return digest(text)


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
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return True


_MISSING = object()


def _lookup(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return _MISSING
        data = data[part]
    return data


def _leaves(data, prefix=()):
    if isinstance(data, dict) and data:
        for key, child in data.items():
            yield from _leaves(child, prefix + (key,))
    else:
        yield prefix, data


def config_drift(current, base, found_delta, employee, delta_changed=False):
    """Classify how a generated config.yaml departs from the render of base + the delta it was rendered from.

    Returns ``(conflicts, replaced)`` as dotted paths. A *conflict* is content a re-render would silently
    destroy and nobody else holds: a value the delta owns that config.yaml now contradicts, or a key that
    neither the base nor the delta defines (a handwritten addition). *Replaced* is every other difference:
    a base-owned leaf that holds an older or rewritten value. That is what a moved fleet base leaves
    behind, and what Hermes' own config migrations write in place on update (`hermes update`
    re-stamps every profile's config.yaml; 2026-11-24 it rewrote all 53 desks and every specialist
    launch was refused as "handwritten"). The base is THE owner of those leaves, so the projection
    re-renders them, the same contract the PM renderer applies, and keeps a backup of the bytes it replaced.

    ``delta_changed``: the delta on disk is not the one the receipt recorded, so config.yaml was rendered
    from an older delta and a delta-owned difference is that edit not yet rendered, not a contradiction.
    A list-patched path (``x-pjangler-merge``) is delta-owned only in its additions and removals; the
    rest of that list belongs to the base.
    """
    expected = r.without_render_pins(r.expected_config(base, found_delta, employee))
    current = r.without_render_pins(current)
    patches = (found_delta.get(r.LIST_PATCH_KEY) or {}).get("list_patches") or {}

    def owned(path):
        return any(_lookup(found_delta, path[:i]) is not _MISSING
                   and not isinstance(_lookup(found_delta, path[:i]), dict) for i in range(1, len(path) + 1)) \
            or _lookup(found_delta, path) is not _MISSING

    def differs(path, value):
        """A conflict, or None when the difference is replaceable."""
        dotted = ".".join(map(str, path))
        rule = patches.get(dotted)
        if isinstance(rule, dict) and isinstance(value, list):
            honoured = (all(item in value for item in rule.get("add") or [])
                        and not any(item in value for item in rule.get("remove") or []))
            return None if honoured or delta_changed else dotted
        return dotted if owned(path) and not delta_changed else None

    conflicts, replaced = [], []
    for path, value in _leaves(current):
        dotted = ".".join(map(str, path))
        want = _lookup(expected, path)
        if want is _MISSING:
            scalar_parent = next((path[:i] for i in range(1, len(path))
                                  if _lookup(expected, path[:i]) is not _MISSING
                                  and not isinstance(_lookup(expected, path[:i]), dict)), None)
            if scalar_parent is not None:
                # A shape change of a rendered value (scalar or list became a mapping), not an addition.
                conflict = differs(scalar_parent, value)
            elif value in ({}, None, [], ""):
                continue  # an empty section loses nothing on re-render
            else:
                conflict = dotted
        elif want == value:
            continue
        else:
            conflict = differs(path, value)
        (conflicts if conflict else replaced).append(conflict or dotted)
    return list(dict.fromkeys(conflicts)), replaced


def member_consumer(profile: str) -> str:
    """Same deterministic gateway consumer contract as Flume's member helper."""
    name = f"hermes-{profile}"
    if len(name) <= 26:
        return name
    suffix = hashlib.sha1(profile.encode()).hexdigest()[:4]
    keep = 26 - len("hermes-") - 1 - len(suffix)
    return f"hermes-{profile[:keep].rstrip('-_')}-{suffix}"


def project(request, check):
    employee = request["id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,57}", employee):
        raise ValueError("unsafe specialist identity")
    if employee == "default":
        raise ValueError("reserved Hermes profile identity: default")
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
        pending_path = profile / PENDING
        safe(pending_path)
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
        if pending is not None:
            if (not isinstance(pending, dict) or pending.get("id") != employee
                    or pending.get("definition") != request["definition"]
                    or set(pending.get("files", {})) not in (MANAGED_FILES, LEGACY_MANAGED_FILES)
                    or set(pending.get("before", {})) != set(pending.get("files", {}))
                    or not isinstance(pending.get("links", {}), dict)
                    or set(pending.get("links", {})) != set(pending.get("before_links", {}))):
                raise ValueError("pending profile ownership conflict")
            # Old journals lack links; new ones cover link publication too.
            for name, target in pending.get("links", {}).items():
                if (not re.fullmatch(r"plugins/tts/[a-z0-9][a-z0-9_-]{0,63}", name)
                        or not isinstance(target, str) or not Path(target).is_absolute()
                        or pending["before_links"][name] not in (None, target)):
                    raise ValueError("invalid pending plugin publication")
                actual = plugin_state(profile, name, target)
                if actual not in (pending["before_links"][name], target):
                    raise ValueError(f"ownership conflict: interrupted publication {name}")
            # Only the recorded pre-write bytes or transaction output are ours.
            for name, text in pending["files"].items():
                path = profile / name
                safe(path)
                if not isinstance(text, str):
                    raise ValueError("invalid pending profile publication")
                actual = digest(path.read_text()) if path.exists() else None
                if actual not in (pending["before"][name], digest(text)):
                    raise ValueError(f"ownership conflict: interrupted publication {name}")

        def owned_text(name):
            if pending is not None:
                return pending["files"][name]
            path = profile / name
            safe(path)
            return path.read_text() if path.exists() else None

        def owned_mapping(name):
            data = yaml.safe_load(owned_text(name) or "{}")
            if not isinstance(data, dict):
                raise ValueError(f"expected mapping: {profile / name}")
            return data

        old = json.loads(owned_text("specialist-projection.json") or "{}")
        if old and (old.get("id") != employee or old.get("definition") != request["definition"]):
            raise ValueError("profile ownership conflict")
        if profile.exists() and not old and list(profile.iterdir()):
            raise ValueError("occupied profile has no specialist ownership receipt")
        # Read config ONLY after locking. A concurrent config writer cannot be lost.
        delta_path = profile / "config.delta.yaml"
        delta = owned_mapping("config.delta.yaml")
        # The generated config.yaml was rendered from the delta as found, before this run's pins.
        found_delta = copy.deepcopy(delta)
        metadata = owned_mapping("profile.yaml")
        memory_path = profile / "hindsight/config.json"
        safe(memory_path)
        memory = json.loads(owned_text("hindsight/config.json") or "{}")
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
            "providers.automaticai.enabled": True,
            "providers.automaticai.api": "https://api-direct.automaticai.io/v1",
            "providers.automaticai.base_url": "https://api-direct.automaticai.io/v1",
            "providers.automaticai.api_key": "",
            "providers.automaticai.key_cmd": "",
            "providers.automaticai.key_env": "AUTOMATICAI_GATEWAY_KEY",
            "dashboard.enabled": False,
            "dashboard.basic_auth.enabled": False,
            "dashboard.basic_auth.secret": "",
            # A specialist's credential is provisioned by Flume after the
            # profile exists; the projection owns no gateway token or field.
            "secrets.onepassword.env.AUTOMATICAI_GATEWAY_KEY": f"op://DeLoSecrets/yeurk5dpqkaarspvsn3cjtmkki/{member_consumer(employee)}",
            "skills.external_dirs": [],
            "skills.project_discovery": False,
            # Global skill selection is SHARED operating guidance (the canonical
            # ~/.agents/skills.json root), and Skillex profile sync unions it with
            # the role's own set/pack selection. Hermes itself never reads this
            # key (agent/skill_utils.py discovers via external_dirs and
            # project_discovery only), so enabling it widens no Hermes discovery
            # root; strict Skillex ownership still comes from external_dirs: []
            # plus the .skillex-only marker, which the check below enforces.
            "skills.inherit_global": True,
            # skills/ belongs to Skillex alone; what the employee learns (skill_manage) lands beside it,
            # and the curator, which archives into skills/.archive, stays off for a strict desk.
            "skills.create_dir": "learned-skills",
            "curator.enabled": False,
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
            if key == "secrets.onepassword.env.AUTOMATICAI_GATEWAY_KEY":
                valid_refs = {wanted}
                if "config" in old and key in old["config"]:
                    valid_refs.add(old["config"][key])
                if leaf in node:
                    current_val = node[leaf]
                    if current_val in valid_refs:
                        wanted = current_val
                    else:
                        raise ValueError(f"ownership conflict: config {key}")
                elif "config" in old and old["config"].get(key):
                    wanted = old["config"][key]
            elif leaf in node and node[leaf] != old.get("config", {}).get(key, wanted):
                # The fleet route migration can reach the delta before Flume's
                # receipt. Adopt ONLY this known owned transition, never an
                # arbitrary endpoint or a change to another managed setting.
                migrated_route = (
                    key in ("providers.automaticai.api", "providers.automaticai.base_url")
                    and old.get("config", {}).get(key) == "https://api.automaticai.io/v1"
                    and node[leaf] == wanted
                )
                if not migrated_route:
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
        memory.setdefault("recall_types", ["world", "experience", "observation"])
        # Plugin/voice parity with the PM path (20-runtime-repo.sh): the same
        # fleet.vox_* defaults, the same delta transform (voice-config.py), the
        # same plugins/tts/<plugin> symlink. Definition `voice:` wins over the
        # fleet default; tts slugs are non-secret. A missing optional plugin dir
        # defers the link but never the config contract.
        voice_config = load_voice_config_module()
        fleet_cfg = template_config()
        plugin_name = os.environ.get("VOX_PLUGIN_NAME") or str(fleet_cfg.get("vox_plugin_name") or "vox")
        plugin_dir = Path(os.path.expandvars(os.path.expanduser(
            os.environ.get("VOX_PLUGIN_DIR") or str(fleet_cfg.get("vox_plugin_dir") or f"~/code/voxxy/plugins/tts/{plugin_name}"))))
        if not plugin_dir.is_absolute():
            raise ValueError("voice contract refused: plugin directory must be absolute")
        base = mapping(r.BASE)
        current_voice = voice_override(delta, plugin_name, old)
        if "voice" in request:
            voice = request["voice"]
            previous = old.get("voice", {}).get("voice", voice)
            if current_voice is not None and current_voice not in (previous, voice):
                raise ValueError("ownership conflict: definition voice conflicts with profile override")
        else:
            # No definition voice: keep the desk's own pin, else an explicit
            # VOX_VOICE, else None -- the desk inherits the fleet base voice
            # (~/.hermes/config.yaml), so a default change reaches it on render.
            voice = current_voice if current_voice is not None else (os.environ.get("VOX_VOICE") or None)
        try:
            if voice is not None and not isinstance(voice, str):
                raise voice_config.ContractError("invalid TTS voice name")
            voice_config.reconcile_delta(delta, plugin_name, voice)
        except voice_config.ContractError as exc:
            raise ValueError(f"voice contract refused: {exc}")
        # Preserve a desk-specific endpoint; otherwise use the same service key
        # as fleet-sync. The plugin reads tts.<plugin>.base_url, not vox_url.
        endpoint = ((delta["tts"].get(plugin_name) or {}).get("base_url") or os.environ.get("VOX_URL") or
                    fleet_cfg.get("vox_url") or ((base.get("tts") or {}).get(plugin_name) or {}).get("base_url") or
                    "https://vox.delo.sh")
        if not isinstance(endpoint, str):
            raise ValueError("voice contract refused: invalid Vox URL")
        url = urlsplit(endpoint)
        if (url.scheme not in ("http", "https") or not url.netloc or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError("voice contract refused: Vox URL must be an HTTP(S) endpoint without credentials")
        delta["tts"].setdefault(plugin_name, {})["base_url"] = endpoint
        managed["tts.provider"] = plugin_name
        if voice is not None:
            managed["tts.voice"] = voice
            managed[f"tts.{plugin_name}.voice"] = voice
        managed[f"tts.{plugin_name}.base_url"] = endpoint
        # THE expected config (base + delta + renderer pins), the same value `check` compares against.
        merged = r.expected_config(base, delta, employee)
        for name, value in (("config.delta", delta), ("config", merged), ("profile", metadata), ("hindsight", memory)):
            refuse_credentials(value, name)
        current_config = profile / "config.yaml"
        safe(current_config)
        replaced_drift = []
        if pending is None and current_config.exists() and old.get("files", {}).get("config.yaml") not in (digest(current_config.read_text()), receipt_digest("config.yaml", current_config.read_text())) and not any(r.is_render_of(mapping(current_config), base, d, employee) for d in (found_delta, delta)):
            found_delta_text = owned_text("config.delta.yaml") or ""
            delta_changed = old.get("files", {}).get("config.delta.yaml") not in (
                digest(found_delta_text), receipt_digest("config.delta.yaml", found_delta_text))
            conflicts, replaced_drift = config_drift(mapping(current_config), base, found_delta, employee, delta_changed)
            if conflicts:
                shown = ", ".join(conflicts[:6]) + (f" (+{len(conflicts) - 6} more)" if len(conflicts) > 6 else "")
                raise ValueError(f"ownership conflict: handwritten config.yaml ({shown}); retain changes in config.delta.yaml")
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
            ".no-bundled-skills": "",
        }
        for name in files:
            path = profile / name
            safe(path)
            if pending is None and name == "SOUL.md" and path.exists() and digest(path.read_text()) != old.get("files", {}).get(name):
                raise ValueError(f"ownership conflict: {name}")
        safe(profile / ".agents", True)
        if (profile / ".agents").exists():
            raise ValueError("strict specialist refuses profile .agents discovery root")
        # The tts plugin symlink mirrors the PM install in 20-runtime-repo.sh:
        # plugins/tts/<plugin> -> the fleet plugin dir. It is a convergence link,
        # never owned bytes; foreign occupation refuses, a missing optional
        # plugin dir defers with an explicit note (the config contract stands).
        voice_note = None
        plugin_link = profile / "plugins/tts" / plugin_name
        link_name = f"plugins/tts/{plugin_name}"
        link_ok = plugin_state(profile, link_name, str(plugin_dir)) is not None
        links = {link_name: str(plugin_dir)} if plugin_dir.is_dir() else {}
        if pending is not None and any(links.get(name) != target for name, target in pending.get("links", {}).items()):
            raise ValueError("pending plugin publication changed; restore the original plugin configuration")
        if not plugin_dir.is_dir():
            voice_note = f"optional {plugin_name} plugin not installed at {plugin_dir}; voice activation deferred"
        receipt = {"id": employee, "definition": request["definition"], "config": managed,
                   "metadata": owned_meta, "files": {k: receipt_digest(k, v) for k, v in files.items()},
                   "voice": {"plugin": plugin_name, "voice": voice,
                             "plugin_dir": str(plugin_dir), "installed": plugin_dir.is_dir()}}
        refuse_credentials(receipt, "receipt")
        files["specialist-projection.json"] = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        changes = [name for name, text in files.items() if not same_projection(profile / name, text)]
        if links and not link_ok and link_name not in changes:
            changes.append(link_name)
        if pending is not None:
            changes.append(PENDING)
        drift_backup = None
        if not check and replaced_drift and "config.yaml" in changes:
            # The replaced bytes stay recoverable: same backup root and layout as the PM renderer.
            drift_backup = r.backup([current_config], f"specialist-drift-{employee}")
        if not check and changes:
            journal = {"id": employee, "definition": request["definition"], "files": files,
                       "before": {name: digest((profile / name).read_text()) if (profile / name).exists() else None for name in files},
                       "links": links, "before_links": {name: plugin_state(profile, name, target) for name, target in links.items()}}
            atomic(pending_path, json.dumps(journal, sort_keys=True, indent=2) + "\n")
            sync_directory(profile.parent)
            if os.environ.get("FLUME_TEST_SPECIALIST_INTERRUPT_AFTER") == PENDING:
                os._exit(91)
            for name, target in links.items():
                if plugin_state(profile, name, target) is None:
                    plugin_link.parent.mkdir(parents=True, exist_ok=True)
                    # Exclusive creation: never unlink or replace a foreign leaf.
                    plugin_link.symlink_to(target)
                    sync_directory(plugin_link.parent)
                    sync_directory(plugin_link.parent.parent)
                    sync_directory(profile)
                if os.environ.get("FLUME_TEST_SPECIALIST_INTERRUPT_AFTER") == name:
                    os._exit(91)
            for name, text in files.items():
                atomic(profile / name, text)
                if os.environ.get("FLUME_TEST_SPECIALIST_INTERRUPT_AFTER") == name:
                    os._exit(91)
            pending_path.unlink()
            sync_directory(profile)
        result = {"profile": str(profile), "changed": changes, "write_bank": f"agent-{employee}", "recall_banks": recall}
        if voice_note:
            result["voice_deferred"] = voice_note
        if replaced_drift:
            # Base-owned leaves a moved base or a Hermes migration left in config.yaml; re-rendered, never adopted.
            result["replaced_drift"] = {"paths": replaced_drift,
                                        "backup": str(drift_backup / profile.name / "config.yaml") if drift_backup else None}
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(project(json.load(sys.stdin), args.check)))
    except (ValueError, OSError, r.PROFILE_LOCK.ProfileConfigLockError) as exc:
        sys.exit(f"specialist projection refused: {exc}")
