"""The ticket-provider adapters here are a VENDORED MIRROR, not the original.

The canonical copies live in Krebs at `krebs/adapters/tp/`, which
`33god-platform/components/krebs.yaml` has declared as its source of truth all
along. They are mirrored into the template because a deployed employee needs its
adapter rendered into its own role directory, and a role directory cannot reach
into another repo at runtime.

Two consumers, one source: Flume renders this mirror, and pjangler reads Krebs
directly for `pj init --provision-ticket-board`. If the two drift, a project's
board and its PM's board are talked to by two different programs that merely
share a name -- which is the class of bug this file exists to prevent.

Skipped when Krebs is not checked out beside the template; enforced wherever it
is (a 33GOD working tree, and CI).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

ADAPTERS = ("plane.sh", "linear.sh", "trello.sh")
TEMPLATE_ROOT = Path(__file__).resolve().parents[1]
MIRROR_DIR = TEMPLATE_ROOT / "template" / ".scripts" / "providers"


def _krebs_dir() -> Path | None:
    override = os.environ.get("KREBS_TP_ADAPTERS")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None
    # The template is normally checked out at 33GOD/hermes-agent-template or
    # vendored at flume/templates/hermes-agent; walk up for the monorepo root.
    for parent in TEMPLATE_ROOT.parents:
        candidate = parent / "krebs" / "adapters" / "tp"
        if candidate.is_dir():
            return candidate
    return None


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ADAPTERS)
def test_vendored_adapter_matches_krebs(name: str) -> None:
    krebs = _krebs_dir()
    if krebs is None:
        pytest.skip("Krebs is not checked out beside this template; set KREBS_TP_ADAPTERS to enforce")

    canonical = krebs / name
    mirrored = MIRROR_DIR / name
    assert canonical.is_file(), f"canonical adapter missing: {canonical}"
    assert mirrored.is_file(), f"vendored adapter missing: {mirrored}"

    if _digest(canonical) != _digest(mirrored):
        pytest.fail(
            f"{name} has drifted from Krebs.\n"
            f"  canonical: {canonical}\n"
            f"  vendored:  {mirrored}\n"
            f"Edit the Krebs copy, then re-sync: mise run tp:vendor"
        )


def test_no_adapter_exists_only_in_the_mirror() -> None:
    """A provider added here but not to Krebs is invisible to pjangler."""
    krebs = _krebs_dir()
    if krebs is None:
        pytest.skip("Krebs is not checked out beside this template; set KREBS_TP_ADAPTERS to enforce")
    mirrored = {p.name for p in MIRROR_DIR.glob("*.sh")}
    canonical = {p.name for p in krebs.glob("*.sh")}
    assert mirrored <= canonical, (
        f"adapters present only in the template mirror: {sorted(mirrored - canonical)}. "
        "Add them to krebs/adapters/tp/ so project board creation can use them too."
    )
