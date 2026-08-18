#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_ENV_LIBRARY_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/fleet-env.sh"
FLEET_ENV_PARSER_SOURCE="$SCRIPT_DIR/../template/.scripts/lib/parse-fleet-env.py"
ROLE_LIBRARY_SOURCE="$SCRIPT_DIR/../template/.scripts/_lib.sh"
WRAPPER_TEMPLATE="$SCRIPT_DIR/../template/hermes.jinja"
for trusted_asset in \
  "$FLEET_ENV_LIBRARY_SOURCE" \
  "$FLEET_ENV_PARSER_SOURCE" \
  "$ROLE_LIBRARY_SOURCE" \
  "$WRAPPER_TEMPLATE"
do
  if [[ ! -f "$trusted_asset" || -L "$trusted_asset" ]]; then
    echo "backfill-fleet-sot: trusted template asset is unavailable" >&2
    exit 2
  fi
done
# shellcheck source=../template/.scripts/lib/fleet-env.sh
builtin source "$FLEET_ENV_LIBRARY_SOURCE"
scrub_subprocess_interpreter_injection

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
load_fleet_environment "$FLEET_ENV" "$FLEET_ENV_PARSER_SOURCE"

REGISTRY_FILE="${HERMES_FLEET_REGISTRY_FILE:-$(cfg fleet.registry_file "$HOME/.hermes/agents-registry.yaml")}"
HERMES_FLEET_BIN="${HERMES_FLEET_BIN:-$(cfg fleet.hermes_bin "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1/.venv/bin/hermes")}"
HERMES_FLEET_REPO="${HERMES_FLEET_REPO:-$(cfg fleet.hermes_repo "$HOME/.local/share/hermes-agent/releases/0408fec7a153e6c32c064acd2b8053917f1525f1")}"
HERMES_FLEET_OAUTH_FILE="${HERMES_FLEET_OAUTH_FILE:-$(cfg fleet.oauth_file "$HOME/.hermes/auth.json")}"
HERMES_FLEET_CODEX_HOME="${HERMES_FLEET_CODEX_HOME:-$(cfg fleet.codex_home "$HOME/.codex")}"
SCAFFOLD_SRC="$(cd "$(dirname "$0")/../template/.runtime-scaffold" && pwd)"

mkdir -p "$(dirname "$FLEET_ENV")"
upsert_fleet_env() {
  local key="$1" value="$2"
  python3 -I "$FLEET_ENV_PARSER_SOURCE" --upsert "$FLEET_ENV" "$key" "$value"
}

new_fleet_env=0
if [[ ! -e "$FLEET_ENV" && ! -L "$FLEET_ENV" ]]; then
  new_fleet_env=1
  upsert_fleet_env HERMES_FLEET_BIN "$HERMES_FLEET_BIN"
  upsert_fleet_env HERMES_FLEET_REPO "$HERMES_FLEET_REPO"
  upsert_fleet_env HERMES_FLEET_REGISTRY_FILE "$REGISTRY_FILE"
fi
upsert_fleet_env HERMES_FLEET_OAUTH_FILE "$HERMES_FLEET_OAUTH_FILE"
upsert_fleet_env HERMES_FLEET_CODEX_HOME "$HERMES_FLEET_CODEX_HOME"

python3 - "$REGISTRY_FILE" "$HERMES_FLEET_BIN" "$HERMES_FLEET_REPO" "$FLEET_ENV" \
  "$SCAFFOLD_SRC" "$HERMES_FLEET_OAUTH_FILE" "$HERMES_FLEET_CODEX_HOME" \
  "$FLEET_ENV_LIBRARY_SOURCE" "$FLEET_ENV_PARSER_SOURCE" \
  "$ROLE_LIBRARY_SOURCE" "$WRAPPER_TEMPLATE" <<'PYEOF'
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
fleet_library_source = pathlib.Path(sys.argv[8])
fleet_parser_source = pathlib.Path(sys.argv[9])
role_library_source = pathlib.Path(sys.argv[10])
wrapper_template = pathlib.Path(sys.argv[11]).read_text()

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

    scripts_dir = role_path / ".scripts"
    role_lib_dir = scripts_dir / "lib"
    if scripts_dir.is_symlink() or role_lib_dir.is_symlink():
        raise SystemExit(f"refusing symlinked role script directory: {role_path}")
    role_lib_dir.mkdir(parents=True, exist_ok=True)
    for source, destination in (
        (fleet_library_source, role_lib_dir / "fleet-env.sh"),
        (fleet_parser_source, role_lib_dir / "parse-fleet-env.py"),
        (role_library_source, scripts_dir / "_lib.sh"),
    ):
        if destination.is_symlink():
            raise SystemExit(f"refusing symlinked role template asset: {destination}")
        shutil.copy2(source, destination)
    lib_count += 1

    wrapper = role_path / "hermes"
    if wrapper.is_symlink():
        raise SystemExit(f"refusing symlinked Hermes launcher: {wrapper}")
    profile_name = str(cfg.get("profile_name") or agent_id)
    wrapper_text = wrapper_template.replace("{{ agent_id }}", agent_id)
    wrapper_text = wrapper_text.replace(
        f'PROFILE_NAME="${{HERMES_PROFILE_NAME:-{agent_id}}}"',
        f'PROFILE_NAME="${{HERMES_PROFILE_NAME:-{profile_name}}}"',
    )
    wrapper.write_text(wrapper_text)

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
