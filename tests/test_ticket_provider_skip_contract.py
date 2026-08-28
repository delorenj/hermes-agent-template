from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SCRIPTS = ROOT / "template" / ".scripts"
GNU_LOADER_CONTROL_STEMS = (
    "LD_ASSUME_KERNEL",
    "LD_AUDIT",
    "LD_BIND_NOT",
    "LD_BIND_NOW",
    "LD_DEBUG",
    "LD_DEBUG_OUTPUT",
    "LD_DYNAMIC_WEAK",
    "LD_HWCAP_MASK",
    "LD_LIBRARY_PATH",
    "LD_ORIGIN_PATH",
    "LD_POINTER_GUARD",
    "LD_PREFER_MAP_32BIT_EXEC",
    "LD_PRELOAD",
    "LD_PROFILE",
    "LD_PROFILE_OUTPUT",
    "LD_SHOW_AUXV",
    "LD_TRACE_LOADED_OBJECTS",
    "LD_TRACE_PRELINKING",
    "LD_USE_LOAD_BIAS",
    "LD_VERBOSE",
    "LD_WARN",
)
GNU_LOADER_CONTROL_KEYS = tuple(
    key
    for stem in GNU_LOADER_CONTROL_STEMS
    for key in (stem, f"{stem}_32", f"{stem}_64")
)
FLEET_FRAME_HEADER = "PJANGLER_FLEET_ENV_V1"
FLEET_FRAME_FOOTER = "PJANGLER_FLEET_ENV_END"


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TEMPLATE_SCRIPTS / "42-ticket-provider.sh", scripts)
    shutil.copy2(TEMPLATE_SCRIPTS / "_lib.sh", scripts)
    shutil.copytree(TEMPLATE_SCRIPTS / "lib", scripts / "lib")
    shutil.copytree(TEMPLATE_SCRIPTS / "providers", scripts / "providers")

    (role / "role.yaml").write_text(
        """repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
model:
  provider: ""
  name: "inherited-model"
  base_url: ""
  api_mode: ""
  key_env: ""
telegram:
  bot_username: demo_pm_bot
plane:
  workspace: test-space
runtime:
  github_owner: test
  github_repo: agent-hm-demo-pm
ticket_provider:
  name: plane
  workspace: old-space
  board_id: ""
  project: ""
""",
        encoding="utf-8",
    )
    (project / ".project.json").write_text(
        json.dumps(
            {
                "project_name": "Demo",
                "project_slug": "demo",
                "repo_path": str(project),
                "ticket_provider": {
                    "type": "plane",
                    "workspace": "test-space",
                    "identifier": "DEMO",
                    "board_id": "",
                    "state": "planned",
                },
                "agents": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "provider-calls.log"
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROVIDER_CALL_LOG"
case "$*" in
  *'/projects/?per_page=200'*) printf '%s\n' '[{"id":"granted-board","name":"Demo"}]' ;;
  *'/projects/granted-board/'*) printf '%s\n' '{"id":"granted-board","name":"Demo","identifier":"LIVE"}' ;;
  *) printf '%s\n' '{}' ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PLANE_", "TRELLO_", "LINEAR_"))
        and key not in {"SKIP_PLANE", "TICKET_PROVIDER"}
    }
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HERMES_FLEET_ENV": str(home / ".hermes" / "missing-fleet.env"),
            "HERMES_TEMPLATE_CONFIG": str(home / ".config" / "missing-config.toml"),
            "PROVIDER_CALL_LOG": str(call_log),
            # Deliberately ambient: SKIP_PLANE must dominate available credentials.
            "PLANE_API_KEY": "ambient-plane-test-key",
            "TRELLO_KEY": "ambient-trello-test-key",
            "TRELLO_TOKEN": "ambient-trello-test-token",
        }
    )
    return project, role, env, call_log


def _run(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(role / ".scripts" / "42-ticket-provider.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _source_library(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; '
                "if [[ -v PJAN67_SAFE_MUTABLE ]]; then "
                "PJAN67_SAFE_MUTABLE=\"${PJAN67_SAFE_MUTABLE}:mutable\"; "
                "export PJAN67_SAFE_MUTABLE; fi; "
                "env -0"
            ),
            "pjan67-lib-probe",
            str(role / ".scripts" / "_lib.sh"),
        ],
        env=env,
        capture_output=True,
        check=False,
    )


def _source_library_child_env(role: Path, env: dict[str, str]) -> dict[str, str]:
    result = _source_library(role, env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return {
        key.decode(): value.decode()
        for entry in result.stdout.split(b"\0")
        if entry
        for key, value in [entry.split(b"=", 1)]
    }


def _source_library_after_failure(
    role: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, str]]:
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'if source "$1"; then source_status=0; else source_status=$?; fi; '
                "set +e; env -0; exit \"$source_status\""
            ),
            "pjan67-lib-failure-probe",
            str(role / ".scripts" / "_lib.sh"),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    child_env = {
        key.decode(): value.decode()
        for entry in result.stdout.split(b"\0")
        if entry
        for key, value in [entry.split(b"=", 1)]
    }
    return result, child_env


def _builtin_hijack_source(marker: Path, framed_payload: str) -> str:
    return "\n".join(
        [
            "export PJAN67_ATOMIC_FIRST=must-not-escape",
            "builtin() {",
            f"  command printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}",
            "  case \"$1\" in",
            "    printf)",
            "      if [[ \"${3:-}\" == PJANGLER_FLEET_ENV_V1 ]]; then",
            f"        command printf '%b' {shlex.quote(framed_payload)}",
            "      fi",
            "      return 0",
            "      ;;",
            "    read) return 1 ;;",
            "    *) return 0 ;;",
            "  esac",
            "}",
            "export -f builtin",
            "readonly -f builtin",
            "",
        ]
    )


def _run_raw_fleet_stream(
    role: Path,
    env: dict[str, str],
    stream: Path,
    *,
    producer_status: int = 0,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, str]]:
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; '
                'exec {fleet_fd}< <(/usr/bin/cat "$2"; exit "$3"); fleet_pid=$!; '
                "if import_fleet_environment_stream \"$fleet_fd\" \"$fleet_pid\"; "
                "then import_status=0; else import_status=$?; fi; "
                "set +e; env -0; exit \"$import_status\""
            ),
            "pjan67-raw-frame-probe",
            str(role / ".scripts" / "_lib.sh"),
            str(stream),
            str(producer_status),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    child_env = {
        key.decode(): value.decode()
        for entry in result.stdout.split(b"\0")
        if entry and b"=" in entry
        for key, value in [entry.split(b"=", 1)]
    }
    return result, child_env


def _fleet_authority_fixture(
    tmp_path: Path, caller_skip: str, fleet_skip: str
) -> tuple[Path, dict[str, str]]:
    role = tmp_path / "project" / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TEMPLATE_SCRIPTS / "_lib.sh", scripts)
    shutil.copytree(TEMPLATE_SCRIPTS / "lib", scripts / "lib")
    fleet_env = tmp_path / "home" / ".hermes" / "fleet.env"
    fleet_env.parent.mkdir(parents=True)
    fleet_env.write_text(
        "\n".join(
            [
                f"export SKIP_PLANE={fleet_skip}",
                "export PLANE_API_KEY=fleet-generic-sentinel",
                "export PLANE_33GOD_API_KEY=fleet-workspace-sentinel",
                "export PLANE_TEST_SPACE_API_KEY=fleet-dynamic-sentinel",
                "export TRELLO_KEY=fleet-trello-key-sentinel",
                "export TRELLO_TOKEN=fleet-trello-token-sentinel",
                "export LINEAR_API_KEY=fleet-linear-sentinel",
                "export LD_SDK_KEY=fleet-non-loader-functional-value",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PLANE_", "TRELLO_", "LINEAR_"))
        and key != "SKIP_PLANE"
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "HERMES_FLEET_ENV": str(fleet_env),
            "HERMES_TEMPLATE_CONFIG": str(tmp_path / "missing-config.toml"),
            "SKIP_PLANE": caller_skip,
            "PJAN67_BASH_FUNCTION_LOG": str(tmp_path / "bash-function.log"),
        }
    )
    return role, env


def test_fleet_import_never_executes_a_readonly_builtin_function(
    tmp_path: Path,
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    marker = tmp_path / "builtin-hijack.log"
    fleet_env = Path(env["HERMES_FLEET_ENV"])
    fleet_env.write_text(
        _builtin_hijack_source(
            marker,
            f"{FLEET_FRAME_HEADER}\\0{FLEET_FRAME_FOOTER}\\0",
        ),
        encoding="utf-8",
    )

    result, child_env = _source_library_after_failure(role, env)

    assert result.returncode != 0
    assert b"fleet environment import failed" in result.stderr
    assert "PJAN67_ATOMIC_FIRST" not in child_env
    assert not marker.exists(), "fleet data must never execute as shell code"


@pytest.mark.parametrize(
    "case,framed_payload",
    [
        (
            "malformed",
            f"{FLEET_FRAME_HEADER}\\0PJAN67_ATOMIC_FIRST=leaked\\0MALFORMED\\0{FLEET_FRAME_FOOTER}\\0",
        ),
        (
            "duplicate",
            f"{FLEET_FRAME_HEADER}\\0PJAN67_ATOMIC_FIRST=one\\0PJAN67_ATOMIC_FIRST=two\\0{FLEET_FRAME_FOOTER}\\0",
        ),
        ("truncated", f"{FLEET_FRAME_HEADER}\\0PJAN67_ATOMIC_FIRST=leaked\\0"),
        (
            "unterminated",
            f"{FLEET_FRAME_HEADER}\\0PJAN67_ATOMIC_FIRST=leaked\\0{FLEET_FRAME_FOOTER}\\0",
        ),
    ],
)
def test_fleet_import_hijack_cannot_partially_apply_forged_frames(
    tmp_path: Path, case: str, framed_payload: str
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    marker = tmp_path / f"builtin-{case}.log"
    Path(env["HERMES_FLEET_ENV"]).write_text(
        _builtin_hijack_source(marker, framed_payload),
        encoding="utf-8",
    )

    result, child_env = _source_library_after_failure(role, env)

    assert result.returncode != 0
    assert b"fleet environment import failed" in result.stderr
    assert "PJAN67_ATOMIC_FIRST" not in child_env
    assert not marker.exists(), "frame generation must not execute fleet shell code"


@pytest.mark.parametrize(
    "case,payload,producer_status",
    [
        (
            "malformed",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0MALFORMED\0"
            b"PJANGLER_FLEET_ENV_END\0\0",
            0,
        ),
        (
            "duplicate",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=one\0"
            b"PJAN67_ATOMIC_FIRST=two\0PJANGLER_FLEET_ENV_END\0\0",
            0,
        ),
        (
            "invalid-name",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0BAD-NAME=value\0"
            b"PJANGLER_FLEET_ENV_END\0\0",
            0,
        ),
        (
            "unsafe-family",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0LD_AUDIT=/tmp/nope\0"
            b"PJANGLER_FLEET_ENV_END\0\0",
            0,
        ),
        ("truncated", b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0", 0),
        (
            "unterminated-footer",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0"
            b"PJANGLER_FLEET_ENV_END\0",
            0,
        ),
        (
            "child-failure",
            b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=leaked\0"
            b"PJANGLER_FLEET_ENV_END\0\0",
            37,
        ),
    ],
)
def test_raw_fleet_frames_are_validated_completely_before_any_apply(
    tmp_path: Path, case: str, payload: bytes, producer_status: int
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    Path(env["HERMES_FLEET_ENV"]).unlink()
    stream = tmp_path / f"{case}.frames"
    stream.write_bytes(payload)

    result, child_env = _run_raw_fleet_stream(
        role, env, stream, producer_status=producer_status
    )

    assert result.returncode != 0
    assert b"fleet environment frame rejected" in result.stderr
    assert "PJAN67_ATOMIC_FIRST" not in child_env


def test_complete_raw_fleet_frame_applies_only_after_validation(tmp_path: Path) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    Path(env["HERMES_FLEET_ENV"]).unlink()
    stream = tmp_path / "complete.frames"
    stream.write_bytes(
        b"PJANGLER_FLEET_ENV_V1\0PJAN67_ATOMIC_FIRST=complete\0"
        b"PJANGLER_FLEET_ENV_END\0\0"
    )

    result, child_env = _run_raw_fleet_stream(role, env, stream)

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert child_env["PJAN67_ATOMIC_FIRST"] == "complete"


def test_fleet_apply_rolls_back_if_any_assignment_fails(tmp_path: Path) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    Path(env["HERMES_FLEET_ENV"]).unlink()
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; '
                "fleet_keys=(PJAN67_ATOMIC_FIRST BAD-NAME); "
                "fleet_values=(leaked rejected); "
                "if apply_fleet_environment_records fleet_keys fleet_values; "
                "then apply_status=0; else apply_status=$?; fi; "
                "if [[ -v PJAN67_ATOMIC_FIRST ]]; then exit 91; fi; "
                "exit \"$apply_status\""
            ),
            "pjan67-apply-rollback-probe",
            str(role / ".scripts" / "_lib.sh"),
        ],
        env=env,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.returncode != 91, "the first assignment escaped rollback"
    assert b"fleet environment apply failed" in result.stderr


@pytest.mark.parametrize(
    "caller_state",
    [
        {"SKIP_PLANE": "1"},
        {"SKIP_PLANE": "1", "MCP_LIVE": "1"},
    ],
    ids=["live-false-no-board-grant", "live-true-skip-plane"],
)
def test_skip_plane_exits_before_provider_or_binding_effects(
    tmp_path: Path, caller_state: dict[str, str]
) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    env.update(caller_state)
    project_before = (project / ".project.json").read_bytes()
    role_before = (role / "role.yaml").read_bytes()

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not call_log.exists(), "SKIP_PLANE must prevent every provider/curl call"
    assert (project / ".project.json").read_bytes() == project_before
    assert (role / "role.yaml").read_bytes() == role_before
    assert not (role / ".scripts" / ".done-42-ticket-provider").exists()
    assert not (role / ".scripts" / ".provision.log").exists()


def test_explicit_board_grant_reaches_real_provider_adapter(tmp_path: Path) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    env["SKIP_PLANE"] = "0"

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").strip(), "positive grant must reach fake curl"
    manifest = json.loads((project / ".project.json").read_text(encoding="utf-8"))
    assert manifest["ticket_provider"]["board_id"] == "granted-board"
    assert manifest["ticket_provider"]["identifier"] == "LIVE"
    assert manifest["ticket_provider"]["state"] == "linked"
    assert "board_url" not in manifest["ticket_provider"]
    assert manifest["agents"]["demo-pm"] == {
        "role": "pm",
        "role_dir": "agents/hermes/pm",
        "provisioning_state": "linked",
    }
    role_manifest = yaml.safe_load((role / "role.yaml").read_text(encoding="utf-8"))
    assert role_manifest["model"]["name"] == "inherited-model"
    assert role_manifest["ticket_provider"]["name"] == "plane"
    assert role_manifest["ticket_provider"]["workspace"] == "test-space"
    assert role_manifest["plane"]["workspace"] == "test-space"
    assert (role / ".scripts" / ".done-42-ticket-provider").exists()


def test_done_marker_rerun_canonicalizes_live_plane_binding_and_preserves_unrelated_keys(
    tmp_path: Path,
) -> None:
    project, role, env, _call_log = _fixture(tmp_path)
    manifest_path = project / ".project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ticket_provider"].update(
        {
            "board_id": "granted-board",
            "identifier": "STALE",
            "state": "planned",
            "board_url": "https://stale.invalid/split-brain",
        }
    )
    manifest["unrelated"] = {"preserve": [1, 2, 3]}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (role / ".scripts" / ".done-42-ticket-provider").touch()
    env["SKIP_PLANE"] = "0"

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    provider = updated["ticket_provider"]
    # Two separate claims, two separate stamps. Plane resolved the board (so
    # board_confirmed_at is what "linked" rests on) AND handed back its own key
    # (so identifier_source is "provider", dated by identifier_fetched_at).
    stamps = {
        key: provider.pop(key)
        for key in ("identifier_fetched_at", "board_confirmed_at")
        if key in provider
    }
    assert provider == {
        "type": "plane",
        "workspace": "test-space",
        "identifier": "LIVE",
        "identifier_source": "provider",
        "board_id": "granted-board",
        "state": "linked",
    }
    assert sorted(stamps) == ["board_confirmed_at", "identifier_fetched_at"]
    assert all(value.endswith("Z") for value in stamps.values()), stamps
    assert updated["unrelated"] == {"preserve": [1, 2, 3]}
    assert "revalidating canonical board binding" in result.stderr


@pytest.mark.parametrize("malformed", ["{", "[]\n", "null\n"])
def test_malformed_project_manifest_is_preserved_before_any_provider_call(
    tmp_path: Path, malformed: str
) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    manifest_path = project / ".project.json"
    manifest_path.write_text(malformed, encoding="utf-8")
    before = manifest_path.read_bytes()
    env["SKIP_PLANE"] = "0"

    result = _run(role, env)

    assert result.returncode != 0
    assert "malformed .project.json" in result.stderr
    assert manifest_path.read_bytes() == before
    assert not call_log.exists()
    assert not (role / ".scripts" / ".done-42-ticket-provider").exists()


def test_concurrent_board_bootstrap_converges_through_one_create_transaction(
    tmp_path: Path,
) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    fake_curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PROVIDER_CALL_LOG"
case "$*" in
  *'/projects/?per_page=200'*) printf '%s\n' '[]' ;;
  *'-X POST '*'/projects/'*)
    printf '%s\n' create >> "${PROVIDER_CALL_LOG}.creates"
    printf '%s\n' '{"id":"converged-board","identifier":"LIVE"}'
    ;;
  *'/projects/converged-board/'*)
    printf '%s\n' '{"id":"converged-board","name":"Demo","identifier":"LIVE"}'
    ;;
  *) printf '%s\n' '{}' ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env["SKIP_PLANE"] = "0"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _run(role, env), range(2)))

    assert [result.returncode for result in results] == [0, 0], [
        result.stderr for result in results
    ]
    creates = Path(f"{call_log}.creates").read_text(encoding="utf-8").splitlines()
    assert creates == ["create"]
    calls = call_log.read_text(encoding="utf-8")
    assert calls.count("/projects/?per_page=200") == 1
    manifest = json.loads((project / ".project.json").read_text(encoding="utf-8"))
    assert manifest["ticket_provider"]["board_id"] == "converged-board"
    assert manifest["ticket_provider"]["identifier"] == "LIVE"
    assert manifest["ticket_provider"]["state"] == "linked"
    assert manifest["agents"] == {
        "demo-pm": {
            "role": "pm",
            "role_dir": "agents/hermes/pm",
            "provisioning_state": "linked",
        }
    }
    assert not list(project.glob("..project.json.ticket-provider-*"))


def test_skip_plane_scrubs_fleet_rehydrated_provider_authority_from_children(
    tmp_path: Path,
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")

    child_env = _source_library_child_env(role, env)

    assert child_env["SKIP_PLANE"] == "1", "fleet.env cannot weaken caller authority"
    for key in (
        "PLANE_API_KEY",
        "PLANE_33GOD_API_KEY",
        "PLANE_TEST_SPACE_API_KEY",
        "TRELLO_KEY",
        "TRELLO_TOKEN",
        "LINEAR_API_KEY",
    ):
        assert key not in child_env, f"no-board child inherited {key}"
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "BASH_ENV",
        "ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        *GNU_LOADER_CONTROL_KEYS,
        "GLIBC_TUNABLES",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "BASHOPTS",
        "SHELLOPTS",
        "BASH_COMPAT",
        "BASH_LOADABLES_PATH",
        "BASH_XTRACEFD",
        "PROMPT_COMMAND",
        "PS4",
    ):
        assert key not in child_env, f"deferred child inherited interpreter injection variable {key}"
    assert not any(key.startswith("BASH_FUNC_") for key in child_env)
    assert not Path(env["PJAN67_BASH_FUNCTION_LOG"]).exists()
    assert child_env["LD_SDK_KEY"] == "fleet-non-loader-functional-value"
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert child_env["PYTHONSAFEPATH"] == "1"


def test_explicit_board_grant_preserves_fleet_provider_authority(
    tmp_path: Path,
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="0", fleet_skip="1")

    child_env = _source_library_child_env(role, env)

    assert child_env["SKIP_PLANE"] == "0", "fleet.env cannot revoke caller grant"
    assert child_env["PLANE_API_KEY"] == "fleet-generic-sentinel"
    assert child_env["PLANE_33GOD_API_KEY"] == "fleet-workspace-sentinel"
    assert child_env["PLANE_TEST_SPACE_API_KEY"] == "fleet-dynamic-sentinel"
    assert child_env["TRELLO_KEY"] == "fleet-trello-key-sentinel"
    assert child_env["TRELLO_TOKEN"] == "fleet-trello-token-sentinel"
    assert child_env["LINEAR_API_KEY"] == "fleet-linear-sentinel"
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "BASH_ENV",
        "ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        *GNU_LOADER_CONTROL_KEYS,
        "GLIBC_TUNABLES",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "BASHOPTS",
        "SHELLOPTS",
        "BASH_COMPAT",
        "BASH_LOADABLES_PATH",
        "BASH_XTRACEFD",
        "PROMPT_COMMAND",
        "PS4",
    ):
        assert key not in child_env, f"granted provider child inherited interpreter injection variable {key}"
    assert not any(key.startswith("BASH_FUNC_") for key in child_env)
    assert not Path(env["PJAN67_BASH_FUNCTION_LOG"]).exists()
    assert child_env["LD_SDK_KEY"] == "fleet-non-loader-functional-value"
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert child_env["PYTHONSAFEPATH"] == "1"


def test_fleet_import_preserves_supported_literal_values_and_precedence(
    tmp_path: Path,
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="0", fleet_skip="1")
    fleet_env = Path(env["HERMES_FLEET_ENV"])
    controlled_path = env["PATH"]
    env["HERMES_FLEET_BIN"] = "/explicit/hermes must win"
    fleet_env.write_text(
        fleet_env.read_text(encoding="utf-8")
        + "\n".join(
            [
                "export HERMES_FLEET_BIN='/fleet/hermes must lose'",
                "export HERMES_FLEET_REPO='/fleet/repo with spaces'",
                "export PJAN67_SAFE_SPACES='alpha beta  gamma'",
                "export PJAN67_SAFE_EQUALS='left=middle=right'",
                "export PJAN67_SAFE_NEWLINES=$'line one\\nline two\\n'",
                "export PJAN67_SAFE_MULTILINE='first physical line\nsecond physical line\n'",
                "export PJAN67_SAFE_MUTABLE='safe value'",
                "export PATH=/fleet/path/must/not/win",
                "",
            ]
        ),
        encoding="utf-8",
    )

    child_env = _source_library_child_env(role, env)

    assert child_env["PATH"] == controlled_path
    assert child_env["HERMES_FLEET_BIN"] == "/explicit/hermes must win"
    assert child_env["HERMES_BIN"] == "/explicit/hermes must win"
    assert child_env["HERMES_FLEET_REPO"] == "/fleet/repo with spaces"
    assert child_env["HERMES_AGENT_REPO"] == "/fleet/repo with spaces"
    assert child_env["PJAN67_SAFE_SPACES"] == "alpha beta  gamma"
    assert child_env["PJAN67_SAFE_EQUALS"] == "left=middle=right"
    assert child_env["PJAN67_SAFE_NEWLINES"] == "line one\nline two\n"
    assert child_env["PJAN67_SAFE_MULTILINE"] == "first physical line\nsecond physical line\n"
    assert child_env["PJAN67_SAFE_MUTABLE"] == "safe value:mutable"
    assert not Path(env["PJAN67_BASH_FUNCTION_LOG"]).exists()


@pytest.mark.parametrize(
    "fleet_source",
    [
        "export PJAN67_PARTIAL_IMPORT=must-not-escape\nreturn 37\n",
        "export PJAN67_PARTIAL_IMPORT=must-not-escape\nif [[\n",
    ],
    ids=["explicit-source-failure", "syntax-error"],
)
def test_fleet_import_fails_closed_on_source_errors(
    tmp_path: Path, fleet_source: str
) -> None:
    role, env = _fleet_authority_fixture(tmp_path, caller_skip="1", fleet_skip="0")
    Path(env["HERMES_FLEET_ENV"]).write_text(fleet_source, encoding="utf-8")

    result = _source_library(role, env)

    assert result.returncode != 0
    assert b"fleet environment import failed" in result.stderr
    assert result.stdout == b""
    assert not Path(env["PJAN67_BASH_FUNCTION_LOG"]).exists()


@pytest.mark.parametrize(
    "script",
    ["01-config.sh", "05-fleet-env.sh", "10-hermes-profile.sh", "80-registry.sh"],
)
def test_host_state_guard_exits_before_library_or_host_writes(tmp_path: Path, script: str) -> None:
    role = tmp_path / "project" / "agents" / "hermes" / "pm"
    scripts = role / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(TEMPLATE_SCRIPTS / script, scripts / script)
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".config" / "hermes-agent-template" / "config.toml"
    registry = home / ".hermes" / "agents-registry.yaml"
    env = {
        **os.environ,
        "HOME": str(home),
        "SKIP_HOST_STATE": "1",
        "HERMES_TEMPLATE_CONFIG": str(config),
        "HERMES_FLEET_REGISTRY_FILE": str(registry),
    }

    result = subprocess.run(
        ["bash", str(scripts / script)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not config.exists()
    assert not registry.exists()
    assert not (home / ".hermes" / "profiles").exists()
    assert not (scripts / ".provision.log").exists()


def test_explicit_project_root_cannot_climb_into_enclosing_checkout(tmp_path: Path) -> None:
    project, role, env, call_log = _fixture(tmp_path)
    shutil.rmtree(project / ".git")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    outer_manifest = tmp_path / ".project.json"
    outer_before = b'{"agents": {}}\n'
    outer_manifest.write_bytes(outer_before)
    env.update({"SKIP_PLANE": "0", "PJANGLER_PROJECT_ROOT": str(project)})

    result = _run(role, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.exists(), "the granted provider still runs inside the explicit target"
    assert outer_manifest.read_bytes() == outer_before
    manifest = json.loads((project / ".project.json").read_text(encoding="utf-8"))
    assert manifest["agents"]["demo-pm"]["role_dir"] == "agents/hermes/pm"
