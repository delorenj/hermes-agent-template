"""Unsupported stored and explicit providers must stop before adapter dispatch."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "template/.scripts/lib/ticket-provider.sh"


@pytest.mark.parametrize("provider", ["linear", "jira", ""])
@pytest.mark.parametrize("explicit", [False, True])
def test_unsupported_provider_never_runs_adapter(tmp_path: Path, provider: str, explicit: bool) -> None:
    role = tmp_path / "project/agents/hermes/pm"
    scripts = role / ".scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers" / f"{provider}.sh").write_text("#!/bin/sh\nprintf called > \"$CALL_LOG\"\n")
    (role / "role.yaml").write_text("ticket_provider:\n  name: plane\n")
    (tmp_path / "project/.project.json").write_text(json.dumps({"ticket_provider": {"type": provider, "board_id": "old"}}))
    log = tmp_path / "called"
    script = f'source "{scripts / "lib/ticket-provider.sh"}"; tp list_issues'
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log)}
    if explicit:
        env["TICKET_PROVIDER"] = provider or "unknown"
    result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
    assert result.returncode == 2
    if explicit and not provider:
        assert "unsupported ticket provider" in result.stderr
    elif provider:
        assert f"unsupported ticket provider '{provider}'" in result.stderr
    else:
        assert "unsupported ticket provider" in result.stderr
    if not (explicit and not provider):
        assert "supported: plane, trello" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("manifest", [None, "{", json.dumps({"ticket_provider": {}})])
def test_missing_or_invalid_manifest_does_not_accept_explicit_provider(
    tmp_path: Path, manifest: str | None
) -> None:
    scripts = tmp_path / "project/agents/hermes/pm/.scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers/plane.sh").write_text('#!/bin/sh\nprintf called > "$CALL_LOG"\n')
    if manifest is not None:
        (tmp_path / "project/.project.json").write_text(manifest)
    log = tmp_path / "called"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; tp list_issues', "_", str(scripts / "lib/ticket-provider.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log), "TICKET_PROVIDER": "plane"},
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert not log.exists()


@pytest.mark.parametrize("provider", ["plane", "trello"])
@pytest.mark.parametrize("binding", [None, {}, {"type": ""}])
def test_provisioning_allows_only_absent_or_empty_binding(tmp_path: Path, provider: str, binding: dict | None) -> None:
    scripts = tmp_path / "project/agents/hermes/pm/.scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers" / f"{provider}.sh").write_text('#!/bin/sh\nprintf "%s %s" "$TICKET_PROVIDER" "$1" > "$CALL_LOG"\n')
    manifest: dict[str, object] = {"project_name": "Fixture"}
    if binding is not None:
        manifest["ticket_provider"] = binding
    (tmp_path / "project/.project.json").write_text(json.dumps(manifest))
    log = tmp_path / "called"
    command = ["bash", "-c", 'source "$1"; tp create_board Fixture FIX desc', "_", str(scripts / "lib/ticket-provider.sh")]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log), "TICKET_PROVIDER": provider}
    ordinary = subprocess.run(command, env=env, text=True, capture_output=True)
    assert ordinary.returncode == 2 and not log.exists()
    provisioned = subprocess.run(command, env={**env, "TP_PROVISIONING": "1"}, text=True, capture_output=True)
    assert provisioned.returncode == 0, provisioned.stderr
    assert log.read_text() == f"{provider} create_board"


@pytest.mark.parametrize("provider", ["plane", "trello"])
@pytest.mark.parametrize("binding", [None, "linear", 17])
def test_provisioning_does_not_override_malformed_or_unsupported_binding(tmp_path: Path, provider: str, binding: object) -> None:
    scripts = tmp_path / "project/agents/hermes/pm/.scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers" / f"{provider}.sh").write_text('#!/bin/sh\nprintf called > "$CALL_LOG"\n')
    value = {"ticket_provider": binding} if binding != "linear" else {"ticket_provider": {"type": binding}}
    (tmp_path / "project/.project.json").write_text(json.dumps(value))
    log = tmp_path / "called"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; tp create_board Fixture FIX desc', "_", str(scripts / "lib/ticket-provider.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log), "TICKET_PROVIDER": provider, "TP_PROVISIONING": "1"},
        text=True, capture_output=True,
    )
    assert result.returncode == 2 and not log.exists(), result.stderr


@pytest.mark.parametrize("provider", ["plane", "trello"])
@pytest.mark.parametrize("op", ["resolve", "list_issues", "active_milestone", "create_issue"])
def test_canonical_empty_board_refuses_role_fallback(tmp_path: Path, provider: str, op: str) -> None:
    scripts = tmp_path / "project/agents/hermes/pm/.scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers" / f"{provider}.sh").write_text('#!/bin/sh\nprintf called > "$CALL_LOG"\n')
    (tmp_path / "project/.project.json").write_text(json.dumps({"ticket_provider": {"type": provider, "board_id": ""}}))
    (scripts.parent / "role.yaml").write_text(f"ticket_provider:\n  name: {provider}\n  board: stale-role-board\n  project: stale-role-board\n")
    log = tmp_path / "called"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; tp "$2"', "_", str(scripts / "lib/ticket-provider.sh"), op],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log)}, text=True, capture_output=True,
    )
    assert result.returncode == 2 and not log.exists(), result.stderr
    assert "ticket_provider.board_id is missing" in result.stderr


@pytest.mark.parametrize("provider", ["plane", "trello"])
def test_boardless_explicit_lookup_still_dispatches(tmp_path: Path, provider: str) -> None:
    scripts = tmp_path / "project/agents/hermes/pm/.scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    (scripts / "providers" / f"{provider}.sh").write_text('#!/bin/sh\nprintf "%s" "$1" > "$CALL_LOG"\n')
    (tmp_path / "project/.project.json").write_text(json.dumps({"ticket_provider": {"type": provider, "board_id": ""}}))
    log = tmp_path / "called"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; tp describe_board workspace explicit-board', "_", str(scripts / "lib/ticket-provider.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log)}, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "describe_board"


@pytest.mark.parametrize("provider", ["plane", "trello"])
def test_supported_dispatch_uses_bound_adapter(tmp_path: Path, provider: str) -> None:
    role = tmp_path / "project/agents/hermes/pm"
    scripts = role / ".scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "providers").mkdir()
    shutil.copy2(DISPATCHER, scripts / "lib/ticket-provider.sh")
    adapter = scripts / "providers" / f"{provider}.sh"
    adapter.write_text('#!/bin/sh\nprintf "%s" "$TICKET_PROVIDER" > "$CALL_LOG"\n')
    (tmp_path / "project/.project.json").write_text(json.dumps({"ticket_provider": {"type": provider, "board_id": "fixture"}}))
    log = tmp_path / "called"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; tp list_issues', "_", str(scripts / "lib/ticket-provider.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log)},
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == provider


@pytest.mark.parametrize("render_provider,bound_provider", [("plane", "trello"), ("trello", "plane")])
def test_disposable_render_reads_manifest_before_provider_guidance(tmp_path: Path, render_provider: str, bound_provider: str) -> None:
    source = tmp_path / "template-source"
    source.mkdir()
    shutil.copy2(ROOT / "copier.yml", source / "copier.yml")
    shutil.copytree(ROOT / "template", source / "template", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    project = tmp_path / "project"
    role = project / "agents/hermes/pm"
    rendered = subprocess.run(
        ["copier", "copy", "--skip-tasks", "--defaults", "--trust", "--data", "target_repo=disposable", "--data", f"ticket_provider={render_provider}", str(source), str(role)],
        text=True, capture_output=True,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    prompt = (role / ".scripts/sentinel.prompt.md").read_text()
    assert "not authority for dispatch" in prompt
    assert "`.project.json` `ticket_provider.type` first" in prompt
    assert "**Plane-only**" in prompt
    assert "Stop after that\nTrello report" in prompt
    docs = (role / ".scripts/sentinel/docs/continuous-ticket-orchestration.md").read_text()
    assert "only an explicit `plane` binding activates" in docs
    assert "does not authorize Krebs labels" in docs
    assert "a missing/empty board ID is not supplied by `role.yaml`" in docs
    if render_provider == "plane":
        assert "**Krebs lifecycle" in prompt
        assert "## Pass" in prompt
    else:
        assert "**Krebs lifecycle" not in prompt
        assert "## Pass" not in prompt
        assert "request a Plane-configured render" in prompt
    assert "works identically" not in prompt
    assert not (role / ".scripts/providers/linear.sh").exists()
    assert prompt.index("## Provider gate") < prompt.index("This Trello render" if render_provider == "trello" else "**Krebs lifecycle")
    (project / ".project.json").write_text(json.dumps({"ticket_provider": {"type": bound_provider, "board_id": "fixture"}}))
    for provider in ("plane", "trello"):
        (role / f".scripts/providers/{provider}.sh").write_text('#!/bin/sh\nprintf "%s" "$TICKET_PROVIDER" > "$CALL_LOG"\n')
    log = tmp_path / "called"
    called = subprocess.run(
        ["bash", "-c", 'source "$1"; tp list_issues', "_", str(role / ".scripts/lib/ticket-provider.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CALL_LOG": str(log)}, text=True, capture_output=True,
    )
    assert called.returncode == 0, called.stderr
    assert log.read_text() == bound_provider
