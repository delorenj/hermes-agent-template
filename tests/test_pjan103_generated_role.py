"""PJAN-103: render the real role through Copier, not a Jinja string surrogate."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("provider,expected", [("plane", '  unstarted: "Todo"'), ("trello", '  unstarted: ""')])
def test_generated_role_lane_default(tmp_path: Path, provider: str, expected: str) -> None:
    copier = shutil.which("copier")
    if copier is None:
        pytest.skip("Copier not installed")
    assert copier is not None
    dest = tmp_path / "role"
    result = subprocess.run(
        [copier, "copy", "-T", "--trust", "--defaults", "-d", "target_repo=fixture",
         "-d", "role=pm", "-d", f"ticket_provider={provider}", str(ROOT), str(dest)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    role = (dest / "role.yaml").read_text()
    assert expected in role
    if provider == "plane":
        assert "  in_review:" not in role
        assert "lifecycle:review" in role
        assert "lifecycle:needs-input" in role
