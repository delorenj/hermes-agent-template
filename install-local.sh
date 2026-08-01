#!/usr/bin/env sh
# Least-friction local install of the Hermes PM role into a project.
#
# Local-only: no GitHub runtime repo, no Telegram, no NATS/BloodBank, no Plane
# project creation. It binds the PM to a ticket board you already have. The PM
# runs the continuous ticket sentinel out-of-band on its heartbeat timer
# (board-reconciliation pass + gated runtime checkpoint, one tick).
# Works on macOS (launchd) and Linux (systemd).
#
# One-liner (from anywhere inside the target project):
#   curl -fsSL https://raw.githubusercontent.com/delorenj/hermes-agent-template/main/install-local.sh | sh
#
# Or from a checkout:
#   sh /path/to/hermes-agent-template/install-local.sh
#
# Environment overrides (skip the prompts):
#   HAT_REPO=<name>            project/repo name (default: basename of CWD)
#   HAT_PROVIDER=linear|plane|trello   (default: plane)
#   HAT_ROLES="pm"                     roles to install (default: pm)
#   HAT_DRY_RUN=1              print actions, change nothing
#   Provider creds: LINEAR_API_KEY | PLANE_API_KEY+PLANE_BASE | TRELLO_KEY+TRELLO_TOKEN
set -eu

# Fleet runtime publication. These values intentionally identify the reviewed
# fork commit; clean installs must never fall back to NousResearch/main.
HERMES_RUNTIME_GIT_URL="https://github.com/delorenj/hermes-agent.git"
HERMES_RUNTIME_GIT_REF="feature/PJAN-19-routing-publication"
HERMES_RUNTIME_GIT_SHA="113e1b182b6d72a7dd02a191f134a41668ceaf0e"

say()  { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [ "${HAT_DRY_RUN:-0}" = "1" ]; then printf '  [dry-run] %s\n' "$*"; else eval "$*"; fi; }
ask()  { # ask VAR "prompt" "default"
  eval "_cur=\${$1:-}"; [ -n "${_cur:-}" ] && return 0
  printf '%s [%s]: ' "$2" "${3:-}" >&2; read -r _ans || _ans=""
  eval "$1=\"\${_ans:-$3}\""
}

OS="$(uname -s)"
PROJECT_DIR="$(pwd)"
[ -d "$PROJECT_DIR/.git" ] || warn "Note: $PROJECT_DIR is not a git repo root; the role will install here anyway."

# --- Resolve the template source (local checkout wins, else GitHub) ----------
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/copier.yml" ]; then
  TEMPLATE_SRC="$SCRIPT_DIR"
else
  TEMPLATE_SRC="${HAT_TEMPLATE:-gh:delorenj/hermes-agent-template}"
fi

say "== Hermes local install =="
say "   project:  $PROJECT_DIR"
say "   template: $TEMPLATE_SRC"
say "   os:       $OS"

# --- 1. Ensure the hermes CLI ------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  say "1. hermes: found ($(command -v hermes))"
else
  say "1. hermes: not found — installing"
  HERMES_INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent}"
  if [ "${HAT_DRY_RUN:-0}" = "1" ]; then
    say "  [dry-run] clone $HERMES_RUNTIME_GIT_URL@$HERMES_RUNTIME_GIT_REF"
    say "  [dry-run] verify and install commit $HERMES_RUNTIME_GIT_SHA"
  else
    command -v git >/dev/null 2>&1 || die "git is required to install the pinned Hermes runtime"
    if [ -d "$HERMES_INSTALL_DIR/.git" ]; then
      _origin="$(git -C "$HERMES_INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
      [ "$_origin" = "$HERMES_RUNTIME_GIT_URL" ] \
        || die "existing Hermes checkout origin is not the reviewed fleet fork: $HERMES_INSTALL_DIR"
      git -C "$HERMES_INSTALL_DIR" fetch origin "$HERMES_RUNTIME_GIT_REF"
    elif [ -e "$HERMES_INSTALL_DIR" ]; then
      die "Hermes install path exists but is not the reviewed fork checkout: $HERMES_INSTALL_DIR"
    else
      mkdir -p "$(dirname "$HERMES_INSTALL_DIR")"
      git clone --branch "$HERMES_RUNTIME_GIT_REF" --single-branch \
        "$HERMES_RUNTIME_GIT_URL" "$HERMES_INSTALL_DIR"
    fi
    git -C "$HERMES_INSTALL_DIR" cat-file -e "$HERMES_RUNTIME_GIT_SHA^{commit}" \
      || die "pinned Hermes commit is unavailable from the reviewed fork"
    git -C "$HERMES_INSTALL_DIR" merge-base --is-ancestor \
      "$HERMES_RUNTIME_GIT_SHA" "origin/$HERMES_RUNTIME_GIT_REF" \
      || die "pinned Hermes commit is not on the reviewed publication ref"
    bash "$HERMES_INSTALL_DIR/scripts/install.sh" \
      --dir "$HERMES_INSTALL_DIR" \
      --branch "$HERMES_RUNTIME_GIT_REF" \
      --commit "$HERMES_RUNTIME_GIT_SHA" \
      --skip-setup
    _installed_sha="$(git -C "$HERMES_INSTALL_DIR" rev-parse HEAD)"
    [ "$_installed_sha" = "$HERMES_RUNTIME_GIT_SHA" ] \
      || die "Hermes installer did not retain pinned commit $HERMES_RUNTIME_GIT_SHA"
  fi
  command -v hermes >/dev/null 2>&1 || die "hermes install did not put 'hermes' on PATH. Open a new shell and re-run."
fi
HERMES_BIN="$(command -v hermes)"

# --- 2. Ensure copier --------------------------------------------------------
if command -v copier >/dev/null 2>&1; then
  say "2. copier: found"
else
  say "2. copier: not found — installing"
  if command -v uv >/dev/null 2>&1; then run "uv tool install copier"
  else run "python3 -m pip install --user copier"; fi
  command -v copier >/dev/null 2>&1 || die "copier install failed. Install it (uv tool install copier) and re-run."
fi

# --- 3. Host-correct, local config.toml (so 01-config doesn't seed delo defaults)
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-agent-template"
CFG="$CFG_DIR/config.toml"
if [ -f "$CFG" ]; then
  say "3. config: exists ($CFG) — leaving it"
else
  say "3. config: writing local defaults to $CFG"
  if [ "${HAT_DRY_RUN:-0}" != "1" ]; then
    mkdir -p "$CFG_DIR"
    cat > "$CFG" <<TOML
# Local install — cloud fields intentionally blank.
[fleet]
hermes_bin = "$HERMES_BIN"
hermes_repo = "$HOME/.hermes/hermes-agent"
hermes_git_url = "$HERMES_RUNTIME_GIT_URL"
hermes_git_ref = "$HERMES_RUNTIME_GIT_REF"
hermes_git_sha = "$HERMES_RUNTIME_GIT_SHA"
fleet_env = "~/.hermes/fleet.env"
registry_file = "~/.hermes/agents-registry.yaml"

[github]
runtime_repo_owner = ""

[plane]
base = "${PLANE_BASE:-}"
workspace = ""

TOML
  fi
fi

# --- 4. Provider credentials + board binding ---------------------------------
ask HAT_PROVIDER "Ticket provider (linear|plane|trello)" "plane"
PROVIDER="$HAT_PROVIDER"
ask HAT_REPO "Project/repo name" "$(basename "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]')"
REPO="$HAT_REPO"
PM_ENV="$HOME/.hermes/${REPO}-pm.env"

# These get written into the pm role.yaml binding after render.
# Pre-seed from optional env knobs so the install can run non-interactively.
TP_WORKSPACE="${HAT_PLANE_WORKSPACE:-}"; TP_PROJECT="${HAT_PLANE_PROJECT:-}"
TP_TEAM="${HAT_LINEAR_TEAM:-}"; TP_BOARD="${HAT_TRELLO_BOARD:-}"
case "$PROVIDER" in
  plane)
    ask PLANE_BASE "Plane base URL" "https://app.plane.so"
    : "${PLANE_API_KEY:?Set PLANE_API_KEY in your environment, then re-run}"
    ask TP_WORKSPACE "Plane workspace slug" ""
    [ -n "$TP_WORKSPACE" ] || die "workspace is required"
    say "   Plane projects in '$TP_WORKSPACE':"
    _pj="$(mktemp)"
    curl -fsS "$PLANE_BASE/api/v1/workspaces/$TP_WORKSPACE/projects/?per_page=100" \
      -H "X-API-Key: $PLANE_API_KEY" > "$_pj" 2>/dev/null || true
    python3 - "$_pj" <<'PY' 2>/dev/null || warn "   (could not list projects; check PLANE_API_KEY/base/workspace)"
import sys, json
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
rows = d.get("results", d if isinstance(d, list) else [])
for p in rows:
    print(f"     {p.get('id')}  {(p.get('identifier') or ''):8} {p.get('name')}")
PY
    rm -f "$_pj"
    ask TP_PROJECT "Plane project UUID to manage" ""
    [ -n "$TP_PROJECT" ] || die "project UUID is required"
    CRED_LINES="PLANE_API_KEY=$PLANE_API_KEY
PLANE_BASE=$PLANE_BASE"
    ;;
  linear)
    : "${LINEAR_API_KEY:?Set LINEAR_API_KEY in your environment, then re-run}"
    ask TP_TEAM "Linear team key (for example DEL)" ""
    [ -n "$TP_TEAM" ] || die "team key is required"
    CRED_LINES="LINEAR_API_KEY=$LINEAR_API_KEY"
    ;;
  trello)
    : "${TRELLO_KEY:?Set TRELLO_KEY in your environment, then re-run}"
    : "${TRELLO_TOKEN:?Set TRELLO_TOKEN in your environment, then re-run}"
    ask TP_BOARD "Trello board id" ""
    [ -n "$TP_BOARD" ] || die "board id is required"
    CRED_LINES="TRELLO_KEY=$TRELLO_KEY
TRELLO_TOKEN=$TRELLO_TOKEN"
    ;;
  *) die "unknown provider: $PROVIDER" ;;
esac

say "4. writing provider credentials to $PM_ENV"
if [ "${HAT_DRY_RUN:-0}" != "1" ]; then
  mkdir -p "$HOME/.hermes"; umask 077
  printf '%s\n' "$CRED_LINES" > "$PM_ENV"
fi

# --- 5. Provision each role with copier (cloud + Telegram + systemd skipped) --
# No provider key in copier's env, so 42-ticket-provider skips board creation;
# we bind to the existing board below.
export SKIP_TELEGRAM=1 SKIP_EMAIL=1 SKIP_RUNTIME_REPO=1 SKIP_PLANE=1 \
       SKIP_BLOODBANK=1 SKIP_SYSTEMD=1
ROLES="${HAT_ROLES:-pm}"
for ROLE in $ROLES; do
  say "5. provisioning role: $ROLE"
  DEST="$PROJECT_DIR/agents/hermes/$ROLE"
  # Scrub provider creds from copier's environment so 42-ticket-provider skips
  # board CREATION; we bind to the existing board in step 6 instead.
  run "env -u PLANE_API_KEY -u PLANE_33GOD_API_KEY -u LINEAR_API_KEY \
        -u TRELLO_KEY -u TRELLO_TOKEN \
        copier copy '$TEMPLATE_SRC' '$DEST' --trust --defaults --overwrite \
        --data target_repo='$REPO' --data role='$ROLE' --data ticket_provider='$PROVIDER'"
done

# --- 6. Bind the PM to the existing board ------------------------------------
PM_ROLE="$PROJECT_DIR/agents/hermes/pm/role.yaml"
if [ -f "$PM_ROLE" ] && [ "${HAT_DRY_RUN:-0}" != "1" ]; then
  say "6. binding pm to your $PROVIDER board"
  TP_WORKSPACE="$TP_WORKSPACE" TP_PROJECT="$TP_PROJECT" TP_TEAM="$TP_TEAM" \
  TP_BOARD="$TP_BOARD" PROVIDER="$PROVIDER" python3 - "$PM_ROLE" <<'PY'
import os, re, sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
def setleaf(text, key, val):
    if not val: return text
    new, n = re.subn(rf'(?m)^(\s*{re.escape(key)}:\s*)"?[^"\n]*"?\s*$', rf'\g<1>"{val}"', text, count=1)
    return new if n else text
t = setleaf(t, "workspace", os.environ.get("TP_WORKSPACE",""))
t = setleaf(t, "project",   os.environ.get("TP_PROJECT",""))
t = setleaf(t, "team",      os.environ.get("TP_TEAM",""))
t = setleaf(t, "board",     os.environ.get("TP_BOARD",""))
p.write_text(t)
print("   bound:", {k:v for k,v in os.environ.items() if k.startswith("TP_") and v})
PY
fi

# --- 7. Smoke test the board connection --------------------------------------
if [ "${HAT_DRY_RUN:-0}" != "1" ] && [ -f "$PM_ROLE" ]; then
  say "7. smoke test: reading your board through the adapter"
  LIB="$PROJECT_DIR/agents/hermes/pm/.scripts/lib/ticket-provider.sh"
  ( set -a; . "$PM_ENV" 2>/dev/null; set +a
    bash -c '. "$1"; tp resolve && echo "   issues: $(tp list_issues | python3 -c "import sys,json;print(len(json.load(sys.stdin)))")"' _ "$LIB" ) \
    || warn "   smoke test failed — check the binding in $PM_ROLE and creds in $PM_ENV"
fi

say ""
say "Done. Talk to the PM:   agents/hermes/pm/hermes chat \"status\""
if [ "$OS" = "Darwin" ]; then
  say "Sentinel (launchd):     launchctl list | grep $REPO-pm-heartbeat"
else
  say "Sentinel (systemd):     systemctl --user status hermes-$REPO-pm-heartbeat.timer"
fi
say "Provider creds live in: $PM_ENV"
