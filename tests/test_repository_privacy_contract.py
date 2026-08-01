from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_host_local_runtime_payloads_are_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".codegraph/daemon.pid" in gitignore
    assert ".omo/run-continuation/" in gitignore


def test_host_local_runtime_payloads_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    forbidden = re.compile(
        r"(?:^|/)(?:\.omo/run-continuation/|\.codegraph/[^/]+\.(?:pid|sock|socket)$)"
        r"|(?:^|/)(?:sessions?|runtime-state)/"
        r"|(?:^|/)[^/]+\.(?:pid|sock|socket)$"
    )

    offenders = sorted(path for path in tracked if path and forbidden.search(path))
    assert offenders == [], f"tracked host-local runtime payloads: {offenders}"
