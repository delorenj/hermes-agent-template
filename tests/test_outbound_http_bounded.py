"""Every outbound HTTP call a template script makes must be bounded in time.

2026-11-02: `flume hire` ran the Plane provider with no curl timeout, so one
half-open connection could hang a hire forever with no output. A call is
bounded when curl carries both a connect timeout and a total max time (as
flags, or as `connect-timeout`/`max-time` lines in a `--config -` stdin config),
or when Python's urlopen is given a `timeout=`.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "template" / ".scripts"


def _scripts():
    for path in sorted(SCRIPTS.rglob("*")):
        if path.is_file() and path.suffix in {".sh", ".py", ""} and "__pycache__" not in path.parts:
            try:
                yield path, path.read_text()
            except UnicodeDecodeError:
                continue


def _code_lines(text: str):
    for number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield number, line


def test_every_curl_invocation_has_connect_and_total_timeouts():
    unbounded = []
    for path, text in _scripts():
        lines = text.splitlines()
        for number, line in _code_lines(text):
            if not re.search(r"(^|[|(\s])curl\s", line):
                continue
            # The invocation plus the config block piped into it (`{ printf ...; } | curl --config -`).
            window = "\n".join(lines[max(0, number - 8):number + 4])
            if not (re.search(r"connect-timeout", window) and re.search(r"max-time", window)):
                unbounded.append(f"{path.relative_to(SCRIPTS)}:{number}: {line.strip()}")
    assert not unbounded, "unbounded curl calls:\n" + "\n".join(unbounded)


def test_every_urlopen_has_a_timeout():
    unbounded = [
        f"{path.relative_to(SCRIPTS)}:{number}"
        for path, text in _scripts()
        for number, line in _code_lines(text)
        if "urlopen(" in line and "timeout" not in line
    ]
    assert not unbounded, "urlopen without timeout: " + ", ".join(unbounded)
