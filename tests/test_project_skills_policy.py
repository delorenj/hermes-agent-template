"""FLUME-42: canonical policy callers, identity, and unsupported-runtime refusal.

All mutation tests use disposable fleets. No installed profile or skill is edited.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest
import yaml
from project_skills_runtime_fixture import supported_case

ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / "scripts/hermes-profile-config.py"


def run(case, *args, **extra):
    return subprocess.run(
        [sys.executable, "-I", str(WRITER), *args],
        env={**case["env"], **extra}, capture_output=True, text=True, timeout=8,
    )


def pair(profile):
    return tuple(
        (p.read_bytes(), p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns)
        for p in (profile / "config.delta.yaml", profile / "config.yaml")
    )


@pytest.fixture
def case_root():
    with tempfile.TemporaryDirectory(prefix="hermes-project-policy-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def case(case_root):
    tmp_path = case_root
    home = tmp_path / "home"
    fleet = home / ".hermes"
    profile = fleet / "profiles/demo-pm"
    profile.mkdir(parents=True)
    base = {
        "skills": {"external_dirs": ["legacy"], "trusted_project_dirs": ["/existing/base"]},
        "operator": {"fleet": "keep"},
    }
    delta = {
        "skills": {"trusted_project_dirs": ["/existing/profile"], "disabled": ["keep-disabled"]},
        "operator": {"profile": "keep"},
    }
    (fleet / "config.yaml").write_text(yaml.safe_dump(base))
    (profile / "config.delta.yaml").write_text("# keep operator header\n" + yaml.safe_dump(delta))
    (profile / "config.yaml").write_text(yaml.safe_dump({
        "skills": {**base["skills"], **delta["skills"]},
        "operator": {**base["operator"], **delta["operator"]},
    }))
    project = home / "project without git"
    role = project / "agents/hermes/pm"
    role.mkdir(parents=True)
    (project / ".project.json").write_text(json.dumps({
        "repo_path": str(project),
        "agents": {"demo-pm": {"role": "pm", "role_dir": "agents/hermes/pm"}},
    }))
    (role / "role.yaml").write_text("agent_id: demo-pm\nprofile: demo-pm\nrole: pm\n")
    (role / "SOUL.md").write_text("Disposable PM identity.\n")
    (profile / "SOUL.md").symlink_to(role / "SOUL.md")
    registry = fleet / "agents-registry.yaml"
    registry.write_text(yaml.safe_dump({"agents": {"demo-pm": {
        "role": "pm", "profile_name": "demo-pm", "project_path": str(project),
        "role_dir": str(role), "bloodbank": {"enabled": False}, "correlation": None,
    }}}))
    runtime = tmp_path / "pinned-runtime"
    (runtime / "tools").mkdir(parents=True)
    (runtime / "agent").mkdir()
    (runtime / "hermes_cli").mkdir()
    (runtime / "tools/skills_tool.py").write_text("def _find_all_skills():\n    return []\n")
    (runtime / "agent/skill_utils.py").write_text("def get_all_skills_dirs():\n    return []\n")
    (runtime / "hermes_cli/config_defaults.py").write_text("DEFAULT_CONFIG = {'skills': {'external_dirs': []}}\n")
    binary = runtime / ".venv/bin/hermes"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    (binary.parent / "python").symlink_to(sys.executable)
    env = {
        "HOME": str(home), "PATH": os.environ["PATH"], "HERMES_FLEET_HOME": str(fleet),
        "HERMES_FLEET_REPO": str(runtime), "HERMES_FLEET_BIN": str(binary),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", "fixture"),
    }
    return dict(home=home, fleet=fleet, profile=profile, project=project, role=role,
                registry=registry, runtime=runtime, env=env)


def test_canonical_policy_writer_preserves_config_trust_and_noop(case):
    result = run(case, "skills-policy", "--profile", "demo-pm")
    assert result.returncode == 0, result.stdout + result.stderr
    delta = yaml.safe_load((case["profile"] / "config.delta.yaml").read_text())
    assert delta["skills"] == {"external_dirs": [], "trusted_project_dirs": ["/existing/profile"], "disabled": ["keep-disabled"]}
    current = yaml.safe_load((case["profile"] / "config.yaml").read_text())
    assert current["operator"] == {"fleet": "keep", "profile": "keep"}
    assert current["skills"]["trusted_project_dirs"] == ["/existing/profile"]
    assert (case["profile"] / "config.delta.yaml").read_text().startswith("# keep operator header\n")
    assert (case["profile"] / "config.yaml").read_text().startswith("# ---")
    before = pair(case["profile"])
    assert run(case, "skills-policy", "--profile", "demo-pm").returncode == 0
    assert pair(case["profile"]) == before
    assert run(case, "check", "--profile", "demo-pm").returncode == 0


def test_policy_dry_run_and_drift_gate_leave_exact_pair(case):
    before = pair(case["profile"])
    assert run(case, "skills-policy", "--profile", "demo-pm", "--dry-run").returncode == 0
    assert pair(case["profile"]) == before
    current_path = case["profile"] / "config.yaml"
    current = yaml.safe_load(current_path.read_text())
    current["skills"]["external_dirs"] = []
    current["operator"]["out_of_band"] = "must absorb first"
    current_path.write_text(yaml.safe_dump(current))
    before = pair(case["profile"])
    refused = run(case, "skills-policy", "--profile", "demo-pm")
    assert refused.returncode != 0
    assert "drift" in refused.stderr
    assert pair(case["profile"]) == before


@pytest.mark.parametrize("target", ["profile", "config.delta.yaml", "config.yaml", "lock"])
def test_policy_refuses_symlinks_before_any_config_write(case, target):
    profile = case["profile"]
    path = profile if target == "profile" else profile.parent / ".demo-pm.config.lock" if target == "lock" else profile / target
    moved = path.with_name(path.name + ".fixture-original")
    if path.exists():
        path.rename(moved)
    else:
        moved.write_text("keep me")
    path.symlink_to(moved)
    before = pair(profile)
    result = run(case, "skills-policy", "--profile", "demo-pm")
    assert result.returncode != 0
    assert "symlink" in result.stderr or "real directory" in result.stderr
    assert pair(profile) == before


def test_top_level_timeout_then_retry_reads_current_delta(case):
    profile = case["profile"]
    lock = profile.parent / ".demo-pm.config.lock"
    with lock.open("a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        before = pair(profile)
        result = run(case, "skills-policy", "--profile", "demo-pm", HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS="0.08")
        assert result.returncode != 0
        assert "timed out waiting for profile config lock" in result.stderr
        assert pair(profile) == before
        # A different canonical writer wins while this writer is waiting/failed.
        delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
        delta["operator"]["profile"] = "new winning value"
        delta["skills"]["trusted_project_dirs"].append("/concurrent/root")
        (profile / "config.delta.yaml").write_text(yaml.safe_dump(delta))
        base = yaml.safe_load((case["fleet"] / "config.yaml").read_text())
        (profile / "config.yaml").write_text(yaml.safe_dump({
            "skills": {**base["skills"], **delta["skills"]},
            "operator": {**base["operator"], **delta["operator"]},
        }))
    retry = run(case, "skills-policy", "--profile", "demo-pm")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    current = yaml.safe_load((profile / "config.yaml").read_text())
    assert current["operator"]["profile"] == "new winning value"
    assert current["skills"]["trusted_project_dirs"] == ["/existing/profile", "/concurrent/root"]


def test_waiting_top_level_writer_does_not_snapshot_before_lock(case, tmp_path):
    profile = case["profile"]
    lock = profile.parent / ".demo-pm.config.lock"
    attempt = tmp_path / "attempt"
    with lock.open("a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-I", str(WRITER), "skills-policy", "--profile", "demo-pm"],
            env={**case["env"], "PJANGLER_TEST_PROFILE_CONFIG_LOCK_ATTEMPT": str(attempt)},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 4
            while not attempt.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert attempt.read_text().strip() == str(lock)
            assert process.poll() is None
            delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
            delta["operator"]["profile"] = "value changed after attempt"
            (profile / "config.delta.yaml").write_text(yaml.safe_dump(delta))
            current = yaml.safe_load((profile / "config.yaml").read_text())
            current["operator"]["profile"] = delta["operator"]["profile"]
            (profile / "config.yaml").write_text(yaml.safe_dump(current))
        except BaseException:
            process.kill()
            process.communicate()
            raise
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stdout + stderr
    assert yaml.safe_load((profile / "config.yaml").read_text())["operator"]["profile"] == "value changed after attempt"


@pytest.mark.parametrize("timeout", ["inf", "-inf", "nan"])
def test_nonfinite_lock_timeout_is_refused(case, timeout):
    before = pair(case["profile"])
    result = run(case, "skills-policy", "--profile", "demo-pm", HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS=timeout)
    assert result.returncode != 0
    assert "finite" in result.stderr
    assert pair(case["profile"]) == before


def test_project_discovery_blocks_unsupported_pin_without_mutation(case):
    before = pair(case["profile"])
    result = run(case, "project-skills", "--registered-pms", "--json", "--dry-run")
    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads(result.stdout)
    row = report["profiles"][0]
    assert row["profile"] == "demo-pm"  # inactive + correlation=null is still included
    assert row["project_root"] == str(case["project"])
    assert row["skills_directory_status"] == "missing"
    assert "runtime_project_discovery_unsupported" in [b["code"] for b in row["blockers"]]
    assert report["runtime"]["repo"] == str(case["runtime"])
    assert report["runtime"]["features"]["project_discovery"] is False
    assert pair(case["profile"]) == before
    assert not (case["project"] / ".agents/skills").exists()


def test_binding_disagreement_blocks_only_affected_row(case):
    manifest = case["project"] / ".project.json"
    manifest.write_text(json.dumps({"repo_path": str(case["home"])}))
    result = run(case, "project-skills", "--registered-pms", "--json")
    assert result.returncode == 3, result.stdout + result.stderr
    blockers = json.loads(result.stdout)["profiles"][0]["blockers"]
    assert "binding_manifest_mismatch" in [b["code"] for b in blockers]


def test_explicit_non_git_binding_never_trusts_an_ancestor(case):
    result = run(case, "project-skills", "--profile", "demo-pm", "--project-root", str(case["project"]), "--role-dir", str(case["role"]), "--json")
    assert result.returncode == 3, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["project_root"] == str(case["project"])
    assert not any(b["code"].startswith("binding_") for b in row["blockers"])


def test_project_blocker_uses_same_lock_and_timeout_preserves_pair(case):
    profile = case["profile"]
    with (profile.parent / ".demo-pm.config.lock").open("a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        before = pair(profile)
        result = run(case, "project-skills", "--registered-pms", "--json", HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS="0.06")
        assert result.returncode == 3
        assert "profile_lock_failed" in [b["code"] for b in json.loads(result.stdout)["profiles"][0]["blockers"]]
        assert pair(profile) == before


@pytest.mark.parametrize("text", ["[not, a, mapping]\n", "skills: {}\nskills: {}\n"])
def test_policy_refuses_ambiguous_delta_without_changes(case, text):
    (case["profile"] / "config.delta.yaml").write_text(text)
    before = pair(case["profile"])
    result = run(case, "skills-policy", "--profile", "demo-pm")
    assert result.returncode != 0
    assert pair(case["profile"]) == before


def test_policy_does_not_require_or_replace_inherited_trust_list(case):
    delta_path = case["profile"] / "config.delta.yaml"
    config_path = case["profile"] / "config.yaml"
    delta = yaml.safe_load(delta_path.read_text())
    delta["skills"].pop("trusted_project_dirs")
    delta_path.write_text(yaml.safe_dump(delta))
    config = yaml.safe_load(config_path.read_text())
    config["skills"]["trusted_project_dirs"] = ["/existing/base"]
    config_path.write_text(yaml.safe_dump(config))
    result = run(case, "skills-policy", "--profile", "demo-pm")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "trusted_project_dirs" not in yaml.safe_load(delta_path.read_text())["skills"]
    assert yaml.safe_load(config_path.read_text())["skills"]["trusted_project_dirs"] == ["/existing/base"]
    assert run(case, "render", "--profile", "demo-pm").returncode == 0
    assert yaml.safe_load(config_path.read_text())["skills"]["trusted_project_dirs"] == ["/existing/base"]


def test_runtime_with_unverified_feature_names_is_still_refused(case):
    (case["runtime"] / "hermes_cli/config_defaults.py").write_text(
        "DEFAULT_CONFIG = {'skills': {'project_discovery': True, 'trusted_project_dirs': []}}\n"
    )
    before = pair(case["profile"])
    result = run(case, "project-skills", "--registered-pms", "--json")
    assert result.returncode == 3, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["runtime"]["blocker"]["code"] == "runtime_project_discovery_unverified"
    assert pair(case["profile"]) == before


def test_wrong_binary_pin_refuses_just_project_binding(case):
    result = run(case, "project-skills", "--registered-pms", "--json", HERMES_FLEET_BIN="/usr/bin/false")
    assert result.returncode == 3, result.stdout + result.stderr
    assert json.loads(result.stdout)["runtime"]["blocker"]["code"] == "runtime_pin_invalid"


def test_policy_requires_explicit_single_profile(case):
    before = pair(case["profile"])
    result = run(case, "skills-policy")
    assert result.returncode != 0
    assert "requires --profile" in result.stderr
    assert pair(case["profile"]) == before


def test_actual_pin_reader_uses_data_only_fleet_assignments(case):
    fleet_env = case["fleet"] / "fleet.env"
    fleet_env.write_text(
        f'HERMES_FLEET_REPO="{case["runtime"]}"\n'
        f'HERMES_FLEET_BIN="{case["runtime"] / ".venv/bin/hermes"}"\n'
    )
    env = {k: v for k, v in case["env"].items() if k not in ("HERMES_FLEET_REPO", "HERMES_FLEET_BIN")}
    result = subprocess.run([sys.executable, "-I", str(WRITER), "project-skills", "--registered-pms", "--json"],
                            env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 3, result.stdout + result.stderr
    assert json.loads(result.stdout)["runtime"]["repo"] == str(case["runtime"])
    marker = case["home"] / "never-created"
    fleet_env.write_text(f'HERMES_FLEET_REPO="$(touch {marker})"\nHERMES_FLEET_BIN=/ignored\n')
    result = subprocess.run([sys.executable, "-I", str(WRITER), "project-skills", "--registered-pms", "--json"],
                            env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 3, result.stdout + result.stderr
    assert json.loads(result.stdout)["runtime"]["blocker"]["code"] == "runtime_pin_invalid"
    assert not marker.exists()


def test_malformed_role_is_an_affected_row_refusal(case):
    (case["role"] / "role.yaml").write_text("role: [unterminated\n")
    before = pair(case["profile"])
    result = run(case, "project-skills", "--registered-pms", "--json")
    assert result.returncode == 3, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert "binding_manifest_mismatch" in [b["code"] for b in row["blockers"]]
    assert pair(case["profile"]) == before


def project_run(case, **extra):
    return run(case, "project-skills", "--registered-pms", "--json", **extra)


def test_supported_writer_preserves_base_and_delta_trust_and_future_base_edits(case):
    supported_case(case)
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["runtime"]["supported"] is True
    row = report["profiles"][0]
    assert row["outcome"] == "updated"
    assert row["blockers"] == []
    profile = case["profile"]
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["skills"]["trusted_project_dirs"] == ["/existing/base", "/existing/profile", str(case["project"])]
    assert config["skills"]["project_discovery"] is True
    assert config["skills"]["external_dirs"] == []
    assert config["skills"]["disabled"] == ["keep-disabled"]
    assert config["operator"] == {"fleet": "keep", "profile": "keep"}
    assert "trusted_project_dirs" not in delta["skills"]
    assert delta["x-pjangler-merge"]["list_patches"]["skills.trusted_project_dirs"]["add"] == ["/existing/profile", str(case["project"])]
    assert (profile / "config.delta.yaml").read_text().startswith("# keep operator header\n")
    before = pair(profile)
    assert project_run(case).returncode == 0
    assert pair(profile) == before
    base_path = case["fleet"] / "config.yaml"
    base = yaml.safe_load(base_path.read_text())
    base["skills"]["trusted_project_dirs"].append("/new/fleet/root")
    base_path.write_text(yaml.safe_dump(base))
    assert run(case, "render", "--profile", "demo-pm").returncode == 0
    assert yaml.safe_load((profile / "config.yaml").read_text())["skills"]["trusted_project_dirs"] == [
        "/existing/base", "/new/fleet/root", "/existing/profile", str(case["project"]),
    ]
    before = pair(profile)
    assert run(case, "skills-policy", "--profile", "demo-pm").returncode == 0
    assert pair(profile) == before


def test_supported_project_dry_run_is_an_exact_pair_noop(case):
    supported_case(case)
    before = pair(case["profile"])
    result = run(case, "project-skills", "--registered-pms", "--json", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["profiles"][0]["outcome"] == "would-update"
    assert pair(case["profile"]) == before


def test_successful_project_writer_timeout_then_retry_reads_current_delta(case):
    supported_case(case)
    profile = case["profile"]
    with (profile.parent / ".demo-pm.config.lock").open("a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        before = pair(profile)
        timed_out = project_run(case, HERMES_PROFILE_CONFIG_LOCK_TIMEOUT_SECONDS="0.05")
        assert timed_out.returncode == 3
        assert "profile_lock_failed" in [b["code"] for b in json.loads(timed_out.stdout)["profiles"][0]["blockers"]]
        assert pair(profile) == before
        for name in ("config.delta.yaml", "config.yaml"):
            path = profile / name
            value = yaml.safe_load(path.read_text())
            value["operator"]["profile"] = "winning value after timeout"
            value["skills"]["trusted_project_dirs"].append("/winning/root")
            path.write_text(yaml.safe_dump(value))
    retry = project_run(case)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["operator"]["profile"] == "winning value after timeout"
    assert config["skills"]["trusted_project_dirs"] == ["/existing/base", "/existing/profile", "/winning/root", str(case["project"])]


def test_successful_project_waiter_snapshots_only_after_lock(case, tmp_path):
    supported_case(case)
    profile = case["profile"]
    attempt = tmp_path / "project-attempt"
    with (profile.parent / ".demo-pm.config.lock").open("a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-I", str(WRITER), "project-skills", "--registered-pms", "--json"],
            env={**case["env"], "PJANGLER_TEST_PROFILE_CONFIG_LOCK_ATTEMPT": str(attempt)},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 6
            while not attempt.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert attempt.exists()
            assert process.poll() is None
            for name in ("config.delta.yaml", "config.yaml"):
                path = profile / name
                value = yaml.safe_load(path.read_text())
                value["operator"]["profile"] = "winning value after lock attempt"
                value["skills"]["trusted_project_dirs"].append("/waiter/root")
                path.write_text(yaml.safe_dump(value))
        except BaseException:
            process.kill()
            process.communicate()
            raise
    stdout, stderr = process.communicate(timeout=8)
    assert process.returncode == 0, stdout + stderr
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert config["operator"]["profile"] == "winning value after lock attempt"
    assert "/waiter/root" in config["skills"]["trusted_project_dirs"]


@pytest.mark.parametrize("target", ["profile", "config.delta.yaml", "config.yaml", "lock"])
def test_supported_project_writer_refuses_symlinks_without_pair_mutation(case, target):
    supported_case(case)
    profile = case["profile"]
    path = profile if target == "profile" else profile.parent / ".demo-pm.config.lock" if target == "lock" else profile / target
    moved = path.with_name(path.name + ".fixture-original")
    if path.exists():
        path.rename(moved)
    else:
        moved.write_text("keep")
    path.symlink_to(moved)
    before = pair(profile)
    assert project_run(case).returncode == 3
    assert pair(profile) == before


def test_supported_project_writer_drift_gate_refuses_before_mutation(case):
    supported_case(case)
    path = case["profile"] / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["operator"]["out_of_band"] = "preserve"
    path.write_text(yaml.safe_dump(config))
    before = pair(case["profile"])
    result = project_run(case)
    assert result.returncode == 3
    assert "profile_config_drift" in [b["code"] for b in json.loads(result.stdout)["profiles"][0]["blockers"]]
    assert pair(case["profile"]) == before


@pytest.mark.parametrize("kind", ["nested-role-git"])
def test_supported_resolver_blocks_only_unresolvable_exact_binding(case, kind):
    supported_case(case)
    if kind == "non-git":
        (case["project"] / ".git").rmdir()
    else:
        (case["role"] / ".git").mkdir()
    before = pair(case["profile"])
    result = project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert any(b["code"] in ("project_root_unresolved", "role_project_root_mismatch") for b in row["blockers"])
    assert pair(case["profile"]) == before
    assert row["project_root"] == str(case["project"])


def explicit_project_run(case, *args):
    return run(case, "project-skills", "--profile", "demo-pm", "--project-root", str(case["project"]),
               "--role-dir", str(case["role"]), "--json", *args)


@pytest.mark.parametrize("explicit", [False, True])
def test_missing_manifest_registered_role_is_independent_trust_evidence(case, explicit):
    supported_case(case)
    (case["project"] / ".project.json").unlink()
    result = explicit_project_run(case) if explicit else project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["manifest_status"] == "missing"
    assert row["registry_identity_status"] == "verified"
    assert row["binding_source"] == "registry+role"
    assert row["trust_eligible"] is True
    assert row["config_enabled"] is True
    assert row["native_discovery_status"] == "discovered"
    config = yaml.safe_load((case["profile"] / "config.yaml").read_text())
    assert config["skills"]["trusted_project_dirs"] == ["/existing/base", "/existing/profile", str(case["project"])]
    before = pair(case["profile"])
    repeat = explicit_project_run(case) if explicit else project_run(case)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert pair(case["profile"]) == before


def test_unregistered_explicit_root_without_manifest_evidence_refuses(case):
    supported_case(case)
    case["registry"].write_text("agents: {}\n")
    (case["project"] / ".project.json").unlink()
    before = pair(case["profile"])
    result = explicit_project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert "binding_evidence_insufficient" in [item["code"] for item in row["blockers"]]
    assert row["trust_eligible"] is False
    assert pair(case["profile"]) == before


@pytest.mark.parametrize("soul_kind", ["symlink", "plain_file"])
def test_unregistered_manifest_role_binding_accepts_proven_desk(case, soul_kind):
    supported_case(case)
    case["registry"].write_text("agents: {}\n")
    if soul_kind == "plain_file":
        soul = case["profile"] / "SOUL.md"
        soul.unlink()
        soul.write_text("Existing standalone persona; identity is proved by manifest and role.\n")
    result = explicit_project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["registry_identity_status"] == "unregistered"
    assert row["binding_source"] == "manifest+role"
    assert row["soul_status"] == soul_kind
    assert row["config_enabled"] is True


@pytest.mark.parametrize("contradiction", ["root", "claim", "missing-claim"])
def test_present_manifest_contradiction_never_falls_back_to_registry(case, contradiction):
    supported_case(case)
    path = case["project"] / ".project.json"
    manifest = json.loads(path.read_text())
    if contradiction == "root":
        manifest["repo_path"] = str(case["home"])
    elif contradiction == "claim":
        manifest["agents"]["demo-pm"]["role_dir"] = "agents/hermes/other"
    else:
        manifest["agents"] = {}
    path.write_text(json.dumps(manifest))
    before = pair(case["profile"])
    result = project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    assert pair(case["profile"]) == before


def test_explicit_binding_cannot_override_conflicting_registry(case):
    supported_case(case)
    data = yaml.safe_load(case["registry"].read_text())
    data["agents"]["demo-pm"]["project_path"] = str(case["home"])
    case["registry"].write_text(yaml.safe_dump(data))
    before = pair(case["profile"])
    result = explicit_project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "binding_registry_mismatch" in [item["code"] for item in json.loads(result.stdout)["profiles"][0]["blockers"]]
    assert pair(case["profile"]) == before


def test_soul_symlink_into_another_project_refuses_without_changes(case):
    supported_case(case)
    soul = case["profile"] / "SOUL.md"
    soul.unlink()
    foreign = case["home"] / "foreign-soul.md"
    foreign.write_text("Foreign persona.\n")
    soul.symlink_to(foreign)
    before = pair(case["profile"])
    result = project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "binding_soul_mismatch" in [item["code"] for item in json.loads(result.stdout)["profiles"][0]["blockers"]]
    assert pair(case["profile"]) == before


def test_non_git_proven_root_is_configured_with_explicit_native_discovery_gap(case):
    supported_case(case)
    (case["project"] / ".git").rmdir()
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["trust_eligible"] is True
    assert row["config_enabled"] is True
    assert row["native_discovery_status"] == "no_native_root"
    assert row["resolver"]["project_cwd"]["root"] is None
    assert row["resolver_after"]["project_cwd"]["project_dirs"] == []
    config = yaml.safe_load((case["profile"] / "config.yaml").read_text())
    assert config["skills"]["trusted_project_dirs"] == ["/existing/base", "/existing/profile", str(case["project"])]
    assert not (case["project"] / ".git").exists()


def test_non_git_below_ancestor_git_never_trusts_that_ancestor(case):
    supported_case(case)
    (case["project"] / ".git").rmdir()
    ancestor = case["home"].parent
    (ancestor / ".git").mkdir()
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["trust_eligible"] is True
    assert row["config_enabled"] is True
    assert row["native_discovery_status"] == "different_native_root"
    assert row["resolver_after"]["project_cwd"]["root"] == str(ancestor)
    config = yaml.safe_load((case["profile"] / "config.yaml").read_text())
    assert str(case["project"]) in config["skills"]["trusted_project_dirs"]
    assert str(ancestor) not in config["skills"]["trusted_project_dirs"]


def test_project_trust_preserves_unknown_legacy_key_and_unrelated_list_patch(case):
    supported_case(case)
    for name in ("config.delta.yaml", "config.yaml"):
        path = case["profile"] / name
        config = yaml.safe_load(path.read_text())
        config["skills"]["trusted_project_roots"] = ["/unknown/legacy-key"]
        if name == "config.delta.yaml":
            config["x-pjangler-merge"] = {"list_patches": {"operator.allowed": {"add": ["keep"]}}}
        else:
            config["operator"]["allowed"] = ["keep"]
        path.write_text(yaml.safe_dump(config))
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    delta = yaml.safe_load((case["profile"] / "config.delta.yaml").read_text())
    current = yaml.safe_load((case["profile"] / "config.yaml").read_text())
    assert delta["skills"]["trusted_project_roots"] == ["/unknown/legacy-key"]
    assert current["skills"]["trusted_project_roots"] == ["/unknown/legacy-key"]
    assert current["operator"]["allowed"] == ["keep"]
    assert delta["x-pjangler-merge"]["list_patches"]["operator.allowed"] == {"add": ["keep"]}


@pytest.mark.parametrize("kind", ["registered-missing-manifest", "unregistered-non-git"])
def test_current_real_pin_supports_evidenced_trust_independent_of_native_root(case, kind):
    supported_case(case)
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("followup_real_pin", ROOT / "template/.scripts/lib/project-skills.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    repo, binary = helper.runtime_pins(SimpleNamespace(HERMES_HOME=Path.home() / ".hermes"))
    case["env"].update(HERMES_FLEET_REPO=str(repo), HERMES_FLEET_BIN=str(binary))
    if kind == "registered-missing-manifest":
        (case["project"] / ".project.json").unlink()
    else:
        case["registry"].write_text("agents: {}\n")
        (case["project"] / ".git").rmdir()
    result = explicit_project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    observed = row["resolver_after"]["project_cwd"]
    assert row["config_enabled"] is True
    assert observed["trusted"] is True
    if kind == "registered-missing-manifest":
        assert observed["root"] == str(case["project"])
        assert observed["project_dirs"] == [str(case["project"] / ".agents/skills")]
    else:
        assert observed["root"] is None
        assert observed["project_dirs"] == []


def test_supported_writer_reports_missing_directory_without_fabricating_it(case):
    supported_case(case)
    skill = case["project"] / ".agents/skills/proof/SKILL.md"
    skill.unlink()
    skill.parent.rmdir()
    skill.parent.parent.rmdir()
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["profiles"][0]["skills_directory_status"] == "missing"
    assert not (case["project"] / ".agents/skills").exists()


def test_current_real_pin_writer_and_resolver_on_disposable_project(case):
    supported_case(case)
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("real_pin_reader", ROOT / "template/.scripts/lib/project-skills.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    repo, binary = helper.runtime_pins(SimpleNamespace(HERMES_HOME=Path.home() / ".hermes"))
    case["env"].update(HERMES_FLEET_REPO=str(repo), HERMES_FLEET_BIN=str(binary))
    result = project_run(case)
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)["profiles"][0]
    assert row["resolver"]["project_cwd"]["root"] == str(case["project"])
    code = '''import sys,json
sys.path.insert(0,sys.argv[1])
from agent.skill_utils import get_project_skills_dirs
from tools.skills_tool import skill_view
result=json.loads(skill_view("proof", file_path="SKILL.md", preprocess=False))
print(json.dumps({"dirs":[str(p) for p in get_project_skills_dirs()], "success":result.get("success"), "path":result.get("_source_path")}))
'''
    observed = subprocess.run([str(repo / ".venv/bin/python"), "-I", "-B", "-c", code, str(repo)],
                              cwd=case["project"], env={**case["env"], "HERMES_HOME":str(case["profile"])},
                              capture_output=True, text=True, timeout=15)
    assert observed.returncode == 0, observed.stderr
    evidence = json.loads(observed.stdout)
    assert evidence["success"] is True
    assert evidence["dirs"] == [str(case["project"] / ".agents/skills")]
    assert evidence["path"] == str(case["project"] / ".agents/skills/proof/SKILL.md")


def test_project_writer_refuses_external_list_patch_escape_without_mutation(case):
    supported_case(case)
    for name in ("config.delta.yaml", "config.yaml"):
        path = case["profile"] / name
        value = yaml.safe_load(path.read_text())
        if name == "config.delta.yaml":
            value["x-pjangler-merge"] = {"list_patches": {"skills.external_dirs": {"add": ["/escaped/external"]}}}
        else:
            value["skills"]["external_dirs"].append("/escaped/external")
        path.write_text(yaml.safe_dump(value))
    before = pair(case["profile"])
    result = project_run(case)
    assert result.returncode == 3, result.stdout + result.stderr
    assert pair(case["profile"]) == before
