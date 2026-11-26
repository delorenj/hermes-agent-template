"""Every desk reads the fleet's A2A ingress settings from its own 1Password-mapped secret scope.

One multiplexed gateway serves every profile on one A2A listener, exposed with a shared bearer token.
A profile never reads the process environment, so the settings have to be in each profile's own scope:
the renderer pins the op:// references into every generated config, current desk or new hire.
"""
import copy
import importlib.util
from pathlib import Path

import pytest

RENDERER = Path(__file__).parents[1] / "scripts" / "hermes-profile-config.py"


def load_renderer():
    spec = importlib.util.spec_from_file_location("profile_config", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = {
    "platforms": {"a2a": {"enabled": True}},
    "secrets": {"onepassword": {"enabled": True, "env": {"PLANE_API_KEY": "op://DeLoSecrets/Plane/Main/key"}}},
}


@pytest.mark.parametrize("profile", ["demo-pm", "new-hire"])
def test_every_desk_gets_the_a2a_refs(tmp_path, profile):
    mod = load_renderer()
    before = copy.deepcopy(BASE)
    env = mod.expected_config(BASE, {}, profile, registry=tmp_path / "missing")["secrets"]["onepassword"]["env"]
    assert BASE == before
    assert env["PLANE_API_KEY"] == "op://DeLoSecrets/Plane/Main/key"
    assert set(mod.A2A_INGRESS_ENV) <= set(env)
    for name in mod.A2A_INGRESS_ENV:
        assert env[name].startswith("op://DeLoSecrets/"), "a reference, never a literal"


def test_a_delta_cannot_override_the_fleet_ingress(tmp_path):
    mod = load_renderer()
    delta = {"secrets": {"onepassword": {"env": {"A2A_HOST": "127.0.0.1", "TELEGRAM_BOT_TOKEN": "op://DeLoSecrets/x/y"}}}}
    env = mod.expected_config(BASE, delta, "demo-pm", registry=tmp_path / "missing")["secrets"]["onepassword"]["env"]
    assert env["A2A_HOST"] == mod.A2A_INGRESS_REFS["A2A_HOST"]
    assert env["TELEGRAM_BOT_TOKEN"] == "op://DeLoSecrets/x/y"


@pytest.mark.parametrize("base", [
    {"platforms": {"a2a": {"enabled": False}}, "secrets": BASE["secrets"]},
    {"gateway": {"platforms": {"a2a": {"enabled": False}}}, "secrets": BASE["secrets"]},
    {"platforms": {"a2a": {"enabled": True}}},
    {"platforms": {"a2a": {"enabled": True}}, "secrets": {"onepassword": {"enabled": False}}},
])
def test_a_desk_without_a2a_or_a_vault_source_is_left_alone(tmp_path, base):
    mod = load_renderer()
    config = mod.expected_config(base, {}, "demo-pm", registry=tmp_path / "missing")
    env = ((config.get("secrets") or {}).get("onepassword") or {}).get("env") or {}
    assert not set(mod.A2A_INGRESS_ENV) & set(env)


def test_the_pin_is_a_renderer_owned_path(tmp_path):
    """Writers strip pins before comparing a config to its delta, so a pin is never read as drift."""
    mod = load_renderer()
    config = mod.expected_config(BASE, {}, "demo-pm", registry=tmp_path / "missing")
    stripped = mod.without_render_pins(config)
    assert mod.is_render_of(config, BASE, {}, "demo-pm", registry=tmp_path / "missing")
    assert not set(mod.A2A_INGRESS_ENV) & set(stripped["secrets"]["onepassword"]["env"])
