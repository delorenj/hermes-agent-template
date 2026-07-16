#!/usr/bin/env python3
"""Fail closed when a runtime tree contains likely credential material."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

KEY_VALUE = re.compile(
    r"(?im)^\s*[\"']?(?:api[_-]?key|token|secret|password|authorization|cookie|client[_-]?secret|private[_-]?key)[\"']?\s*[:=]\s*[\"']?([^\s\"'#][^\r\n#]*)"
)
TOKEN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|tvly-[A-Za-z0-9_-]{10,}|fc-[A-Za-z0-9_-]{10,}|[0-9]{6,}:[A-Za-z0-9_-]{20,})"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SAFE_VALUES = {"openrouter", "openai", "anthropic", "auto", "none", "null", "false", "true"}
SKIP_PARTS = {".git", "__pycache__"}


def findings(root: Path) -> list[str]:
    result: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.name == "secret-scan.py":
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            result.append(f"forbidden secret file: {path.relative_to(root)}")
            continue
        if path.name in {"auth.json", "auth.lock"} or path.suffix in {".pem", ".key"}:
            result.append(f"forbidden credential file: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN.search(text) or PRIVATE_KEY.search(text):
            result.append(f"credential token pattern: {path.relative_to(root)}")
        for match in KEY_VALUE.finditer(text):
            value = match.group(1).strip().strip("\"'").rstrip(",")
            if (
                not value
                or value.lower() in SAFE_VALUES
                or value.startswith(("${", "$", "op://", "env:", "[REDACTED]"))
                or value.endswith("_ENV")
            ):
                continue
            result.append(f"literal credential assignment: {path.relative_to(root)}")
            break
    return sorted(set(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    issues = findings(args.root.resolve())
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
