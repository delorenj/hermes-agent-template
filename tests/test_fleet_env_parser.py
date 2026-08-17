from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
PARSER = ROOT / "template" / ".scripts" / "lib" / "parse-fleet-env.py"
HEADER = b"PJANGLER_FLEET_ENV_V1"
FOOTER = b"PJANGLER_FLEET_ENV_END"


def _parse(tmp_path: Path, source: str) -> subprocess.CompletedProcess[bytes]:
    fleet = tmp_path / "fleet.env"
    fleet.write_text(source, encoding="utf-8")
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


@pytest.mark.parametrize(
    "source",
    [
        "FIRST=must-not-frame\nFIRST=duplicate\n",
        "FIRST=must-not-frame\nBAD-NAME=value\n",
        "FIRST=must-not-frame\nreadonly SECOND=value\n",
        "FIRST=must-not-frame\nbuiltin() { :; }\n",
        "FIRST=must-not-frame\nSECOND=$(touch /tmp/must-not-run)\n",
        "FIRST=must-not-frame\nSECOND=${HOME}\n",
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
