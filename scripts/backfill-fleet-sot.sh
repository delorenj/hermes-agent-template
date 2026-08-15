#!/usr/bin/env bash
set -euo pipefail

# Distributable config — same source of truth the template's _lib.sh reads.
HERMES_TEMPLATE_CONFIG="${HERMES_TEMPLATE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template/config.toml}"
cfg() {  # cfg <dotted.key> <default>  — read a value from config.toml (always exits 0)
  local key="$1" def="$2"
  { [[ -f "$HERMES_TEMPLATE_CONFIG" ]] && command -v python3 >/dev/null 2>&1; } || { printf '%s' "$def"; return 0; }
  python3 - "$HERMES_TEMPLATE_CONFIG" "$key" "$def" <<'PYEOF' || printf '%s' "$def"
import sys, os
try:
    import tomllib
except ModuleNotFoundError:
    print(sys.argv[3], end=""); sys.exit(0)
try:
    cur = tomllib.load(open(sys.argv[1], "rb"))
except Exception:
    print(sys.argv[3], end=""); sys.exit(0)
for p in sys.argv[2].split("."):
    if isinstance(cur, dict) and p in cur:
        cur = cur[p]
    else:
        print(sys.argv[3], end=""); sys.exit(0)
print(os.path.expanduser(str(cur)), end="")
PYEOF
  return 0
}

FLEET_ENV="${HERMES_FLEET_ENV:-$(cfg fleet.fleet_env "$HOME/.hermes/fleet.env")}"
REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
DEFAULT_BIN="${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes")}"
DEFAULT_REPO="${HERMES_FLEET_REPO:-$(cfg fleet.hermes_repo "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1")}"
DEFAULT_OAUTH_FILE="${HERMES_FLEET_OAUTH_FILE:-$(cfg fleet.oauth_file "$HOME/.hermes/auth.json")}"
DEFAULT_CODEX_HOME="${HERMES_FLEET_CODEX_HOME:-$(cfg fleet.codex_home "$HOME/.codex")}"
SCAFFOLD_SRC="$(cd "$(dirname "$0")/../template/.runtime-scaffold" && pwd)"

mkdir -p "$(dirname "$FLEET_ENV")"
if [[ ! -f "$FLEET_ENV" ]]; then
  cat > "$FLEET_ENV" <<EOF
# Hermes fleet source of truth.
HERMES_FLEET_BIN=${DEFAULT_BIN}
HERMES_FLEET_REPO=${DEFAULT_REPO}
HERMES_FLEET_REGISTRY_FILE=${REGISTRY_FILE}
HERMES_FLEET_OAUTH_FILE=${DEFAULT_OAUTH_FILE}
HERMES_FLEET_CODEX_HOME=${DEFAULT_CODEX_HOME}
EOF
  chmod 600 "$FLEET_ENV"
fi

upsert_fleet_env() {
  local key="$1" value="$2"
  python3 - "$FLEET_ENV" "$key" "$value" <<'PYEOF'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
line = f"{key}={value}"
lines = path.read_text().splitlines() if path.exists() else []
for idx, existing in enumerate(lines):
    if existing.startswith(f"{key}="):
        lines[idx] = line
        break
else:
    lines.append(line)
path.write_text("\n".join(lines) + "\n")
PYEOF
}

upsert_fleet_env HERMES_FLEET_OAUTH_FILE "$DEFAULT_OAUTH_FILE"
upsert_fleet_env HERMES_FLEET_CODEX_HOME "$DEFAULT_CODEX_HOME"
chmod 600 "$FLEET_ENV"

# shellcheck disable=SC1090
source "$FLEET_ENV"

HERMES_FLEET_BIN="${HERMES_FLEET_BIN:-$DEFAULT_BIN}"
HERMES_FLEET_REPO="${HERMES_FLEET_REPO:-$DEFAULT_REPO}"
HERMES_FLEET_OAUTH_FILE="${HERMES_FLEET_OAUTH_FILE:-$DEFAULT_OAUTH_FILE}"
HERMES_FLEET_CODEX_HOME="${HERMES_FLEET_CODEX_HOME:-$DEFAULT_CODEX_HOME}"

python3 - "$REGISTRY_FILE" "$HERMES_FLEET_BIN" "$HERMES_FLEET_REPO" "$FLEET_ENV" "$SCAFFOLD_SRC" "$HERMES_FLEET_OAUTH_FILE" "$HERMES_FLEET_CODEX_HOME" <<'PYEOF'
import os
import stat
import sys
import shutil
import pathlib

try:
    import yaml  # type: ignore
except ImportError:
    raise SystemExit("PyYAML required (pip install pyyaml)")

registry_path = pathlib.Path(sys.argv[1])
hermes_bin = sys.argv[2]
hermes_repo = sys.argv[3]
fleet_env = sys.argv[4]
scaffold_src = pathlib.Path(sys.argv[5])
oauth_file = sys.argv[6]
codex_home = sys.argv[7]

if not registry_path.exists():
    raise SystemExit(f"Registry not found: {registry_path}")

data = yaml.safe_load(registry_path.read_text()) or {}
agents = data.get("agents") or {}

wrapper_count = 0
meta_count = 0
scaffold_count = 0
lib_count = 0
systemd_count = 0


def patch_systemd_unit(unit_path: pathlib.Path) -> bool:
    if not unit_path.exists():
        return False
    original = unit_path.read_text()
    lines = original.splitlines()
    rendered = []
    for line in lines:
        if line.startswith("Environment=HERMES_OAUTH_FILE="):
            continue
        if line.startswith("Environment=CODEX_HOME="):
            continue
        rendered.append(line)
        if line.startswith("Environment=HERMES_HOME="):
            rendered.append(f"Environment=HERMES_OAUTH_FILE={oauth_file}")
            rendered.append(f"Environment=CODEX_HOME={codex_home}")
    new_text = "\n".join(rendered) + ("\n" if original.endswith("\n") else "")
    if new_text == original:
        return False
    unit_path.write_text(new_text)
    return True

for agent_id, cfg in agents.items():
    cfg["hermes"] = {
        "bin": hermes_bin,
        "repo": hermes_repo,
        "fleet_env": fleet_env,
        "oauth_file": oauth_file,
        "codex_home": codex_home,
    }
    meta_count += 1

    systemd_dir = pathlib.Path.home() / ".config" / "systemd" / "user"
    for suffix in ("gateway", "consumer"):
        unit = systemd_dir / f"hermes-{agent_id}-{suffix}.service"
        if patch_systemd_unit(unit):
            systemd_count += 1

    role_dir = cfg.get("role_dir")
    if not role_dir:
        continue

    role_path = pathlib.Path(role_dir)
    if not role_path.exists():
        continue

    project_path = pathlib.Path(cfg.get("project_path") or "")
    role_name = cfg.get("role") or "pm"
    alt_role_path = project_path / "_agents" / "hermes" / role_name
    if not (role_path / "hermes").exists() and (alt_role_path / "hermes").exists():
        role_path = alt_role_path
        cfg["role_dir"] = str(alt_role_path)

    target_scaffold = role_path / ".runtime-scaffold"
    shutil.copytree(scaffold_src, target_scaffold, dirs_exist_ok=True)
    scaffold_count += 1

    lib_path = role_path / ".scripts" / "_lib.sh"
    if lib_path.exists():
        lib_text = lib_path.read_text()
        old_block = (
            "# Tools we expect on the host\n"
            "HERMES_BIN=\"${HERMES_BIN:-/home/delorenj/code/hermes-agent/.venv/bin/hermes}\"\n"
            "HERMES_AGENT_REPO=\"${HERMES_AGENT_REPO:-/home/delorenj/code/hermes-agent}\"\n"
            "RUNTIME_SCAFFOLD_DIR=\"${RUNTIME_SCAFFOLD_DIR:-/home/delorenj/code/hermes-agent-template/runtime-scaffold}\"\n"
            "REGISTRY_FILE=\"${REGISTRY_FILE:-$HOME/.hermes/agents-registry.yaml}\"\n"
        )
        new_block = (
            "# Fleet source-of-truth (shared across all wrappers/provisioners)\n"
            "FLEET_ENV=\"${HERMES_FLEET_ENV:-$HOME/.hermes/fleet.env}\"\n"
            "if [[ -f \"$FLEET_ENV\" ]]; then\n"
            "  # shellcheck disable=SC1090\n"
            "  source \"$FLEET_ENV\"\n"
            "fi\n\n"
            "# Tools we expect on the host\n"
            "HERMES_BIN=\"${HERMES_BIN:-${HERMES_FLEET_BIN:-$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes}}\"\n"
            "HERMES_AGENT_REPO=\"${HERMES_AGENT_REPO:-${HERMES_FLEET_REPO:-$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1}}\"\n"
            "HERMES_OAUTH_FILE=\"${HERMES_OAUTH_FILE:-${HERMES_FLEET_OAUTH_FILE:-$HOME/.hermes/auth.json}}\"\n"
            "CODEX_HOME=\"${CODEX_HOME:-${HERMES_FLEET_CODEX_HOME:-$HOME/.codex}}\"\n"
            "# Prefer a scaffold vendored into this agent directory; fall back to legacy template path.\n"
            "RUNTIME_SCAFFOLD_DIR=\"${RUNTIME_SCAFFOLD_DIR:-$ROLE_DIR/.runtime-scaffold}\"\n"
            "if [[ ! -d \"$RUNTIME_SCAFFOLD_DIR\" ]]; then\n"
            "  RUNTIME_SCAFFOLD_DIR=\"${HERMES_TEMPLATE_RUNTIME_SCAFFOLD:-$HOME/code/hermes-agent-template/runtime-scaffold}\"\n"
            "fi\n"
            "REGISTRY_FILE=\"${REGISTRY_FILE:-${HERMES_FLEET_REGISTRY_FILE:-$HOME/.hermes/agents-registry.yaml}}\"\n"
        )
        if old_block in lib_text:
            lib_text = lib_text.replace(old_block, new_block)
        if "HERMES_OAUTH_FILE=" not in lib_text:
            lib_text = lib_text.replace(
                "HERMES_AGENT_REPO=\"${HERMES_AGENT_REPO:-${HERMES_FLEET_REPO:-$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1}}\"\n",
                "HERMES_AGENT_REPO=\"${HERMES_AGENT_REPO:-${HERMES_FLEET_REPO:-$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1}}\"\n"
                "HERMES_OAUTH_FILE=\"${HERMES_OAUTH_FILE:-${HERMES_FLEET_OAUTH_FILE:-$HOME/.hermes/auth.json}}\"\n"
                "CODEX_HOME=\"${CODEX_HOME:-${HERMES_FLEET_CODEX_HOME:-$HOME/.codex}}\"\n",
            )
        lib_text = lib_text.replace(
            "export HERMES_BIN HERMES_AGENT_REPO RUNTIME_SCAFFOLD_DIR REGISTRY_FILE \\\n       BLOODBANK_NATS_HOST BLOODBANK_NATS_PORT \\\n       PLANE_BASE PLANE_API_KEY \\\n       CF_API CF_ZONE_DELO_SH CF_ACCOUNT_ID\n",
            "export FLEET_ENV HERMES_BIN HERMES_AGENT_REPO RUNTIME_SCAFFOLD_DIR REGISTRY_FILE \\\n       BLOODBANK_NATS_HOST BLOODBANK_NATS_PORT \\\n       PLANE_BASE PLANE_API_KEY \\\n       CF_API CF_ZONE_DELO_SH CF_ACCOUNT_ID\n",
        )
        lib_text = lib_text.replace(
            "export FLEET_ENV HERMES_BIN HERMES_AGENT_REPO RUNTIME_SCAFFOLD_DIR REGISTRY_FILE",
            "export FLEET_ENV HERMES_BIN HERMES_AGENT_REPO HERMES_OAUTH_FILE CODEX_HOME RUNTIME_SCAFFOLD_DIR REGISTRY_FILE",
        )
        if lib_text != lib_path.read_text():
            lib_path.write_text(lib_text)
            lib_count += 1

    wrapper = role_path / "hermes"
    if not wrapper.exists():
        continue

    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"# Launcher for {agent_id}. Resolves HERMES_HOME to runtime and execs shared fleet Hermes.\n"
        "set -euo pipefail\n\n"
        "ROLE_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
        "HERMES_HOME=\"$ROLE_DIR/runtime\"\n\n"
        "FLEET_ENV=\"${HERMES_FLEET_ENV:-$HOME/.hermes/fleet.env}\"\n"
        "if [[ -f \"$FLEET_ENV\" ]]; then\n"
        "  # shellcheck disable=SC1090\n"
        "  source \"$FLEET_ENV\"\n"
        "fi\n\n"
        "HERMES_BIN=\"${HERMES_BIN:-${HERMES_FLEET_BIN:-$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes}}\"\n\n"
        "HERMES_OAUTH_FILE=\"${HERMES_OAUTH_FILE:-${HERMES_FLEET_OAUTH_FILE:-$HOME/.hermes/auth.json}}\"\n"
        "CODEX_HOME=\"${CODEX_HOME:-${HERMES_FLEET_CODEX_HOME:-$HOME/.codex}}\"\n\n"
        "if [[ ! -d \"$HERMES_HOME\" ]]; then\n"
        "  echo \"hermes: runtime submodule not initialized at $HERMES_HOME\" >&2\n"
        "  echo \"  fix: git submodule update --init --recursive\" >&2\n"
        "  exit 1\n"
        "fi\n\n"
        "if [[ ! -x \"$HERMES_BIN\" ]]; then\n"
        "  echo \"hermes: binary not executable at $HERMES_BIN\" >&2\n"
        "  echo \"  set HERMES_BIN or HERMES_FLEET_BIN (in $FLEET_ENV) to the shared Hermes binary.\" >&2\n"
        "  exit 1\n"
        "fi\n\n"
        "exec env HERMES_HOME=\"$HERMES_HOME\" HERMES_FLEET_ENV=\"$FLEET_ENV\" "
        "HERMES_OAUTH_FILE=\"$HERMES_OAUTH_FILE\" CODEX_HOME=\"$CODEX_HOME\" \"$HERMES_BIN\" \"$@\"\n"
    )

    mode = wrapper.stat().st_mode
    wrapper.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    wrapper_count += 1

registry_path.write_text(yaml.safe_dump(data, sort_keys=False))
print(f"updated wrappers: {wrapper_count}")
print(f"updated registry entries: {meta_count}")
print(f"copied role-local scaffolds: {scaffold_count}")
print(f"patched role _lib.sh files: {lib_count}")
print(f"patched systemd units: {systemd_count}")
print(f"registry: {registry_path}")
print(f"fleet env: {fleet_env}")
print(f"shared bin: {hermes_bin}")
print(f"shared repo: {hermes_repo}")
print(f"shared oauth file: {oauth_file}")
print(f"shared codex home: {codex_home}")
PYEOF
