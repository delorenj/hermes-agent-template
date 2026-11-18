from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


DISPATCHER = (
    Path(__file__).resolve().parents[1]
    / "template"
    / ".scripts"
    / "lib"
    / "ticket-provider.sh"
)


def stage_dispatcher(tmp_path: Path, provider_name: str) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / provider_name
    scripts = repo / "agents" / "hermes" / "pm" / ".scripts"
    dispatcher = scripts / "lib" / "ticket-provider.sh"
    dispatcher.parent.mkdir(parents=True)
    shutil.copy2(DISPATCHER, dispatcher)
    providers = scripts / "providers"
    providers.mkdir()
    provider = providers / f"{provider_name}.sh"
    provider.write_text(
        """#!/usr/bin/env sh
set -eu
op="$1"
shift
printf '%s\n' "$op|$*" >> "$TP_TEST_LOG"
case "$op" in
  list_issues) printf '%s\\n' "$TP_TEST_ISSUES" ;;
  transition|comment|get_issue)
    printf 'ok\\n'
    ;;
  *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    provider.chmod(0o755)
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "ticket_provider": {
                    "type": provider_name,
                    "identifier": "JIMB",
                }
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / f"{provider_name}.log"
    env = os.environ.copy()
    env["TP_TEST_LOG"] = str(log)
    return dispatcher, env, log


def run_tp(dispatcher: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; shift; tp "$@"',
            "_",
            str(dispatcher),
            *args,
        ],
        cwd=dispatcher.parents[5],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("provider_name", "human_key", "listed_key"),
    [
        ("plane", "JIMB-207", 207),
        ("trello", "abc123", "abc123"),
    ],
)
def test_mutations_resolve_human_key_to_provider_native_id(
    tmp_path: Path,
    provider_name: str,
    human_key: str,
    listed_key: str | int,
) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, provider_name)
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": listed_key, "title": "Ticket"}]
    )

    transition = run_tp(dispatcher, env, "transition", human_key, "completed")
    comment = run_tp(dispatcher, env, "comment", human_key, "reviewed")

    assert transition.returncode == 0, transition.stderr
    assert comment.returncode == 0, comment.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "list_issues|",
        "transition|provider-native-id completed",
        "list_issues|",
        "comment|provider-native-id reviewed",
    ]


def test_unresolved_human_key_fails_before_mutation(tmp_path: Path) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, "plane")
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": 208, "title": "Different ticket"}]
    )

    result = run_tp(dispatcher, env, "transition", "JIMB-207", "completed")

    assert result.returncode != 0
    assert "could not resolve issue reference" in result.stderr
    assert "ok" not in result.stdout
    assert log.read_text(encoding="utf-8").splitlines() == ["list_issues|"]


@pytest.mark.parametrize("reference", ["", " \t "])
@pytest.mark.parametrize(
    ("operation", "trailing_args"),
    [
        ("get_issue", []),
        ("comment", ["reviewed"]),
        ("transition", ["completed"]),
    ],
)
def test_blank_issue_reference_fails_before_any_provider_call(
    tmp_path: Path,
    reference: str,
    operation: str,
    trailing_args: list[str],
) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, "plane")
    env["TP_TEST_ISSUES"] = json.dumps(
        [{"id": "provider-native-id", "key": "", "title": "Missing normalized key"}]
    )

    result = run_tp(dispatcher, env, operation, reference, *trailing_args)

    assert result.returncode == 2
    assert "non-blank issue reference" in result.stderr
    assert "ok" not in result.stdout
    assert not log.exists()


def test_historical_linear_binding_rejected_before_any_adapter_call(tmp_path: Path) -> None:
    dispatcher, env, log = stage_dispatcher(tmp_path, "linear")
    env["TP_TEST_ISSUES"] = json.dumps([{"id": "old", "key": "TEST-101"}])
    result = run_tp(dispatcher, env, "get_issue", "TEST-101")
    assert result.returncode == 2
    assert "unsupported ticket provider 'linear'" in result.stderr
    assert not log.exists()
