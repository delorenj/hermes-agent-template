"""The board-pass entry point (``heartbeat.sh``) and the board-driver lease.

Three bugs found 2026-11-06 while kicking off parked TOLT tickets:

1. A bare ``bash agents/hermes/pm/.scripts/heartbeat.sh`` defaulted HERMES_HOME to the role's
   ``runtime/`` folder, which is not a Hermes home, and Hermes died with "the gateway exited with
   status 1". The pass must run through the role launcher, which resolves the named profile.
2. It ran ``hermes chat --source cron``. ``cron`` is the label the profile scheduler stamps on its
   own admitted runs; a client-created session carrying it is refused by the state store
   (``invalid_params``). The pass runs as ``tool``: a ``oneshot`` session caps delegation at
   ``delegation.oneshot_max_children`` (default 2), a hidden per-pass WIP limit.
3. It acquired ``runtime/wip-driver.lock`` as ``hermes:<agent_id>`` and the PM, told by its SOUL to
   "acquire before driving", acquired under a different owner string, was refused by its own
   launcher's fresh lease, concluded a sibling pass was running and dispatched nothing. A single pass
   must never fight itself, while a foreign fresh owner (Momo) still makes it back off.

The launcher hands the PM its owner in the PROMPT: ``hermes chat`` is a client of the host gateway,
which runs the PM's tools, and a client's ``HERMES_*`` names never reach that session's terminal. So the
fake PM below reads its owner from the prompt, never from the env. The owner is unique per pass
(``hermes:<agent_id>:pass-<utc>-<pid>``) so a concurrent gateway turn of the same agent, which acquires
under its own owner, serializes with the pass instead of sharing (and releasing) one lease.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "template"
LOCK_CLI = TEMPLATE / ".scripts" / "momo-wip-lock.py"
AGENT_ID = "heartbeat-demo-pm"
PASS_OWNER = re.compile(rf"^hermes:{re.escape(AGENT_ID)}:pass-\d{{8}}T\d{{6}}Z-\d+$")
LEASE_ENV = ("HERMES_BOARD_LEASE_PATH", "HERMES_BOARD_LEASE_OWNER", "HERMES_BOARD_LEASE_TTL")

# Stands in for the Hermes binary. Records what the launcher gave it, then optionally plays the PM
# (HB_FAKE_PM): it reads its owner and lease path out of the PROMPT (the only carrier that reaches a
# real gateway session), runs the lease the way the prompt tells it to, tries the names a confused PM
# would invent, and writes every answer down. HB_FAKE_MOMO_AFTER=1: once the PM has released, Momo
# takes the board, so the launcher's exit release must leave Momo's lease alone.
FAKE_HERMES = """#!/usr/bin/env python3
import json, os, re, subprocess, sys
argv = sys.argv[1:]
out = {
    "argv": argv,
    "HERMES_HOME": os.environ.get("HERMES_HOME"),
    "TERMINAL_CWD": os.environ.get("TERMINAL_CWD"),
    "CODEX_HOME": os.environ.get("CODEX_HOME"),
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
    "lease_env": {k: os.environ.get(k) for k in (%(lease_env)r)},
}
if os.environ.get("HB_FAKE_PM"):
    prompt = argv[argv.index("-q") + 1]
    owner = re.search(r"as owner `([^`]+)`", prompt).group(1)
    path = re.search(r"momo-wip-lock.py held +(\\S+) ", prompt).group(1)
    cli = os.environ["HB_LOCK_CLI"]
    bare_env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_BOARD_LEASE_")}
    def run(*a):
        r = subprocess.run([sys.executable, cli, *a], capture_output=True, text=True, env=bare_env)
        return {"rc": r.returncode, "out": r.stdout.strip()}
    agent = owner.split(":")[1]
    out["pm"] = {
        "owner": owner,
        "path": path,
        "held_exact": run("held", path, owner),
        "acquire_exact": run("acquire", path, owner),
        "acquire_bare_agent": run("acquire", path, "hermes:" + agent),
        "acquire_invented": run("acquire", path, "hermes:" + agent + ":sentinel-20261106T161733", "--ttl", "300"),
        "refresh_exact": run("refresh", path, owner),
        "release_exact": run("release", path, owner),
        "reacquire_exact": run("acquire", path, owner, "--ttl", "3600"),
        "release_again": run("release", path, owner),
    }
    if os.environ.get("HB_FAKE_MOMO_AFTER"):
        out["pm"]["momo_takes_board"] = run("acquire", path, "momo", "--ttl", "3600")
json.dump(out, open(os.environ["HB_MARKER"], "w"))
""" % {"lease_env": LEASE_ENV}


def _desk(tmp_path: Path, *, profile: bool = True, launcher: bool = True):
    """A rendered PM desk in a disposable HOME: repo, role dir, scripts, launcher, named profile."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    role = project / "agents" / "hermes" / "pm"
    runtime = role / "runtime"
    runtime.mkdir(parents=True)
    profile_dir = home / ".hermes" / "profiles" / AGENT_ID
    if profile:
        profile_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    shutil.copytree(TEMPLATE / ".scripts", role / ".scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (role / ".scripts" / "sentinel.prompt.md").write_text("fixture prompt\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        f"repo: heartbeat-demo\nrole: pm\nagent_id: {AGENT_ID}\nprofile: {AGENT_ID}\n"
        "reconcile:\n  enabled: true\nticket_provider:\n  name: plane\n",
        encoding="utf-8",
    )
    if launcher:
        script = role / "hermes"
        script.write_text(
            (TEMPLATE / "hermes.jinja").read_text(encoding="utf-8").replace("{{ agent_id }}", AGENT_ID),
            encoding="utf-8",
        )
        script.chmod(0o755)
    fake = tmp_path / "fake-hermes"
    fake.write_text(FAKE_HERMES, encoding="utf-8")
    fake.chmod(0o755)
    fleet = home / ".hermes" / "fleet.env"
    fleet.parent.mkdir(parents=True, exist_ok=True)
    fleet.write_text(f"HERMES_FLEET_BIN=$'{fake}'\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "HERMES_HOME", "HERMES_BIN", "HERMES_FLEET_BIN", "HERMES_FLEET_HOME", "HERMES_PROFILE_NAME",
            "HERMES_FLEET_REGISTRY_FILE", "HERMES_FLEET_REPO", "TELEGRAM_BOT_TOKEN", "SENTINEL_SESSION_SOURCE",
            *LEASE_ENV,
        }
    }
    env.update(
        HOME=str(home),
        HERMES_FLEET_ENV=str(fleet),
        HERMES_TEMPLATE_CONFIG=str(tmp_path / "missing-config.toml"),
        HB_MARKER=str(tmp_path / "marker.json"),
        HB_LOCK_CLI=str(LOCK_CLI),
    )
    return role, runtime, profile_dir, project, env


def _heartbeat(role: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(role / ".scripts" / "heartbeat.sh")], env=env, text=True, capture_output=True, check=False
    )


def _marker(env: dict[str, str]) -> dict:
    return json.loads(Path(env["HB_MARKER"]).read_text(encoding="utf-8"))


def _lock(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LOCK_CLI), *args], capture_output=True, text=True, env=env or os.environ.copy()
    )


def _owner_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in LEASE_ENV}
    env.update(extra)
    return env


# --- bug 1: the home the pass runs against ------------------------------------------------------


def test_bare_heartbeat_resolves_the_named_profile_not_runtime(tmp_path: Path) -> None:
    role, runtime, profile_dir, project, env = _desk(tmp_path)
    assert "HERMES_HOME" not in env

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    seen = _marker(env)
    assert seen["HERMES_HOME"] == str(profile_dir)
    assert seen["HERMES_HOME"] != str(runtime)
    # Everything else the launcher owns reaches the pass too.
    assert seen["TERMINAL_CWD"] == str(project)
    assert seen["CODEX_HOME"]


def test_a_callers_hermes_home_cannot_redirect_the_pass(tmp_path: Path) -> None:
    role, runtime, profile_dir, _project, env = _desk(tmp_path)
    env["HERMES_HOME"] = str(runtime)  # the old, wrong default, now handed in explicitly
    env["TELEGRAM_BOT_TOKEN"] = "must-not-reach-the-profile"

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    seen = _marker(env)
    assert seen["HERMES_HOME"] == str(profile_dir)
    assert seen["TELEGRAM_BOT_TOKEN"] is None


def test_a_callers_profile_name_cannot_swap_the_desk(tmp_path: Path) -> None:
    """role.yaml's profile is pinned for the launcher: a caller's HERMES_PROFILE_NAME (a Kanban worker pins
    its own) must not boot another profile with this desk's prompt and lease owner."""
    role, _runtime, profile_dir, _project, env = _desk(tmp_path)
    other = profile_dir.parent / "some-kanban-worker"
    other.mkdir()
    env["HERMES_PROFILE_NAME"] = "some-kanban-worker"

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    assert _marker(env)["HERMES_HOME"] == str(profile_dir)


def test_missing_profile_fails_loud_and_never_boots_hermes(tmp_path: Path) -> None:
    role, _runtime, _profile, _project, env = _desk(tmp_path, profile=False)

    result = _heartbeat(role, env)

    assert result.returncode != 0
    assert "profile not provisioned" in result.stderr
    assert not Path(env["HB_MARKER"]).exists()
    # ...and the lease the failed pass took is not left behind.
    assert not (role / "runtime" / "wip-driver.lock").exists()


def test_missing_launcher_is_refused_before_any_state_is_written(tmp_path: Path) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path, launcher=False)

    result = _heartbeat(role, env)

    assert result.returncode == 1
    assert "role launcher" in result.stderr
    assert not Path(env["HB_MARKER"]).exists()
    assert not (runtime / "wip-driver.lock").exists()
    assert not (runtime / "continuous-ticket-sentinel-state.json").exists()


# --- bug 2: the session source -------------------------------------------------------------------


def test_pass_runs_as_a_tool_session_never_cron_or_oneshot(tmp_path: Path) -> None:
    role, _runtime, _profile, _project, env = _desk(tmp_path)

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    argv = _marker(env)["argv"]
    assert argv[0] == "chat" and "-Q" in argv
    # tool, not oneshot: oneshot caps the pass at delegation.oneshot_max_children (2) workers.
    assert argv[argv.index("--source") + 1] == "tool"
    assert "cron" not in argv[: argv.index("-q")]
    assert argv[argv.index("--max-turns") + 1] == "90"


def test_session_source_can_be_overridden_to_another_creatable_source(tmp_path: Path) -> None:
    role, _runtime, _profile, _project, env = _desk(tmp_path)
    env["SENTINEL_SESSION_SOURCE"] = "oneshot"

    assert _heartbeat(role, env).returncode == 0
    argv = _marker(env)["argv"]
    assert argv[argv.index("--source") + 1] == "oneshot"


@pytest.mark.parametrize("bad", ["cron", "kanban", "telegram"])
def test_scheduler_owned_or_unknown_source_is_refused_before_anything_runs(tmp_path: Path, bad: str) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)
    env["SENTINEL_SESSION_SOURCE"] = bad

    result = _heartbeat(role, env)

    assert result.returncode == 2
    assert "not a creatable Hermes session source" in result.stderr
    assert not Path(env["HB_MARKER"]).exists()
    assert not (runtime / "wip-driver.lock").exists()


# --- bug 3: one pass never fights its own lease --------------------------------------------------


def test_launcher_held_lease_is_the_agents_own_and_reentrant(tmp_path: Path) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)
    env["HB_FAKE_PM"] = "1"
    lock = runtime / "wip-driver.lock"

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    seen = _marker(env)
    pm = seen["pm"]
    owner = pm["owner"]
    # One owner per pass, never the bare agent id a concurrent gateway turn could also use.
    assert PASS_OWNER.match(owner), owner
    assert pm["path"] == str(lock)
    # Exported for the launcher's direct children only (a gateway session never sees HERMES_* names).
    assert seen["lease_env"] == {
        "HERMES_BOARD_LEASE_PATH": str(lock),
        "HERMES_BOARD_LEASE_OWNER": owner,
        "HERMES_BOARD_LEASE_TTL": "3600",
    }
    # The lease the launcher took is the PM's under the owner the prompt named (no env needed).
    assert pm["held_exact"]["rc"] == 0
    assert pm["acquire_exact"]["rc"] == 0 and "ACQUIRED" in pm["acquire_exact"]["out"]
    assert pm["refresh_exact"]["rc"] == 0
    # Inventing a new owner name is exactly the bug: the agent's own lease refuses it.
    assert pm["acquire_bare_agent"]["rc"] == 1 and f"HELD by {owner}" in pm["acquire_bare_agent"]["out"]
    assert pm["acquire_invented"]["rc"] == 1 and f"HELD by {owner}" in pm["acquire_invented"]["out"]
    # Released early before waiting on workers, then taken back with the same owner to write the board.
    assert pm["release_exact"]["rc"] == 0
    assert pm["reacquire_exact"]["rc"] == 0
    assert pm["release_again"]["rc"] == 0
    assert not lock.exists()


def test_the_exit_release_only_drops_this_passs_own_lease(tmp_path: Path) -> None:
    """The PM released early and Momo took the board: the launcher's exit trap must leave Momo's lease."""
    role, runtime, _profile, _project, env = _desk(tmp_path)
    env.update(HB_FAKE_PM="1", HB_FAKE_MOMO_AFTER="1")
    lock = runtime / "wip-driver.lock"

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    assert _marker(env)["pm"]["momo_takes_board"]["rc"] == 0
    assert json.loads(lock.read_text())["owner"] == "momo"


@pytest.mark.parametrize("turn_owner", [f"hermes:{AGENT_ID}", f"hermes:{AGENT_ID}:turn-telegram-42"])
def test_a_concurrent_turn_of_the_same_agent_makes_the_pass_back_off(tmp_path: Path, turn_owner: str) -> None:
    """A gateway turn of this same agent (Telegram, a Bloodbank or crank delegation) holding the lease is
    another driver: the pass must not share it ("reentrant") and boot a second driver on the board."""
    role, runtime, _profile, _project, env = _desk(tmp_path)
    lock = runtime / "wip-driver.lock"
    assert _lock("acquire", str(lock), turn_owner).returncode == 0

    result = _heartbeat(role, env)

    assert result.returncode == 0
    assert f"HELD by {turn_owner}" in result.stdout
    assert not Path(env["HB_MARKER"]).exists()
    assert json.loads(lock.read_text())["owner"] == turn_owner


def test_the_prompt_tells_the_pm_the_lease_is_already_theirs(tmp_path: Path) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)

    assert _heartbeat(role, env).returncode == 0

    seen = _marker(env)
    argv = seen["argv"]
    prompt = argv[argv.index("-q") + 1]
    owner = seen["lease_env"]["HERMES_BOARD_LEASE_OWNER"]
    lock = runtime / "wip-driver.lock"
    assert PASS_OWNER.match(owner), owner
    assert prompt.startswith("fixture prompt")
    assert "Board-driver lease for THIS pass (already held for you)" in prompt
    assert f"as owner `{owner}`" in prompt
    assert f"momo-wip-lock.py refresh {lock} {owner}" in prompt
    assert f"momo-wip-lock.py release {lock} {owner}" in prompt
    # Taking it back after an early release is allowed, with the same owner only.
    assert f"momo-wip-lock.py acquire {lock} {owner} --ttl 3600" in prompt
    assert "Never acquire the lease under any other owner name" in prompt
    # The env is not a carrier inside a gateway session: the prompt must not lean on it.
    assert "never omit the owner" in prompt
    assert "reads it from the env" not in prompt


def test_the_lease_is_released_when_the_pass_ends(tmp_path: Path) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)

    assert _heartbeat(role, env).returncode == 0

    assert not (runtime / "wip-driver.lock").exists()


@pytest.mark.parametrize("foreign", ["momo", "hermes:some-other-pm"])
def test_a_foreign_fresh_owner_still_makes_the_pass_back_off(tmp_path: Path, foreign: str) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)
    lock = runtime / "wip-driver.lock"
    assert _lock("acquire", str(lock), foreign, "--ttl", "3600").returncode == 0

    result = _heartbeat(role, env)

    assert result.returncode == 0
    assert f"HELD by {foreign}" in result.stdout
    assert "skipping full reconcile pass" in result.stdout
    assert not Path(env["HB_MARKER"]).exists(), "Hermes must not boot while another driver holds the board"
    # The foreign lease is untouched, not released by our exit trap.
    assert json.loads(lock.read_text())["owner"] == foreign


def test_a_stale_foreign_lease_does_not_wedge_the_board(tmp_path: Path) -> None:
    role, runtime, _profile, _project, env = _desk(tmp_path)
    lock = runtime / "wip-driver.lock"
    lock.write_text(json.dumps({"owner": "momo", "heartbeat_at": time.time() - 7200, "ttl": 3600}))

    result = _heartbeat(role, env)

    assert result.returncode == 0, result.stderr
    assert PASS_OWNER.match(_marker(env)["lease_env"]["HERMES_BOARD_LEASE_OWNER"])


# --- the lease CLI itself -----------------------------------------------------------------------


def test_reentrant_acquire_keeps_the_leases_longer_ttl(tmp_path: Path) -> None:
    lock = str(tmp_path / "l")
    assert _lock("acquire", lock, "hermes:x-pm", "--ttl", "3600").returncode == 0
    assert _lock("acquire", lock, "hermes:x-pm").returncode == 0  # a PM re-asserting, no --ttl
    assert json.loads(Path(lock).read_text())["ttl"] == 3600
    # An explicit ttl still wins, and a different owner after expiry starts from the default.
    assert _lock("acquire", lock, "hermes:x-pm", "--ttl", "60").returncode == 0
    assert json.loads(Path(lock).read_text())["ttl"] == 60


def test_fresh_acquire_defaults_to_the_short_ttl(tmp_path: Path) -> None:
    lock = str(tmp_path / "l")
    assert _lock("acquire", lock, "momo").returncode == 0
    assert json.loads(Path(lock).read_text())["ttl"] == 300


def test_owner_defaults_to_an_exported_owner_for_direct_child_scripts(tmp_path: Path) -> None:
    # Only for scripts the launcher runs itself: a gateway-run PM's terminal never sees this env.
    lock = str(tmp_path / "l")
    assert _lock("acquire", lock, "hermes:x-pm", "--ttl", "3600").returncode == 0
    env = _owner_env(HERMES_BOARD_LEASE_OWNER="hermes:x-pm")
    assert _lock("held", lock, env=env).returncode == 0
    assert _lock("refresh", lock, env=env).returncode == 0
    assert _lock("acquire", lock, env=env).returncode == 0
    assert _lock("release", lock, env=env).returncode == 0
    assert not Path(lock).exists()


def test_owner_is_still_required_without_the_env(tmp_path: Path) -> None:
    result = _lock("refresh", str(tmp_path / "l"), env=_owner_env())
    assert result.returncode == 2
    assert "HERMES_BOARD_LEASE_OWNER" in result.stderr


def test_held_distinguishes_mine_foreign_and_free(tmp_path: Path) -> None:
    lock = str(tmp_path / "l")
    assert _lock("held", lock, "hermes:x-pm").returncode == 1  # free
    assert _lock("acquire", lock, "momo", "--ttl", "3600").returncode == 0
    foreign = _lock("held", lock, "hermes:x-pm")
    assert foreign.returncode == 1 and "back off" in foreign.stdout
    assert _lock("held", lock, "momo").returncode == 0
    Path(lock).write_text(json.dumps({"owner": "hermes:x-pm", "heartbeat_at": time.time() - 9999, "ttl": 300}))
    assert _lock("held", lock, "hermes:x-pm").returncode == 1  # stale is not held


def test_only_the_identical_owner_string_is_reentrant(tmp_path: Path) -> None:
    lock = str(tmp_path / "l")
    assert _lock("acquire", lock, "hermes:x-pm", "--ttl", "3600").returncode == 0
    invented = _lock("acquire", lock, "hermes:x-pm:sentinel-20261106T161733")
    assert invented.returncode == 1
    assert _lock("acquire", lock, "momo").returncode == 1  # Momo can still be made to wait too


# --- the wording the PM reads --------------------------------------------------------------------


def _render_prompt() -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    template = env.from_string((TEMPLATE / ".scripts" / "sentinel.prompt.md.jinja").read_text(encoding="utf-8"))
    return template.render(
        agent_id=AGENT_ID, role="pm", target_repo="demo", display_name="Demo PM", ticket_provider="plane"
    )


def test_sentinel_prompt_states_the_self_lease_rule() -> None:
    text = _render_prompt()
    flat = " ".join(text.split())
    assert "yours if the launcher holds it" in flat
    assert "Never acquire it under any other name" in flat
    assert "that exact owner written out literally" in flat
    # No launcher: one unique owner per turn, reused verbatim, default ttl (a turn has no exit trap).
    assert f"hermes:{AGENT_ID}:<turn tag>" in flat
    assert "Never regenerate it" in flat
    assert "--ttl 3600" not in flat
    # Another pass of the same desk is a real sibling driver, not "you".
    assert "another pass or turn of your own desk" in flat
    assert "any other fresh owner means back off" in flat
    # The launcher's env never reaches a gateway session, so the prompt must not test for it.
    assert "HERMES_BOARD_LEASE_OWNER" not in flat
    # The lease stays a serializer, never a WIP limit.
    assert "not** a WIP limit" in text
