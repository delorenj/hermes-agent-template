from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "template" / ".scripts" / "10-hermes-profile.sh"


def test_profile_creation_never_clones_default_credentials() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'profile create "$PROFILE_NAME" --no-alias' in text
    assert 'profile create "$PROFILE_NAME" --clone' not in text
    assert 'cp -L "$PROFILE_ENV"' not in text
    assert "approval-gated" in text
