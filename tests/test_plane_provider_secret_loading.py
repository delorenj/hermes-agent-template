from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


PROVIDER = (
    Path(__file__).resolve().parents[1]
    / "template"
    / ".scripts"
    / "providers"
    / "plane.sh"
)

STRICT_CURL = """#!/usr/bin/env python3
import sys

payload = '{"id":"15258893-0206-4e8f-aea6-340eb217988c","identifier":"LIVE"}'
args = sys.argv[1:]
outfile = args[args.index("-o") + 1]
headerfile = args[args.index("-D") + 1]
with open(outfile, "w", encoding="utf-8") as stream:
    stream.write(payload)
with open(headerfile, "w", encoding="utf-8") as stream:
    stream.write("HTTP/1.1 200 OK\\r\\n")
sys.stdout.write("200")
"""


def test_plane_provider_reads_only_workspace_key_as_inert_dotenv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    provider = repo / "agents" / "hermes" / "pm" / ".scripts" / "providers" / "plane.sh"
    provider.parent.mkdir(parents=True)
    shutil.copy2(PROVIDER, provider)

    board_id = "15258893-0206-4e8f-aea6-340eb217988c"
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "ticket_provider": {
                    "type": "plane",
                    "workspace": "test-space",
                    "board_id": board_id,
                }
            }
        ),
        encoding="utf-8",
    )

    marker = tmp_path / "must-not-exist"
    fleet_env = tmp_path / "fleet.env"
    fleet_env.write_text(
        "\n".join(
            [
                "PLANE_OTHER_API_KEY=not-the-selected-value",
                f"UNRELATED_COMMAND=$(touch {marker})",
                'export PLANE_TEST_SPACE_API_KEY="scoped-test-value"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("PLANE_API_KEY", None)
    env.pop("PLANE_TEST_SPACE_API_KEY", None)
    env["HERMES_FLEET_ENV"] = str(fleet_env)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(STRICT_CURL, encoding="utf-8")
    fake_curl.chmod(0o755)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["sh", str(provider), "resolve"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "provider": "plane",
        "board_id": board_id,
        "board_url": f"https://plane.delo.sh/test-space/projects/{board_id}/issues/",
        "identifier": "LIVE",
    }
    assert not marker.exists()


def test_plane_provider_resolves_workspace_1password_reference(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    provider = repo / "agents" / "hermes" / "pm" / ".scripts" / "providers" / "plane.sh"
    provider.parent.mkdir(parents=True)
    shutil.copy2(PROVIDER, provider)

    board_id = "15258893-0206-4e8f-aea6-340eb217988c"
    (repo / ".project.json").write_text(
        json.dumps(
            {
                "ticket_provider": {
                    "type": "plane",
                    "workspace": "test-space",
                    "board_id": board_id,
                }
            }
        ),
        encoding="utf-8",
    )

    secret_ref = "op://Example/Plane/apiKey"
    fleet_env = tmp_path / "fleet.env"
    fleet_env.write_text(
        f"PLANE_TEST_SPACE_API_KEY={secret_ref}\n",
        encoding="utf-8",
    )

    op_log = tmp_path / "op-read.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_op = fake_bin / "op"
    fake_op.write_text(
        "#!/usr/bin/env sh\n"
        "test \"$1\" = read || exit 64\n"
        "printf '%s' \"$2\" > \"$OP_TEST_LOG\"\n"
        "printf '%s' resolved-test-value\n",
        encoding="utf-8",
    )
    fake_op.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(STRICT_CURL, encoding="utf-8")
    fake_curl.chmod(0o755)

    env = os.environ.copy()
    env.pop("PLANE_API_KEY", None)
    env.pop("PLANE_TEST_SPACE_API_KEY", None)
    env["HERMES_FLEET_ENV"] = str(fleet_env)
    env["OP_TEST_LOG"] = str(op_log)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["sh", str(provider), "resolve"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "provider": "plane",
        "board_id": board_id,
        "board_url": f"https://plane.delo.sh/test-space/projects/{board_id}/issues/",
        "identifier": "LIVE",
    }
    assert op_log.read_text(encoding="utf-8") == secret_ref
