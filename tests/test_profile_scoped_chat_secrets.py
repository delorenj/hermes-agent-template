from __future__ import annotations

import importlib.util
import stat
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate-profile-scoped-chat-secrets.py"
SPEC = importlib.util.spec_from_file_location("chat_secret_migration", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_dry_run_never_changes_files_or_reports_values(tmp_path: Path) -> None:
    central = tmp_path / "central.env"
    target = tmp_path / "profile.env"
    _write(
        central,
        "KIMI_API_KEY=provider-secret\nSLACK_APP_TOKEN=app-secret\n"
        "SLACK_BOT_TOKEN=bot-secret\nSLACK_ALLOWED_USERS=U123\n",
    )

    moved = MODULE.migrate(central, target, MODULE.DEFAULT_KEYS, apply=False)

    assert moved == list(MODULE.DEFAULT_KEYS)
    assert not target.exists()
    assert "SLACK_BOT_TOKEN=bot-secret" in central.read_text(encoding="utf-8")


def test_apply_moves_only_chat_keys_and_creates_private_backup(tmp_path: Path) -> None:
    central = tmp_path / "central.env"
    target = tmp_path / "profile.env"
    _write(
        central,
        "KIMI_API_KEY=provider-secret\nSLACK_APP_TOKEN=app-secret\n"
        "SLACK_BOT_TOKEN=bot-secret\nSLACK_ALLOWED_USERS=U123\n",
    )

    MODULE.migrate(central, target, MODULE.DEFAULT_KEYS, apply=True)

    assert central.read_text(encoding="utf-8") == "KIMI_API_KEY=provider-secret\n"
    target_text = target.read_text(encoding="utf-8")
    assert "SLACK_APP_TOKEN=app-secret" in target_text
    assert "SLACK_BOT_TOKEN=bot-secret" in target_text
    assert "SLACK_ALLOWED_USERS=U123" in target_text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    backups = list(tmp_path.glob("central.env.bak-*-before-chat-scope"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_refuses_target_symlink_to_shared_env(tmp_path: Path) -> None:
    central = tmp_path / "central.env"
    target = tmp_path / "profile.env"
    _write(
        central,
        "SLACK_APP_TOKEN=app\nSLACK_BOT_TOKEN=bot\nSLACK_ALLOWED_USERS=user\n",
    )
    target.symlink_to(central)

    try:
        MODULE.migrate(central, target, MODULE.DEFAULT_KEYS, apply=True)
    except ValueError as exc:
        assert "not a symlink" in str(exc)
    else:
        raise AssertionError("expected symlink safety failure")


def test_refuses_conflicting_target_value(tmp_path: Path) -> None:
    central = tmp_path / "central.env"
    target = tmp_path / "profile.env"
    _write(
        central,
        "SLACK_APP_TOKEN=app\nSLACK_BOT_TOKEN=bot\nSLACK_ALLOWED_USERS=user\n",
    )
    _write(target, "SLACK_BOT_TOKEN=different\n")

    try:
        MODULE.migrate(central, target, MODULE.DEFAULT_KEYS, apply=True)
    except ValueError as exc:
        assert "different values" in str(exc)
    else:
        raise AssertionError("expected conflict safety failure")
