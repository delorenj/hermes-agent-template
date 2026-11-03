"""Linux provisioning follows the macOS/launchd contract: ONE multiplexed host gateway.

`70-systemd.sh` never writes or enables a per-agent gateway unit. It retires one if an older
template left it behind, and (once a channel credential is verified) makes sure the host gateway
`hermes-gateway.service` is managed, never restarted, and serves this profile.

Why: on 2026-11-02 hiring tolt-pm enabled `hermes-tolt-pm-gateway.service`, which became the
host gateway owner (serving every profile) while the canonical `hermes-gateway.service` looped on
"The host gateway already serves profile 'default' - nothing to start".

Everything below runs against fakes: a stateful fake `systemctl`, a fake `hermes` launcher and a
fake runtime interpreter that prints the host-gateway probe verdict. Nothing touches the real
user manager.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "template" / ".scripts"
AGENT_UNIT = "hermes-demo-pm-gateway.service"
HOST_UNIT = "hermes-gateway.service"

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# Stateful fake of `systemctl --user`. Unit state lives in $SC_STATE/<unit>.{active,enabled,main}.
printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
[[ "$1" == "--user" ]] && shift
cmd="$1"; shift
state() { [[ -f "$SC_STATE/$1.$2" ]] && cat "$SC_STATE/$1.$2" || true; }
case "$cmd" in
  is-system-running) echo running; exit 0 ;;
  daemon-reload|reset-failed) exit 0 ;;
  is-active)
    v="$(state "$1" active)"
    case "$v" in
      "") echo inactive; exit 4 ;;
      active|activating) echo "$v"; exit 0 ;;
      *) echo "$v"; exit 3 ;;
    esac ;;
  is-enabled)
    v="$(state "$1" enabled)"
    case "$v" in
      "") echo not-found; exit 4 ;;
      enabled) echo enabled; exit 0 ;;
      *) echo "$v"; exit 1 ;;
    esac ;;
  disable)
    now=0; [[ "$1" == "--now" ]] && { now=1; shift; }
    [[ -z "${SC_DISABLE_FAIL:-}" ]] || exit 1
    [[ -f "$SC_STATE/$1.enabled" ]] && echo disabled > "$SC_STATE/$1.enabled"
    if (( now )) && [[ -f "$SC_STATE/$1.active" ]]; then echo inactive > "$SC_STATE/$1.active"; fi
    exit 0 ;;
  stop) echo inactive > "$SC_STATE/$1.active"; exit 0 ;;
  show)
    unit="$1"; main="$(state "$unit" main)"
    printf 'MainPID=%s\n' "${main:-0}"
    printf 'ControlGroup=\n'
    exit 0 ;;
esac
exit 1
"""

FAKE_HERMES = r"""#!/usr/bin/env bash
if [[ "$1" == "--print-runtime-command" ]]; then
  printf '["%s", "-I", "-c", "bootstrap"]\n' "$FAKE_RUNTIME_PY"
  exit 0
fi
if [[ "$1" == "gateway" ]]; then
  printf 'HERMES_HOME=%s %s\n' "$HERMES_HOME" "$*" >> "$HERMES_LOG"
  if [[ "$2" == "install" ]]; then
    echo enabled > "$SC_STATE/hermes-gateway.service.enabled"
    echo active > "$SC_STATE/hermes-gateway.service.active"
    echo 9100 > "$SC_STATE/hermes-gateway.service.main"
  fi
  exit 0
fi
exit 0
"""

# argv: -I -c <program> <repo> <profile> <rescan>
FAKE_RUNTIME_PY = r"""#!/usr/bin/env bash
printf 'profile=%s rescan=%s home=%s\n' "$5" "$6" "$HERMES_HOME" >> "$PROBE_LOG"
cat "$PROBE_VERDICT"
"""


def _make_role(tmp_path: Path, *, telegram: str = "deferred") -> Path:
    role = tmp_path / "role"
    scripts = role / ".scripts"
    (role / "runtime").mkdir(parents=True)
    scripts.mkdir(parents=True)
    shutil.copytree(SCRIPTS / "lib", scripts / "lib")
    for name in ("_lib.sh", "credential-launch.sh", "70-systemd.sh", "99-summary.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    shutil.copy2(SCRIPTS / "store-onepassword-secret.py", scripts)
    (scripts / "heartbeat.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (role / "role.yaml").write_text(
        f"""repo: demo
role: pm
agent_id: demo-pm
display_name: "Demo PM"
profile: demo-pm
model:
  provider: ""
  name: ""
  base_url: ""
  api_mode: ""
  key_env: ""
telegram:
  provisioning_status: "{telegram}"
  bot_username: "demo_pm_bot"
  bot_id: ""
slack:
  provisioning_status: "deferred"
bloodbank:
  enabled: false
service_state:
  gateway: "pending"
  heartbeat: "pending"
plane:
  workspace: "test"
  identifier: ""
runtime:
  github_owner: "test"
  github_repo: "agent-hm-demo-pm"
""",
        encoding="utf-8",
    )
    return role


class Host:
    """A disposable HOME plus the fakes the 70 step talks to."""

    def __init__(self, tmp_path: Path, *, telegram: str = "deferred") -> None:
        self.tmp = tmp_path
        self.role = _make_role(tmp_path, telegram=telegram)
        self.home = tmp_path / "home"
        self.profile = self.home / ".hermes" / "profiles" / "demo-pm"
        self.profile.mkdir(parents=True)
        self.units = self.home / ".config" / "systemd" / "user"
        self.state = tmp_path / "sc-state"
        self.state.mkdir()
        self.sysctl_log = tmp_path / "systemctl.log"
        self.hermes_log = tmp_path / "hermes.log"
        self.probe_log = tmp_path / "probe.log"
        self.verdict = tmp_path / "verdict"
        self.verdict.write_text("ok|no-host\n", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("hermes", FAKE_HERMES), ("runtime-python", FAKE_RUNTIME_PY)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        op = bin_dir / "op"
        op.write_text("#!/usr/bin/env bash\n[[ \"$1\" == read ]] && { echo resolved; exit 0; }; exit 1\n", encoding="utf-8")
        op.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(
            HOME=str(self.home),
            PATH=f"{bin_dir}:{self.env['PATH']}",
            HERMES_BIN=str(bin_dir / "hermes"),
            FAKE_RUNTIME_PY=str(bin_dir / "runtime-python"),
            HERMES_FLEET_ENV=str(self.home / ".hermes" / "fleet.env"),
            REGISTRY_FILE=str(tmp_path / "registry.yaml"),
            SC_STATE=str(self.state),
            SYSTEMCTL_LOG=str(self.sysctl_log),
            HERMES_LOG=str(self.hermes_log),
            PROBE_LOG=str(self.probe_log),
            PROBE_VERDICT=str(self.verdict),
            HOST_GATEWAY_SERVE_ATTEMPTS="2",
            HOST_GATEWAY_SERVE_INTERVAL_SECONDS="0",
        )
        if telegram == "verified":
            (self.profile / "config.delta.yaml").write_text(
                "secrets:\n  onepassword:\n    enabled: true\n    env:\n"
                "      TELEGRAM_BOT_TOKEN: op://DeLoSecrets/demo/password\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)

    def unit(self, name: str, *, active: str | None, enabled: str | None, main: int | None = None) -> None:
        for suffix, value in (("active", active), ("enabled", enabled), ("main", main)):
            if value is not None:
                (self.state / f"{name}.{suffix}").write_text(f"{value}\n", encoding="utf-8")

    def agent_unit_files(self, *, dropin: bool = False) -> Path:
        self.units.mkdir(parents=True, exist_ok=True)
        path = self.units / AGENT_UNIT
        path.write_text("[Service]\nExecStart=/old/credential-launch.sh gateway\n", encoding="utf-8")
        if dropin:
            (self.units / f"{AGENT_UNIT}.d").mkdir()
            (self.units / f"{AGENT_UNIT}.d" / "10-versioned-runtime.conf").write_text("[Service]\n", encoding="utf-8")
        return path

    def probe(self, verdict: str) -> None:
        self.verdict.write_text(verdict + "\n", encoding="utf-8")

    def run(self, step: str = "70-systemd.sh") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.role / ".scripts" / step)],
            env=self.env, text=True, capture_output=True, check=False,
        )

    @property
    def calls(self) -> str:
        return self.sysctl_log.read_text(encoding="utf-8") if self.sysctl_log.exists() else ""

    @property
    def hermes_calls(self) -> str:
        return self.hermes_log.read_text(encoding="utf-8") if self.hermes_log.exists() else ""

    def service_state(self) -> dict:
        return yaml.safe_load((self.role / "role.yaml").read_text(encoding="utf-8"))["service_state"]

    @property
    def marker(self) -> Path:
        return self.role / ".scripts" / ".done-70-systemd"


def _assert_no_per_agent_gateway(host: Host) -> None:
    assert not (host.units / AGENT_UNIT).exists()
    assert not (host.units / f"{AGENT_UNIT}.d").exists()
    assert f"enable --now {AGENT_UNIT}" not in host.calls
    assert f"enable {AGENT_UNIT}" not in host.calls
    if host.units.exists():
        assert not list(host.units.glob("hermes-*-gateway.service"))


def test_deferred_gateway_writes_no_unit_and_records_deferred(tmp_path: Path) -> None:
    host = Host(tmp_path)

    result = host.run()

    assert result.returncode == 0, result.stderr
    _assert_no_per_agent_gateway(host)
    assert host.service_state() == {"gateway": "deferred", "heartbeat": "retired"}
    assert host.marker.is_file()
    # Nothing about the host gateway is touched when no channel credential is verified.
    assert host.hermes_calls == ""
    assert not host.probe_log.exists()


def test_verified_channel_uses_the_host_gateway_and_never_a_per_agent_unit(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.unit(HOST_UNIT, active="active", enabled="enabled", main=9100)
    host.probe("ok|serving|9100")

    result = host.run()

    assert result.returncode == 0, result.stderr
    _assert_no_per_agent_gateway(host)
    assert host.service_state() == {"gateway": "active", "heartbeat": "retired"}
    assert host.marker.is_file()
    # A running host gateway is NEVER restarted, reinstalled, started or stopped.
    assert host.hermes_calls == ""
    assert f"restart {HOST_UNIT}" not in host.calls
    assert f"stop {HOST_UNIT}" not in host.calls
    # It asked the host to rescan profiles/ for THIS profile, against the fleet root.
    probe = host.probe_log.read_text(encoding="utf-8")
    assert "profile=demo-pm rescan=1" in probe
    assert f"home={host.home / '.hermes'}" in probe
    assert "serves 'demo-pm'" in result.stderr or "serves profile 'demo-pm'" in result.stderr


def test_missing_host_gateway_is_installed_through_the_cli_once(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.probe("ok|serving|9100")

    result = host.run()

    assert result.returncode == 0, result.stderr
    # Installed with the fork's own CLI, against the fleet root, only because it was missing.
    assert host.hermes_calls.splitlines() == [
        f"HERMES_HOME={host.home / '.hermes'} gateway install --if-missing"
    ]
    assert host.service_state()["gateway"] == "active"
    _assert_no_per_agent_gateway(host)


def test_stopped_installed_host_gateway_is_started_not_restarted(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.unit(HOST_UNIT, active="inactive", enabled="enabled", main=0)
    host.probe("ok|no-host")
    # After `start` the host is up and serving.
    start_probe = tmp_path / "bin" / "hermes"
    start_probe.write_text(
        start_probe.read_text(encoding="utf-8").replace(
            'exit 0\nfi\nexit 0',
            'if [[ "$2" == "start" ]]; then\n    echo "ok|serving|9100" > "$PROBE_VERDICT"\n'
            '    echo active > "$SC_STATE/hermes-gateway.service.active"\n'
            '    echo 9100 > "$SC_STATE/hermes-gateway.service.main"\n  fi\n  exit 0\nfi\nexit 0',
        ),
        encoding="utf-8",
    )

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"HERMES_HOME={host.home / '.hermes'} gateway start" in host.hermes_calls
    assert "gateway restart" not in host.hermes_calls
    assert "gateway install" not in host.hermes_calls
    assert host.service_state()["gateway"] == "active"


def test_host_gateway_that_cannot_be_shown_serving_is_an_error_not_a_unit(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.unit(HOST_UNIT, active="active", enabled="enabled", main=9100)
    host.probe("ok|not-serving|9100")
    host.marker.touch()

    result = host.run()

    assert result.returncode != 0
    assert "does not serve profile 'demo-pm'" in result.stderr
    assert host.service_state()["gateway"] == "error"
    assert not host.marker.exists()
    _assert_no_per_agent_gateway(host)
    # A pre-existing host gateway serves other profiles: report it, never stop it.
    assert "stop" not in host.hermes_calls
    assert f"stop {HOST_UNIT}" not in host.calls


def test_a_host_gateway_created_by_this_run_is_stopped_if_it_never_serves(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.probe("error|control socket never answered")

    result = host.run()

    assert result.returncode != 0
    assert host.service_state()["gateway"] == "error"
    assert "gateway install --if-missing" in host.hermes_calls
    assert "gateway stop" in host.hermes_calls


def test_retires_an_idle_per_agent_gateway_unit_the_way_the_consumer_is_retired(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.agent_unit_files(dropin=True)
    host.unit(AGENT_UNIT, active="inactive", enabled="enabled", main=0)
    legacy_heartbeat = host.units / "hermes-demo-pm-heartbeat.timer"
    legacy_heartbeat.write_text("[Timer]\n", encoding="utf-8")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable --now {AGENT_UNIT}" in host.calls
    assert "daemon-reload" in host.calls
    _assert_no_per_agent_gateway(host)
    assert not legacy_heartbeat.exists()
    assert f"retired per-agent gateway unit: {AGENT_UNIT}" in result.stderr
    assert "LOUD WARNING" not in result.stderr
    assert host.service_state() == {"gateway": "deferred", "heartbeat": "retired"}


def test_retires_a_per_agent_unit_that_is_a_stray_gateway_and_stops_it(tmp_path: Path) -> None:
    # Active, but the live host is a DIFFERENT process (hermes-gateway.service's): safe to stop.
    host = Host(tmp_path)
    host.agent_unit_files()
    host.unit(AGENT_UNIT, active="active", enabled="enabled", main=4242)
    host.unit(HOST_UNIT, active="active", enabled="enabled", main=9100)
    host.probe("ok|serving|9100")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable --now {AGENT_UNIT}" in host.calls
    _assert_no_per_agent_gateway(host)
    assert "LOUD WARNING" not in result.stderr


def test_never_stops_a_per_agent_unit_whose_process_is_the_live_host_gateway(tmp_path: Path) -> None:
    # The 2026-11-02 incident: hermes-tolt-pm-gateway.service IS the host gateway.
    host = Host(tmp_path)
    host.agent_unit_files(dropin=True)
    host.unit(AGENT_UNIT, active="active", enabled="enabled", main=4242)
    host.unit(HOST_UNIT, active="activating", enabled="enabled", main=9100)
    host.probe("ok|serving|4242")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable {AGENT_UNIT}" in host.calls
    assert f"disable --now {AGENT_UNIT}" not in host.calls
    assert f"stop {AGENT_UNIT}" not in host.calls
    assert "restart" not in host.calls
    _assert_no_per_agent_gateway(host)  # file and drop-in gone, daemon-reloaded
    assert "daemon-reload" in host.calls
    assert "LOUD WARNING" in result.stderr
    assert "NOT stopped" in result.stderr
    assert "PID 4242" in result.stderr
    assert 'hermes-gateway-restart --reason "' in result.stderr
    # The retired unit's process is still the fleet's gateway; the script does not touch it.
    assert (host.state / f"{AGENT_UNIT}.active").read_text(encoding="utf-8").strip() == "active"
    assert host.hermes_calls == ""


def test_handover_is_flagged_when_the_serving_host_is_not_hermes_gateway_service(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.agent_unit_files()
    host.unit(AGENT_UNIT, active="active", enabled="enabled", main=4242)
    host.unit(HOST_UNIT, active="active", enabled="enabled", main=9100)
    host.probe("ok|serving|4242")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable --now {AGENT_UNIT}" not in host.calls
    assert host.service_state()["gateway"] == "active"  # it does serve the profile
    assert "is NOT managed by hermes-gateway.service" in result.stderr
    assert "hermes-gateway-restart --reason" in result.stderr
    assert host.hermes_calls == ""  # never restarted, installed or started


def test_per_agent_unit_is_kept_running_when_the_host_cannot_be_probed(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.agent_unit_files()
    host.unit(AGENT_UNIT, active="active", enabled="enabled", main=4242)
    host.probe("error|cannot resolve the hermes runtime interpreter")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable --now {AGENT_UNIT}" not in host.calls
    assert f"disable {AGENT_UNIT}" in host.calls
    assert "LOUD WARNING" in result.stderr
    assert not (host.units / AGENT_UNIT).exists()


def test_per_agent_unit_is_preserved_when_it_cannot_be_disabled(tmp_path: Path) -> None:
    host = Host(tmp_path)
    unit = host.agent_unit_files()
    host.unit(AGENT_UNIT, active="inactive", enabled="enabled", main=0)
    host.env["SC_DISABLE_FAIL"] = "1"

    result = host.run()

    assert result.returncode != 0
    assert "per-agent gateway disable failed; preserving unit" in result.stderr
    assert unit.is_file()


def test_retirement_is_idempotent_for_an_unmanaged_leftover_process(tmp_path: Path) -> None:
    # Second run after a hand-over-pending retire: the unit FILE is gone, systemd still holds the
    # running process. It must still never be stopped.
    host = Host(tmp_path)
    host.unit(AGENT_UNIT, active="active", enabled=None, main=4242)
    host.probe("ok|serving|4242")

    result = host.run()

    assert result.returncode == 0, result.stderr
    assert f"disable --now {AGENT_UNIT}" not in host.calls
    assert "LOUD WARNING" in result.stderr


def test_without_a_user_manager_leftover_unit_files_are_removed_and_nothing_is_enabled(tmp_path: Path) -> None:
    host = Host(tmp_path, telegram="verified")
    host.agent_unit_files(dropin=True)
    systemctl = tmp_path / "bin" / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
case "$*" in
  *"is-active hermes-demo-pm-consumer.service"*) echo inactive; exit 4 ;;
  *"is-enabled hermes-demo-pm-consumer.service"*) echo not-found; exit 4 ;;
esac
exit 1
""",
        encoding="utf-8",
    )

    result = host.run()

    assert result.returncode == 0, result.stderr
    _assert_no_per_agent_gateway(host)
    assert host.service_state()["gateway"] == "deferred"
    assert not host.marker.exists()
    assert "host gateway (hermes-gateway.service) was not checked" in result.stderr


def test_summary_reports_the_host_gateway_not_a_per_agent_unit(tmp_path: Path) -> None:
    host = Host(tmp_path)
    role_yaml = host.role / "role.yaml"
    role_yaml.write_text(
        role_yaml.read_text(encoding="utf-8").replace('gateway: "pending"', 'gateway: "active"'),
        encoding="utf-8",
    )
    host.probe("ok|serving|9100")
    serving = host.run("99-summary.sh")
    assert serving.returncode == 0, serving.stderr
    assert "Gateway:        active  (hermes-gateway.service)" in serving.stderr
    assert "Mode:           OPERATIONAL" in serving.stderr
    assert AGENT_UNIT not in serving.stderr
    assert "journalctl --user -fu hermes-gateway.service" in serving.stderr
    assert "rescan=0" in host.probe_log.read_text(encoding="utf-8")  # read-only: no rescan

    host.probe("ok|not-serving|9100")
    stale = host.run("99-summary.sh")
    assert stale.returncode == 0, stale.stderr
    assert "Gateway:        error (stale active claim)" in stale.stderr
    assert "Mode:           INCOMPLETE" in stale.stderr


@pytest.mark.parametrize("name", ["70-systemd.sh", "99-summary.sh"])
def test_scripts_never_template_a_per_agent_gateway_unit(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    for fragment in ("[Service]", "ExecStart=", "enable --now \"$GW_UNIT\"", "WantedBy="):
        assert fragment not in text, f"{name} still renders or enables a per-agent unit: {fragment}"


def test_fleet_sync_never_restarts_a_unit_and_points_at_the_guarded_wrapper() -> None:
    text = (ROOT / "scripts" / "fleet-sync.sh").read_text(encoding="utf-8")
    for fragment in ("try-restart", "RESTART_UNITS", 'systemd.get("gateway_unit")', "systemctl --user restart"):
        assert fragment not in text, f"fleet-sync.sh still reaches for a raw restart: {fragment}"
    assert "hermes-gateway-restart --reason" in text


@pytest.mark.parametrize("script", ["retrofit-70-envfile.sh", "hermes-runtime-dropin.py"])
def test_per_agent_unit_rewriters_are_retired_and_refuse(script: str) -> None:
    path = ROOT / "scripts" / script
    cmd = ["bash", str(path)] if script.endswith(".sh") else [sys.executable, str(path)]
    result = subprocess.run(cmd + ["--apply"], text=True, capture_output=True, check=False)
    assert result.returncode == 64
    assert "RETIRED" in result.stderr
    assert "hermes-gateway-restart" in result.stderr
