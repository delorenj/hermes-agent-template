from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
ACTIVE_OPERATOR_SURFACES = (
    ROOT / "README.md",
    ROOT / "docs" / "operations.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "sentinel.md",
    ROOT / "docs" / "sentinel" / "README.md",
    ROOT / "docs" / "sentinel" / "architecture.md",
    ROOT / "docs" / "sentinel" / "development.md",
    ROOT / "template" / ".scripts" / "config.example.toml",
)


def test_active_operator_surfaces_describe_only_pure_local_runtime() -> None:
    forbidden = {
        "per-agent remote runtime name": r"agent-hm-",
        "active runtime repository": r"(?:github\s+)?runtime repositor(?:y|ies)|runtime repo(?:s)?\b",
        "runtime submodule": r"runtime.{0,60}submodule|submodule.{0,60}runtime",
        "runtime LFS": r"git\s+lfs",
        "runtime push": r"git\s+push|commit\s*\+\s*push",
        "cascading profile deletion": r"hermes\s+profile\s+delete|cascad(?:e|es|ing)",
        "blanket runtime deletion": r"rm\s+-rf[^\n]*(?:runtime|agents/hermes|\$RUNTIME|\$ROLE_DIR)",
        "runtime submodule deletion": r"git\s+submodule\s+deinit|\.git/modules/agents/hermes|git\s+rm[^\n]*runtime",
    }

    for path in ACTIVE_OPERATOR_SURFACES:
        content = path.read_text(encoding="utf-8")
        for label, pattern in forbidden.items():
            assert re.search(pattern, content, re.IGNORECASE | re.DOTALL) is None, (
                f"{path.relative_to(ROOT)} contains obsolete {label} guidance"
            )


def test_operations_make_recovery_and_retirement_boundaries_explicit() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    for required in (
        "does **not** ship automatic backup",
        "verified filesystem backup",
        "Hindsight can restore only",
        "secret manager can restore only",
        "Retire an agent (preserves runtime by default)",
        "Optional destructive runtime removal (separate operation)",
        "Type REMOVE LOCAL RUNTIME $RUNTIME",
        'test "$RUNTIME" = "$PROJECT/agents/hermes/$ROLE/runtime"',
        'sha256sum -c "${BACKUP}.sha256"',
    ):
        assert required in operations


def test_legacy_runtime_owner_config_is_explicitly_inert() -> None:
    config = (ROOT / "template" / ".scripts" / "config.example.toml").read_text(
        encoding="utf-8"
    )
    github_block = config.split("[github]", 1)[1].split("[plane]", 1)[0]

    assert "LEGACY INERT COMPATIBILITY METADATA ONLY" in github_block
    assert 'runtime_repo_owner = ""' in github_block
    assert "does not create, attach" in github_block


def test_retired_checkpoint_runbook_is_historical_evidence_only() -> None:
    runbook = (
        ROOT / "docs" / "runbooks" / "runtime-checkpoint-repair.md"
    ).read_text(encoding="utf-8")

    assert "Historical evidence only" in runbook
    assert "do not execute" in runbook
    assert "commands have intentionally been removed" in runbook
