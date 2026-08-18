from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
PARSER = ROOT / "template" / ".scripts" / "lib" / "parse-fleet-env.py"
HEADER = b"PJANGLER_FLEET_ENV_V1"
FOOTER = b"PJANGLER_FLEET_ENV_END"
UNICODE_ERROR = b"fleet environment parse error: input and values must be valid UTF-8 Unicode\n"


def _parse(
    tmp_path: Path,
    source: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    fleet = tmp_path / "fleet.env"
    fleet.write_text(source, encoding="utf-8")
    return subprocess.run(
        ["python3", "-I", str(PARSER), str(fleet)],
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _parse_bytes(tmp_path: Path, source: bytes) -> subprocess.CompletedProcess[bytes]:
    fleet = tmp_path / "fleet.env"
    fleet.write_bytes(source)
    return subprocess.run(
        ["python3", "-I", str(PARSER), str(fleet)],
        capture_output=True,
        check=False,
    )


def test_parser_emits_complete_literal_records_only_after_full_parse(
    tmp_path: Path,
) -> None:
    result = _parse(
        tmp_path,
        """# shared fleet configuration
PLAIN=alpha
export URL=https://example.invalid/a=b
SPACES='alpha beta  gamma'
DOUBLE="literal \\$dollar and \\`tick\\`"
ANSI=$'line one\\nline two\\n'
MULTILINE='first physical line
second physical line
'
EMPTY=
""",
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stderr == b""
    assert result.stdout.endswith(FOOTER + b"\0\0")
    assert result.stdout.split(b"\0") == [
        HEADER,
        b"PLAIN=alpha",
        b"URL=https://example.invalid/a=b",
        b"SPACES=alpha beta  gamma",
        b"DOUBLE=literal $dollar and `tick`",
        b"ANSI=line one\nline two\n",
        b"MULTILINE=first physical line\nsecond physical line\n",
        b"EMPTY=",
        FOOTER,
        b"",
        b"",
    ]


def test_parser_supports_only_the_legacy_fleet_home_path_expansion(
    tmp_path: Path,
) -> None:
    result = _parse(
        tmp_path,
        """HERMES_FLEET_HOME=/fleet/from-file
HERMES_FLEET_REGISTRY_FILE=$HERMES_FLEET_HOME/agents-registry.yaml
BRACED=${HERMES_FLEET_HOME}/literal-suffix
""",
        env={"HERMES_FLEET_HOME": "/fleet/from sanitized caller"},
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.split(b"\0") == [
        HEADER,
        b"HERMES_FLEET_HOME=/fleet/from-file",
        b"HERMES_FLEET_REGISTRY_FILE=/fleet/from sanitized caller/agents-registry.yaml",
        b"BRACED=/fleet/from sanitized caller/literal-suffix",
        FOOTER,
        b"",
        b"",
    ]


def test_parser_uses_an_earlier_fleet_home_when_the_caller_did_not_set_one(
    tmp_path: Path,
) -> None:
    inherited = os.environ.copy()
    inherited.pop("HERMES_FLEET_HOME", None)
    fleet = tmp_path / "fleet.env"
    fleet.write_text(
        "HERMES_FLEET_HOME=/fleet/from-file\n"
        "HERMES_FLEET_REGISTRY_FILE=$HERMES_FLEET_HOME/registry.yaml\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["python3", "-I", str(PARSER), str(fleet)],
        capture_output=True,
        check=False,
        env=inherited,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"HERMES_FLEET_REGISTRY_FILE=/fleet/from-file/registry.yaml\0" in result.stdout


@pytest.mark.parametrize(
    "source",
    [
        "FIRST=must-not-frame\nFIRST=duplicate\n",
        "FIRST=must-not-frame\nBAD-NAME=value\n",
        "FIRST=must-not-frame\nreadonly SECOND=value\n",
        "FIRST=must-not-frame\nbuiltin() { :; }\n",
        "FIRST=must-not-frame\nSECOND=$(touch /tmp/must-not-run)\n",
        "FIRST=must-not-frame\nSECOND=$HOME\n",
        "FIRST=must-not-frame\nSECOND=${HOME}\n",
        "FIRST=must-not-frame\nSECOND=${HERMES_FLEET_HOME:-/fallback}\n",
        "FIRST=must-not-frame\nSECOND=$HERMES_FLEET_REPO/suffix\n",
        "FIRST=must-not-frame\nSECOND=$HERMES_FLEET_HOME$(touch /tmp/must-not-run)\n",
        "FIRST=must-not-frame\nSECOND=unquoted value\n",
        "FIRST=must-not-frame\nSECOND='unterminated\n",
    ],
)
def test_parser_rejects_unsupported_or_incomplete_input_without_any_frame(
    tmp_path: Path, source: str
) -> None:
    result = _parse(tmp_path, source)

    assert result.returncode != 0
    assert result.stdout == b""
    assert b"fleet environment parse error" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        b"VALUE=$'\\uD800'\n",
        b"VALUE=$'\\uFDD0'\n",
        b"VALUE=$'\\U0010FFFF'\n",
        b"VALUE=\xff\n",
    ],
)
def test_parser_normalizes_invalid_unicode_without_traceback_or_path(
    tmp_path: Path, source: bytes
) -> None:
    result = _parse_bytes(tmp_path, source)

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == UNICODE_ERROR
    assert b"Traceback" not in result.stderr
    assert str(tmp_path).encode() not in result.stderr
