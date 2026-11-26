"""The renderer's fleet A2A ingress pin, for tests that compare a generated config to its delta.

``hermes-profile-config.py`` pins the A2A ingress op:// refs into every generated
``secrets.onepassword.env``. A test about some OTHER credential (Slack, Telegram, ...) must compare
the rest of that map to the delta, and the pinned refs to the renderer's own, so it neither breaks
on the pin nor lets a wrong or missing pin pass.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_RENDERER = Path(__file__).resolve().parents[2] / "scripts" / "hermes-profile-config.py"


def _renderer():
    spec = importlib.util.spec_from_file_location("profile_config_pins", _RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unpinned_env(generated_env: dict) -> dict:
    """*generated_env* minus the A2A ingress refs, after asserting they are exactly the renderer's."""
    renderer = _renderer()
    env = dict(generated_env)
    pinned = {name: env.pop(name, None) for name in renderer.A2A_INGRESS_ENV}
    assert pinned == renderer.A2A_INGRESS_REFS
    return env
