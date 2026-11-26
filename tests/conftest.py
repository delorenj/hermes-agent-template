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


def _inside_git_worktree(path: str) -> bool:
    p = Path(path).resolve()
    return any((d / ".git").exists() for d in (p, *p.parents))


# Skillex refuses to write receipts inside a source repository (E_RECEIPT_UNSAFE_PATH). A Claude Code
# shell exports TMPDIR=~/.claude/tmp, which is a git repo, so ~70 skillex/desk-root tests errored at
# setup for every agent that ran the suite from one (2026-11-26). Scratch space must sit outside git.
if _inside_git_worktree(os.environ.get("TMPDIR") or "/tmp"):
    import tempfile

    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = None
