#!/usr/bin/env python3
"""Store one process-only secret in 1Password and print its op:// reference.

The secret is read from stdin and is never placed in argv, a temporary file,
stdout, or an ``op`` child environment.  Item JSON travels only over an
anonymous pipe to the 1Password CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys


def fail(message: str) -> "None":
    raise SystemExit(f"1Password secret storage failed: {message}")


def op_run(args: list[str], *, payload: str | None = None) -> subprocess.CompletedProcess[str]:
    allowed = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SystemRoot",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
        "OP_ACCOUNT",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_LOAD_DESKTOP_APP_SETTINGS",
        "OP_SERVICE_ACCOUNT_TOKEN",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update(
        (name, value)
        for name, value in os.environ.items()
        if name.startswith("OP_SESSION_")
    )
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [OP, *args],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
        timeout=30,
    )


OP = shutil.which("op") or ""
if not OP:
    fail("the op CLI is not installed")

if len(sys.argv) == 3 and sys.argv[1] == "--validate-reference":
    reference = sys.argv[2]
    if not reference.startswith("op://") or any(ch in reference for ch in "\r\n\0"):
        fail("invalid 1Password reference")
    resolved = op_run(["read", "--", reference])
    if resolved.returncode != 0 or not resolved.stdout.rstrip("\n"):
        fail("the configured reference did not resolve")
    raise SystemExit(0)

if len(sys.argv) != 3:
    fail("usage: store-onepassword-secret.py <vault> <item>")

vault, item = sys.argv[1:]
safe = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,126}")
if not safe.fullmatch(vault) or not safe.fullmatch(item):
    fail("vault or item name contains unsupported characters")

secret = sys.stdin.read()
if secret.endswith("\n"):
    secret = secret[:-1]
if not secret:
    fail("refusing to store an empty value")

listed = op_run(["item", "list", "--vault", vault, "--format=json"])
if listed.returncode != 0:
    fail("vault access/authentication was rejected")
try:
    rows = json.loads(listed.stdout)
except (TypeError, json.JSONDecodeError):
    fail("op item list returned invalid JSON")

matches = [row for row in rows if isinstance(row, dict) and row.get("title") == item]
if len(matches) > 1:
    fail("multiple items have the requested title")

if matches:
    item_id = str(matches[0].get("id") or "")
    if not item_id:
        fail("the existing item has no id")
    fetched = op_run(["item", "get", item_id, "--vault", vault, "--format=json"])
    if fetched.returncode != 0:
        fail("the existing item could not be read")
    try:
        document = json.loads(fetched.stdout)
    except (TypeError, json.JSONDecodeError):
        fail("op item get returned invalid JSON")
    fields = document.get("fields")
    if not isinstance(fields, list):
        fail("the existing item has no fields list")
    concealed_field = next(
        (field for field in fields if isinstance(field, dict) and field.get("id") == "password"),
        None,
    )
    if concealed_field is None:
        concealed_field = {
            "id": "password",
            "type": "CONCEALED",
            "purpose": "PASSWORD",
            "label": "password",
        }
        fields.append(concealed_field)
    concealed_field["value"] = secret
    stored = op_run(
        ["item", "edit", item_id, "--vault", vault],
        payload=json.dumps(document),
    )
else:
    document = {
        "title": item,
        "category": "PASSWORD",
        "fields": [
            {
                "id": "password",
                "type": "CONCEALED",
                "purpose": "PASSWORD",
                "label": "password",
                "value": secret,
            },
            {
                "id": "notesPlain",
                "type": "STRING",
                "purpose": "NOTES",
                "label": "notesPlain",
                "value": "Managed by hermes-agent-template; rotate through the provisioner.",
            },
        ],
    }
    stored = op_run(
        ["item", "create", "--vault", vault, "-"],
        payload=json.dumps(document),
    )

if stored.returncode != 0:
    fail("op rejected the item update")

reference = f"op://{vault}/{item}/password"
verified = op_run(["read", "--", reference])
if verified.returncode != 0 or verified.stdout.rstrip("\n") != secret:
    fail("the stored reference did not resolve to the supplied value")

print(reference)
