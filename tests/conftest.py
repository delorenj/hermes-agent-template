"""Bind every test subprocess to the renderer under test.

Provisioning helpers (channel-transaction.py, voice-config.py, _lib.sh, 80-registry.sh) compute a
generated profile config through THE canonical renderer, located by lib/profile-renderer.py. Tests
run those helpers from disposable role copies with a disposable HOME, where neither the relative
lookup nor ~/code/33GOD/hermes-agent-template exists; pin the renderer of THIS checkout instead of
silently testing whatever the host's canonical checkout holds. A test that needs a different (or
missing) renderer still overrides PROFILE_RENDERER explicitly.
"""
import os
from pathlib import Path

os.environ.setdefault(
    "PROFILE_RENDERER", str(Path(__file__).resolve().parents[1] / "scripts" / "hermes-profile-config.py")
)
