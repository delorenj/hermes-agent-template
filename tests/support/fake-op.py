#!/usr/bin/python3
"""Minimal stateful 1Password CLI double for provisioning contract tests."""

from __future__ import annotations

import json
import os
import pathlib
import sys


if "UNRELATED_PROVIDER_SECRET" in os.environ:
    raise SystemExit("unrelated provider secret leaked into op child")

home = pathlib.Path(os.environ["HOME"])
store = home / ".fake-onepassword"
store.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]


def documents() -> list[dict]:
    rows = []
    for path in sorted(store.glob("*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def locate(identity: str) -> tuple[pathlib.Path, dict] | None:
    for path in sorted(store.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if identity in {str(document.get("id") or ""), str(document.get("title") or "")}:
            return path, document
    return None


if args[:2] == ["item", "list"]:
    print(json.dumps([{"id": row["id"], "title": row["title"]} for row in documents()]))
elif args[:2] == ["item", "create"]:
    document = json.load(sys.stdin)
    document["id"] = document["title"]
    (store / f"{document['id']}.json").write_text(json.dumps(document), encoding="utf-8")
    print("{}")
elif args[:2] == ["item", "get"]:
    found = locate(args[2])
    if not found:
        raise SystemExit(1)
    print(json.dumps(found[1]))
elif args[:2] == ["item", "edit"]:
    found = locate(args[2])
    if not found:
        raise SystemExit(1)
    document = json.load(sys.stdin)
    found[0].write_text(json.dumps(document), encoding="utf-8")
    print("{}")
elif args[:1] == ["read"]:
    if (home / ".fake-onepassword-outage").exists():
        raise SystemExit(75)
    reference = args[-1]
    _vault, item, field = reference.removeprefix("op://").split("/", 2)
    found = locate(item)
    if not found:
        raise SystemExit(1)
    value = next(
        entry.get("value", "")
        for entry in found[1].get("fields", [])
        if entry.get("id") == field
    )
    print(value)
else:
    raise SystemExit(f"unsupported fake op invocation: {args}")
