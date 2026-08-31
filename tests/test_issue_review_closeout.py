from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template"
AUTONOMOUS_REVIEW = (
    TEMPLATE / ".scripts" / "sentinel" / "bin" / "issue-autonomous-review.sh"
)
CLOSE_GATE = TEMPLATE / ".scripts" / "sentinel" / "bin" / "issue-close-gate.sh"
ISSUE = "JIMB-207"

EVIDENCE = f"""# {ISSUE} closeout fixture

## Issue
Hermetic closeout behavior.

## Acceptance Criteria
- The adapter controls completion.

## Repo Changes
- Worker: fixture-implementer

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
}
"""


def stage_role(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    (role / "role.yaml").write_text(
        """repo: closeout-fixture
reconcile:
  auto_review: true
  grace_hours: 0
ticket_provider:
  name: fixture
""",
        encoding="utf-8",
    )

    evidence_dir = (
        tmp_path
        / "_bmad-output"
        / "implementation-artifacts"
        / "issue-evidence"
    )
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{ISSUE}.md").write_text(EVIDENCE, encoding="utf-8")
    report = tmp_path / "review.md"
    report.write_text(REPORT, encoding="utf-8")
    calls = tmp_path / "provider-calls.tsv"
    return bin_dir / AUTONOMOUS_REVIEW.name, report, calls


def run_review(
    tmp_path: Path, *args: str, transition_result: str = "pass"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    script, report, calls = stage_role(tmp_path)
    env = dict(os.environ)
    env.update(
        {
            "TP_CALLS": str(calls),
            "TP_TRANSITION_RESULT": transition_result,
        }
    )
    proc = subprocess.run(
        ["bash", str(script), ISSUE, str(report), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    observed = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, observed


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
        ["sh", str(gate), ISSUE, str(tmp_path)],
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
        ["sh", str(gate), ISSUE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert failing.returncode == 1
    assert f"CLOSE GATE: FAIL for {ISSUE}" in failing.stderr
    assert "CLOSE GATE: PASS" not in failing.stdout + failing.stderr
