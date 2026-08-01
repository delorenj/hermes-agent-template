from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "template" / ".scripts" / "10-hermes-profile.sh"


def test_profile_env_symlink_is_detached_before_sanitization() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    detach = text.index('if [[ -L "$PROFILE_ENV" ]]')
    dereference_copy = text.index('cp -L "$PROFILE_ENV" "$PROFILE_ENV_COPY"')
    replace_link = text.index('mv -fT "$PROFILE_ENV_COPY" "$PROFILE_ENV"')
    sanitize = text.index('python3 - "$PROFILE_ENV"')

    assert detach < dereference_copy < replace_link < sanitize
