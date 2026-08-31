from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template"
AUTONOMOUS_REVIEW = (
    TEMPLATE / ".scripts" / "sentinel" / "bin" / "issue-autonomous-review.sh"
)
CLOSE_GATE = TEMPLATE / ".scripts" / "sentinel" / "bin" / "issue-close-gate.sh"
ISSUE = "JIMB-207"

EVIDENCE = f"""# {ISSUE} closeout fixture

## Issue
- Worker: fixture-implementer
Hermetic closeout behavior.

## Acceptance Criteria
- The adapter controls completion.

## Repo Changes
- Adapter and gate changes.

## Verification
The focused closeout suite ran.

## Ledger Update
Ledger updated: yes

## Known Gaps
None.

## Close Recommendation
Close recommendation: ready
"""

REPORT = """# Independent review fixture

## Reviewer
- Reviewer agent: fixture-reviewer
- Independent of implementer: yes

## Locked Intent Baseline
- Completion must be truthful.

## Drift Assessment
- Drift assessment: none

## Adversarial Findings
- Critical/high findings: none

## Decision
- Decision: accept
"""

PROVIDER_SPY = r"""#!/usr/bin/env bash
tp() {
  action="$1"
  shift
  if [[ "${TICKET_PROVIDER:-}" != "${TP_EXPECTED_PROVIDER:-fixture}" ]]; then
    printf 'wrong provider selection: %s\n' "${TICKET_PROVIDER:-<unset>}" >&2
    return 23
  fi
  {
    printf '%s' "$action"
    for value in "$@"; do printf '\t%s' "$value"; done
    printf '\n'
  } >>"$TP_CALLS"

  if [[ "$action" == "transition" && "${TP_TRANSITION_RESULT:-pass}" == "fail" ]]; then
    # These deliberately contradictory words prove the caller suppresses
    # adapter noise instead of leaking a false completion claim.
    printf 'adapter says completed before failing\n'
    printf 'adapter transition failed noisily\n' >&2
    return 19
  fi
  if [[ "$action" == "comment" && "${TP_COMMENT_RESULT:-pass}" == "fail" ]]; then
    # This acceptance-shaped adapter noise must never leak from a failed write.
    printf 'AUTONOMOUS REVIEW: ACCEPTED by noisy adapter\n'
    printf 'adapter comment failed noisily\n' >&2
    return 29
  fi
  if [[ "$action" == "comment" ]]; then
    [[ "${TP_COMMENT_RESULT:-pass}" == "empty" ]] || printf 'fixture-comment-id\n'
  fi
}
"""

DEFAULT_ROLE_YAML = """repo: closeout-fixture
name: deliberately-wrong-top-level-provider
reconcile:
  auto_review: true
  grace_hours: 0
ticket_provider:
  name: fixture
"""


def stage_role(
    tmp_path: Path,
    *,
    role_yaml: str = DEFAULT_ROLE_YAML,
    report: str = REPORT,
    evidence: str = EVIDENCE,
) -> tuple[Path, Path, Path]:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    role = tmp_path / "agents" / "hermes" / "pm"
    bin_dir = role / ".scripts" / "sentinel" / "bin"
    lib_dir = role / ".scripts" / "lib"
    bin_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    shutil.copy2(AUTONOMOUS_REVIEW, bin_dir / AUTONOMOUS_REVIEW.name)
    shutil.copy2(CLOSE_GATE, bin_dir / CLOSE_GATE.name)
    (lib_dir / "ticket-provider.sh").write_text(PROVIDER_SPY, encoding="utf-8")
    (tmp_path / ".project.json").write_text(
        json.dumps({"project_slug": "closeout-fixture"}),
        encoding="utf-8",
    )
    (role / "role.yaml").write_text(role_yaml, encoding="utf-8")

    evidence_dir = (
        tmp_path
        / "_bmad-output"
        / "implementation-artifacts"
        / "issue-evidence"
    )
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{ISSUE}.md").write_text(evidence, encoding="utf-8")
    report_path = tmp_path / "review.md"
    report_path.write_text(report, encoding="utf-8")
    calls = tmp_path / "provider-calls.tsv"
    return bin_dir / AUTONOMOUS_REVIEW.name, report_path, calls


def run_review(
    tmp_path: Path,
    *args: str,
    transition_result: str = "pass",
    comment_result: str = "pass",
    role_yaml: str = DEFAULT_ROLE_YAML,
    report: str = REPORT,
    evidence: str = EVIDENCE,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    script, report_path, calls = stage_role(
        tmp_path, role_yaml=role_yaml, report=report, evidence=evidence
    )
    env = dict(os.environ)
    env.update(
        {
            "TP_CALLS": str(calls),
            "TP_TRANSITION_RESULT": transition_result,
            "TP_COMMENT_RESULT": comment_result,
            "TP_EXPECTED_PROVIDER": "fixture",
        }
    )
    env.pop("RECONCILE_AUTO_REVIEW", None)
    env.pop("RECONCILE_GRACE_HOURS", None)
    env.update(env_overrides or {})
    proc = subprocess.run(
        [str(script), ISSUE, str(report_path), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    observed = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, observed


def run_gate(tmp_path: Path, evidence: str = EVIDENCE) -> subprocess.CompletedProcess[str]:
    _, _, _ = stage_role(tmp_path, evidence=evidence)
    gate = (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )
    return subprocess.run(
        [str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def test_shipped_sentinel_entrypoints_are_executable() -> None:
    for entrypoint in (AUTONOMOUS_REVIEW, CLOSE_GATE):
        assert entrypoint.stat().st_mode & 0o111 == 0o111, (
            f"{entrypoint.relative_to(ROOT)} must ship executable"
        )
        assert os.access(entrypoint, os.X_OK)


def test_close_failure_emits_no_acceptance_or_completion_claim(tmp_path: Path) -> None:
    proc, calls = run_review(tmp_path, "--close", transition_result="fail")

    assert proc.returncode == 1
    assert calls == [f"transition\t{ISSUE}\tcompleted"]
    combined = proc.stdout + proc.stderr
    assert combined.strip() == (
        f"AUTONOMOUS REVIEW: CLOSE FAILED for {ISSUE} - "
        "adapter transition failed; issue left open."
    )
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined
    assert "treat as done" not in combined
    assert "transitioned to completed" not in combined
    assert "adapter says completed" not in combined


def test_close_success_transitions_then_reports_and_comments_once(tmp_path: Path) -> None:
    proc, calls = run_review(tmp_path, "--close")

    assert proc.returncode == 0, proc.stderr
    assert calls[0] == f"transition\t{ISSUE}\tcompleted"
    assert calls[1].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    assert len(calls) == 2
    assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1
    assert proc.stdout.count(f"Ticket {ISSUE} transitioned to completed via adapter.") == 1
    assert "CLOSE FAILED" not in proc.stdout + proc.stderr


def test_close_comment_failure_reports_transition_succeeded_without_acceptance(
    tmp_path: Path,
) -> None:
    proc, calls = run_review(tmp_path, "--close", comment_result="fail")

    assert proc.returncode == 1
    assert calls[0] == f"transition\t{ISSUE}\tcompleted"
    assert calls[1].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    assert len(calls) == 2
    combined = proc.stdout + proc.stderr
    assert combined.strip() == (
        f"AUTONOMOUS REVIEW: CLOSE INCOMPLETE for {ISSUE} - transition succeeded, "
        "but acceptance comment failed; issue may already be completed."
    )
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined
    assert "treat as done" not in combined
    assert "transitioned to completed" not in combined
    assert "noisy adapter" not in combined


def test_close_rc_zero_empty_comment_is_unproven_without_acceptance(
    tmp_path: Path,
) -> None:
    proc, calls = run_review(tmp_path, "--close", comment_result="empty")

    assert proc.returncode == 1
    assert calls[0] == f"transition\t{ISSUE}\tcompleted"
    assert calls[1].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    assert len(calls) == 2, "an ambiguous comment write must never be retried"
    combined = proc.stdout + proc.stderr
    assert combined.strip() == (
        f"AUTONOMOUS REVIEW: CLOSE INCOMPLETE for {ISSUE} - transition succeeded, "
        "but acceptance comment returned no id; comment write unproven and issue "
        "may already be completed."
    )
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined
    assert "treat as done" not in combined
    assert "transitioned to completed" not in combined


def test_default_acceptance_stays_in_review_without_completion_transition(
    tmp_path: Path,
) -> None:
    proc, calls = run_review(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert calls[0].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    assert not any(call.startswith("transition\t") for call in calls)
    assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1
    assert "stays in the review lane" in proc.stdout
    assert "transitioned to completed" not in proc.stdout


def test_default_comment_failure_emits_no_acceptance(tmp_path: Path) -> None:
    proc, calls = run_review(tmp_path, comment_result="fail")

    assert proc.returncode == 1
    assert len(calls) == 1
    assert calls[0].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    combined = proc.stdout + proc.stderr
    assert combined.strip() == (
        f"AUTONOMOUS REVIEW: COMMENT FAILED for {ISSUE} - acceptance comment "
        "was not recorded; issue left in review."
    )
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined
    assert "treat as done" not in combined
    assert "noisy adapter" not in combined


def test_default_rc_zero_empty_comment_is_unproven_without_acceptance(
    tmp_path: Path,
) -> None:
    proc, calls = run_review(tmp_path, comment_result="empty")

    assert proc.returncode == 1
    assert len(calls) == 1, "an ambiguous comment write must never be retried"
    assert calls[0].startswith(f"comment\t{ISSUE}\tAutonomously accepted by fixture-reviewer")
    combined = proc.stdout + proc.stderr
    assert combined.strip() == (
        f"AUTONOMOUS REVIEW: COMMENT UNPROVEN for {ISSUE} - acceptance comment "
        "returned no id; issue left in review."
    )
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined
    assert "treat as done" not in combined


def test_reconcile_false_is_not_bypassed_by_earlier_unrelated_true(
    tmp_path: Path,
) -> None:
    role_yaml = """repo: closeout-fixture
other:
  auto_review: true
  grace_hours: 99
reconcile:
  auto_review: false # authoritative autonomous-review switch
  grace_hours: 7 # informational wait window
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    assert proc.returncode == 3
    assert proc.stderr.strip() == (
        "Autonomous review is disabled (reconcile.auto_review=false)."
    )
    assert calls == []
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_reconcile_true_and_grace_ignore_earlier_unrelated_false_values(
    tmp_path: Path,
) -> None:
    role_yaml = """repo: closeout-fixture
other:
  auto_review: false
  grace_hours: 99
reconcile:
  auto_review: true # authoritative autonomous-review switch
  grace_hours: 7 # informational wait window
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert "grace 7h informational" in calls[0]
    assert "grace 99h" not in calls[0]
    assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("true", True), ("on", True), ("false", False), ("off", False)],
)
def test_role_auto_review_accepts_only_explicit_boolean_tokens(
    tmp_path: Path, value: str, enabled: bool
) -> None:
    role_yaml = f"""repo: closeout-fixture
reconcile:
  auto_review: {value} # authoritative autonomous-review switch
  grace_hours: 0 # informational wait window
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    combined = proc.stdout + proc.stderr
    if enabled:
        assert proc.returncode == 0, proc.stderr
        assert len(calls) == 1
        assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1
    else:
        assert proc.returncode == 3
        assert calls == []
        assert "Autonomous review is disabled" in proc.stderr
        assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined


@pytest.mark.parametrize("value", ["fasle", "no", "0"])
def test_invalid_inline_commented_role_auto_review_fails_before_provider_call(
    tmp_path: Path, value: str
) -> None:
    role_yaml = f"""repo: closeout-fixture
reconcile:
  auto_review: {value} # typo or unsupported YAML-like value
  grace_hours: 0 # informational wait window
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    assert proc.returncode == 3
    assert calls == []
    assert "CONFIG INVALID" in proc.stderr
    assert "reconcile.auto_review must be true|on|false|off" in proc.stderr
    assert f"got '{value}'" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(" TRUE ", True), ("On", True), (" FALSE ", False), ("Off", False)],
)
def test_environment_auto_review_tokens_are_trimmed_and_normalized(
    tmp_path: Path, value: str, enabled: bool
) -> None:
    proc, calls = run_review(
        tmp_path, env_overrides={"RECONCILE_AUTO_REVIEW": value}
    )

    combined = proc.stdout + proc.stderr
    if enabled:
        assert proc.returncode == 0, proc.stderr
        assert len(calls) == 1
        assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1
    else:
        assert proc.returncode == 3
        assert calls == []
        assert "Autonomous review is disabled" in proc.stderr
        assert "AUTONOMOUS REVIEW: ACCEPTED" not in combined


@pytest.mark.parametrize("value", ["fasle", "no", "0"])
def test_invalid_environment_auto_review_fails_before_provider_call(
    tmp_path: Path, value: str
) -> None:
    proc, calls = run_review(
        tmp_path, env_overrides={"RECONCILE_AUTO_REVIEW": value}
    )

    assert proc.returncode == 3
    assert calls == []
    assert "CONFIG INVALID" in proc.stderr
    assert f"got '{value}'" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("value", ["-1", "many"])
def test_invalid_inline_commented_role_grace_fails_before_provider_call(
    tmp_path: Path, value: str
) -> None:
    role_yaml = f"""repo: closeout-fixture
reconcile:
  auto_review: true # authoritative autonomous-review switch
  grace_hours: {value} # invalid informational wait window
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    assert proc.returncode == 3
    assert calls == []
    assert "CONFIG INVALID" in proc.stderr
    assert "reconcile.grace_hours must be a nonnegative integer" in proc.stderr
    assert f"got '{value}'" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("value", ["-1", "many"])
def test_invalid_environment_grace_fails_before_provider_call(
    tmp_path: Path, value: str
) -> None:
    proc, calls = run_review(
        tmp_path, env_overrides={"RECONCILE_GRACE_HOURS": value}
    )

    assert proc.returncode == 3
    assert calls == []
    assert "CONFIG INVALID" in proc.stderr
    assert f"got '{value}'" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_missing_reconcile_values_retain_enabled_zero_grace_defaults(
    tmp_path: Path,
) -> None:
    role_yaml = """repo: closeout-fixture
reconcile:
  # auto_review and grace_hours intentionally omitted
ticket_provider:
  name: fixture
"""

    proc, calls = run_review(tmp_path, role_yaml=role_yaml)

    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert "grace 0h informational" in calls[0]
    assert proc.stdout.count("AUTONOMOUS REVIEW: ACCEPTED") == 1


def test_close_gate_success_names_repo_and_failure_remains_fail_closed(
    tmp_path: Path,
) -> None:
    _, _, _ = stage_role(tmp_path)
    gate = (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )
    passing = subprocess.run(
        [str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert passing.returncode == 0, passing.stderr
    assert passing.stdout.strip() == (
        f"CLOSE GATE: PASS for {ISSUE} (repo: closeout-fixture)"
    )

    evidence = (
        tmp_path
        / "_bmad-output"
        / "implementation-artifacts"
        / "issue-evidence"
        / f"{ISSUE}.md"
    )
    evidence.write_text(
        EVIDENCE.replace(
            "Close recommendation: ready", "Close recommendation: hold"
        ),
        encoding="utf-8",
    )
    failing = subprocess.run(
        [str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert failing.returncode == 1
    assert f"CLOSE GATE: FAIL for {ISSUE}" in failing.stderr
    assert "CLOSE GATE: PASS" not in failing.stdout + failing.stderr


def test_close_gate_fails_when_installed_role_repo_disagrees_with_manifest(
    tmp_path: Path,
) -> None:
    _, _, _ = stage_role(tmp_path)
    gate = (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )
    role_yaml = tmp_path / "agents" / "hermes" / "pm" / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            "repo: closeout-fixture", "repo: different-project"
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 1
    assert "disagrees with target project slug closeout-fixture" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_manifest_never_falls_back_when_slug_is_invalid(
    tmp_path: Path,
) -> None:
    _, _, _ = stage_role(tmp_path)
    gate = (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )
    (tmp_path / ".project.json").write_text(
        json.dumps({"ticket_provider": {"type": "fixture"}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 1
    assert "has no non-blank project_slug" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_uses_basename_only_when_manifest_is_absent(tmp_path: Path) -> None:
    _, _, _ = stage_role(tmp_path)
    gate = (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )
    role_yaml = tmp_path / "agents" / "hermes" / "pm" / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            "repo: closeout-fixture", f"repo: {tmp_path.name}"
        ),
        encoding="utf-8",
    )
    (tmp_path / ".project.json").unlink()

    result = subprocess.run(
        ["sh", str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"CLOSE GATE: PASS for {ISSUE} (repo: {tmp_path.name})"
    )


def stage_relative_gate_target(
    tmp_path: Path, *, manifest: str, role_repo: str
) -> Path:
    _, _, _ = stage_role(tmp_path)
    role_yaml = tmp_path / "agents" / "hermes" / "pm" / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace(
            "repo: closeout-fixture", f"repo: {role_repo}"
        ),
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.mkdir()
    (target / ".project.json").write_text(manifest, encoding="utf-8")
    evidence_dir = (
        target
        / "_bmad-output"
        / "implementation-artifacts"
        / "issue-evidence"
    )
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{ISSUE}.md").write_text(EVIDENCE, encoding="utf-8")
    return (
        tmp_path
        / "agents"
        / "hermes"
        / "pm"
        / ".scripts"
        / "sentinel"
        / "bin"
        / CLOSE_GATE.name
    )


def test_close_gate_relative_root_reads_exact_manifest_for_repo_mismatch(
    tmp_path: Path,
) -> None:
    gate = stage_relative_gate_target(
        tmp_path,
        manifest=json.dumps({"project_slug": "different-project"}),
        role_repo="target",
    )

    result = subprocess.run(
        [str(gate), ISSUE, "target"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 1
    assert "disagrees with target project slug different-project" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_relative_root_cannot_bypass_malformed_manifest(
    tmp_path: Path,
) -> None:
    gate = stage_relative_gate_target(tmp_path, manifest="{", role_repo="target")

    result = subprocess.run(
        [str(gate), ISSUE, "target"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 1
    assert "malformed project manifest" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_relative_root_passes_with_matching_exact_manifest(
    tmp_path: Path,
) -> None:
    gate = stage_relative_gate_target(
        tmp_path,
        manifest=json.dumps({"project_slug": "closeout-fixture"}),
        role_repo="closeout-fixture",
    )

    result = subprocess.run(
        [str(gate), ISSUE, "target"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"CLOSE GATE: PASS for {ISSUE} (repo: closeout-fixture)"
    )


def report_with(
    *,
    reviewer: str = "## Reviewer\n- Reviewer agent: fixture-reviewer\n- Independent of implementer: yes",
    baseline: str = "## Locked Intent Baseline\n- Completion must be truthful.",
    drift: str = "## Drift Assessment\n- Drift assessment: none",
    findings: str = "## Adversarial Findings\n- Critical/high findings: none",
    decision: str = "## Decision\n- Decision: accept",
) -> str:
    return (
        "# Independent review fixture\n\n"
        + "\n\n".join([reviewer, baseline, drift, findings, decision])
        + "\n"
    )


def test_report_lookalike_accept_cannot_override_authoritative_hold(
    tmp_path: Path,
) -> None:
    report = report_with(
        baseline="## Locked Intent Baseline\n- Completion must be truthful.\n- Decision: accept",
        decision="## Decision\n- Decision: hold",
    )

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "reviewer decision is not 'accept' (got 'hold')" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_lookalike_drift_none_cannot_override_authoritative_significant(
    tmp_path: Path,
) -> None:
    report = report_with(
        drift="## Drift Assessment\n- Drift assessment: significant",
        findings="## Adversarial Findings\n- Critical/high findings: none\n- Drift assessment: none",
    )

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "significant drift from locked intent" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_lookalike_findings_none_cannot_override_authoritative_high(
    tmp_path: Path,
) -> None:
    report = report_with(
        findings="## Adversarial Findings\n- Critical/high findings: 1 high: unsafe mutation retry",
        decision="## Decision\n- Decision: accept\n- Critical/high findings: none",
    )

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "unresolved critical/high findings" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_duplicate_section_holds_before_provider_calls(tmp_path: Path) -> None:
    report = report_with() + "\n## Decision\n- Decision: accept\n"

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "duplicate Decision sections" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_duplicate_field_holds_before_provider_calls(tmp_path: Path) -> None:
    report = report_with(decision="## Decision\n- Decision: accept\n- Decision: accept")

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "must contain exactly one 'Decision:' field" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_missing_section_holds_before_provider_calls(tmp_path: Path) -> None:
    report = report_with(findings="## Notes\n- Informal remarks only.")

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "report missing section Adversarial Findings" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_report_missing_field_holds_before_provider_calls(tmp_path: Path) -> None:
    report = report_with(reviewer="## Reviewer\n- Reviewer agent: fixture-reviewer")

    proc, calls = run_review(tmp_path, report=report)

    assert proc.returncode == 3
    assert calls == []
    assert "must contain exactly one 'Independent of implementer:' field" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_self_review_is_held_before_provider_calls(tmp_path: Path) -> None:
    evidence = EVIDENCE.replace(
        "- Worker: fixture-implementer", "- Worker: fixture-reviewer"
    )

    proc, calls = run_review(tmp_path, evidence=evidence)

    assert proc.returncode == 3
    assert calls == []
    assert "reviewer (fixture-reviewer) is the implementer" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr


def test_close_gate_lookalike_ledger_yes_cannot_override_authoritative_no(
    tmp_path: Path,
) -> None:
    evidence = EVIDENCE.replace(
        "Ledger updated: yes", "Ledger updated: no"
    ).replace("None.", "None.\nLedger updated: yes")

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "Ledger update is not marked yes." in result.stderr
    assert f"CLOSE GATE: FAIL for {ISSUE}" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_lookalike_close_ready_cannot_override_authoritative_hold(
    tmp_path: Path,
) -> None:
    evidence = EVIDENCE.replace(
        "Close recommendation: ready", "Close recommendation: hold"
    ).replace("None.", "None.\nClose recommendation: ready")

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "Close recommendation is not ready." in result.stderr
    assert f"CLOSE GATE: FAIL for {ISSUE}" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_duplicate_implementer_fails(tmp_path: Path) -> None:
    evidence = EVIDENCE.replace(
        "- Worker: fixture-implementer",
        "- Worker: fixture-implementer\n- Worker: second-worker",
    )

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "exactly one implementer identity" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_conflicting_implementer_spellings_fail(tmp_path: Path) -> None:
    evidence = EVIDENCE.replace(
        "- Worker: fixture-implementer",
        "- Worker: fixture-implementer\n- Implemented by: someone-else",
    )

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "exactly one implementer identity" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_missing_implementer_fails(tmp_path: Path) -> None:
    evidence = EVIDENCE.replace("- Worker: fixture-implementer\n", "")

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "exactly one implementer identity" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_close_gate_duplicate_section_fails(tmp_path: Path) -> None:
    evidence = EVIDENCE + "\n## Ledger Update\nLedger updated: yes\n"

    result = run_gate(tmp_path, evidence=evidence)

    assert result.returncode == 1
    assert "Duplicate required evidence section: Ledger Update" in result.stderr
    assert "CLOSE GATE: PASS" not in result.stdout + result.stderr


def test_review_holds_when_authoritative_ledger_is_no_despite_lookalike(
    tmp_path: Path,
) -> None:
    evidence = EVIDENCE.replace(
        "Ledger updated: yes", "Ledger updated: no"
    ).replace("None.", "None.\nLedger updated: yes")

    proc, calls = run_review(tmp_path, evidence=evidence)

    assert proc.returncode == 3
    assert calls == []
    assert "close gate failed" in proc.stderr
    assert "AUTONOMOUS REVIEW: ACCEPTED" not in proc.stdout + proc.stderr
