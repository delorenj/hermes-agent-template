"""Named desks route Bloodbank turns; only the host owns the consumer."""
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


@pytest.mark.parametrize("profile", ["demo-pm", "fleet-bloodbank-gateway", "new-hire"])
def test_named_profile_never_owns_bloodbank_transport(tmp_path, profile):
    mod = load_renderer()
    base = {
        "gateway": {"platforms": {
            "bloodbank": {"enabled": True, "extra": {"durable_name": "shared"}},
            "telegram": {"enabled": True},
        }},
        "platform_toolsets": {"bloodbank": ["terminal", "file", "delegation"]},
    }
    before = copy.deepcopy(base)
    expected = mod.expected_config(base, {}, profile, registry=tmp_path / "missing")
    assert expected["gateway"]["platforms"]["bloodbank"]["enabled"] is False
    expected["gateway"]["platforms"]["bloodbank"]["enabled"] = True
    assert expected == before
    assert base == before


def test_render_check_absorb_accepts_old_transport_pin(tmp_path):
    mod = load_renderer()
    base = {"gateway": {"platforms": {"bloodbank": {"enabled": True}}}}
    expected = mod.expected_config(base, {}, "demo-pm", registry=tmp_path / "missing")
    assert expected["gateway"]["platforms"]["bloodbank"]["enabled"] is False
    assert mod.is_render_of(base, base, {}, "demo-pm", registry=tmp_path / "missing")
    assert mod.without_render_pins(expected) == mod.without_render_pins(base)
    assert mod.minimal_delta(expected, expected) == {}
