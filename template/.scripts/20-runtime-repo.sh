#!/usr/bin/env bash
# Create the per-agent runtime GitHub repo, init from scaffold, submodule it.
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
load_role_env

already_done 20-runtime-repo && { log "[20] runtime repo already set up — skipping"; exit 0; }
[[ "${SKIP_RUNTIME_REPO:-0}" == "1" ]] && { log "[20] runtime repo — SKIPPED (SKIP_RUNTIME_REPO=1)"; mark_done 20-runtime-repo; exit 0; }

PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
RUNTIME_LOCAL="$ROLE_DIR/runtime"
PROJECT_PATH="$(project_repo_path)" || die "no project git root"
GH_OWNER="${RUNTIME_REPO%%/*}"
GH_NAME="${RUNTIME_REPO##*/}"

log "[20] runtime repo: gh:$RUNTIME_REPO"

# 1. Create the GitHub repo (private) if it doesn't exist
if gh repo view "$RUNTIME_REPO" >/dev/null 2>&1; then
  log "    GH repo exists; reusing"
else
  log "    creating GH repo (private)"
  gh repo create "$RUNTIME_REPO" --private \
    --description "Hermes runtime (HERMES_HOME) for $AGENT_ID — auto-checkpointed memory + state" \
    --disable-issues --disable-wiki >/dev/null
fi

REMOTE_URL=$(gh repo view "$RUNTIME_REPO" --json sshUrl -q .sshUrl)

# 2. Check if remote already has commits — if so, skip the scaffold push.
#    This makes the step idempotent across failed-run retries.
if git ls-remote --heads "$REMOTE_URL" 2>/dev/null | grep -q refs/heads; then
  log "    remote already has commits — skipping scaffold push"
  REMOTE_HAS_CONTENT=1
else
  REMOTE_HAS_CONTENT=0
fi

# 2a. Stage the runtime scaffold into a tmp dir (only if remote is empty)
TMP=$(mktemp -d)
log "    populating scaffold in $TMP"
cp -a "$RUNTIME_SCAFFOLD_DIR/." "$TMP/"

# Render scaffold templates with role-specific values
python3 - "$TMP" "$AGENT_ID" "$REPO" "$ROLE" "$DISPLAY_NAME" <<'PYEOF'
import sys, pathlib, re
root, agent_id, repo, role, display = sys.argv[1:6]
root = pathlib.Path(root)
mapping = {
    "{{agent_id}}": agent_id, "{{repo}}": repo, "{{role}}": role,
    "{{display_name}}": display,
}
for p in root.rglob("*"):
    if p.is_file() and p.suffix in (".md", ".yaml", ".yml", ".sh", ".py", ".gitignore", ".gitattributes"):
        try:
            t = p.read_text()
            for k, v in mapping.items(): t = t.replace(k, v)
            p.write_text(t)
        except UnicodeDecodeError:
            pass
PYEOF

# 3. Seed the runtime config from the canonical PM config — the fleet's single
#    config source of truth (defaults to the global ~/.hermes/config.yaml). This
#    is a provision-time snapshot; Hermes loads $HERMES_HOME/config.yaml directly
#    (there is no live profile inheritance). Override via config.toml
#    [fleet].canonical_pm_config to share one curated PM config across all repos.
if [[ "$ROLE" == "reporter" ]]; then
  # Generate a delta-only runtime config for least-privilege reporters. Never copy the shared PM
  # config: it may contain dashboard credentials, write-capable MCPs, or broad tools.
  MODEL_PROVIDER="$(yaml_get model.provider)"
  MODEL_NAME="$(yaml_get model.name)"
  CANONICAL_SKILLS_DIR="${CANONICAL_SKILLS_DIR:-$(config_get fleet.canonical_skills_dir "$HOME/.agents/skills")}"
  python3 - "$TMP/config.yaml" "$PROJECT_PATH" "${HERMES_TIMEZONE:-America/New_York}" \
    "$MODEL_PROVIDER" "$MODEL_NAME" "$CANONICAL_SKILLS_DIR" "$ROLE" <<'PYEOF'
import json, pathlib, re, sys
path, cwd, timezone, provider, model, skills, role = sys.argv[1:8]
if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_]+)*", timezone):
    raise SystemExit("unsafe timezone")
config = {
    "timezone": timezone,
    "terminal": {"cwd": cwd},
    "skills": {"external_dirs": [skills]},
}
if provider or model:
    config["model"] = {}
    if provider:
        config["model"]["provider"] = provider
    if model:
        config["model"]["default"] = model
config["platform_toolsets"] = {
    "cli": ["web", "delegation", "no_mcp"],
    "cron": ["web", "delegation", "no_mcp"],
}
config["agent"] = {
    "disabled_toolsets": [
        "browser", "terminal", "file", "code_execution", "cronjob",
        "kanban", "homeassistant", "computer_use", "project", "skills",
    ]
}
config["delegation"] = {"max_spawn_depth": 1, "inherit_mcp_toolsets": False}
pathlib.Path(path).write_text(json.dumps(config, indent=2) + "\n")
PYEOF
  cat > "$TMP/profile.yaml" <<'YAML'
config:
  inherit_from: default
  save_mode: delta
YAML
else
  CANONICAL_PM_CONFIG="$(config_get fleet.canonical_pm_config "$HOME/.hermes/config.yaml")"
  if [[ -f "$CANONICAL_PM_CONFIG" ]]; then
    cp "$CANONICAL_PM_CONFIG" "$TMP/config.yaml"
  fi
fi
# Copy the project's SOUL.md as the canonical starting personality.
cp "$ROLE_DIR/SOUL.md" "$TMP/SOUL.md"

# 4. Git init + LFS + initial commit + push (skip if remote already has content)
if [[ "$REMOTE_HAS_CONTENT" == "0" ]]; then
  (
    cd "$TMP"
    git init -b main >/dev/null
    git lfs install --local >/dev/null 2>&1 || warn "git-lfs not installed; sessions.db will commit as raw binary"
    git lfs track "*.db" >/dev/null 2>&1 || true
    git lfs track "*.sqlite" >/dev/null 2>&1 || true
    python3 "$ROLE_DIR/.scripts/secret-scan.py" "$TMP"
    git add -A
    python3 "$ROLE_DIR/.scripts/secret-scan.py" "$TMP"
    git -c commit.gpgsign=false commit -m "Initial scaffold for $AGENT_ID" >/dev/null
    git remote add origin "$REMOTE_URL"
    python3 "$ROLE_DIR/.scripts/secret-scan.py" "$TMP"
    git push -u origin main 2>&1 | tail -3
  )
fi

# 5. Submodule-add into the role dir
# Compute relative path from the ROLE dir (which exists), then append /runtime
REL_ROLE_PATH="$(realpath --relative-to="$PROJECT_PATH" "$ROLE_DIR")"
REL_SUBMODULE_PATH="${REL_ROLE_PATH}/runtime"
log "    adding submodule at $REL_SUBMODULE_PATH"

# Idempotent: if the submodule is already registered, just update it.
# This handles re-runs where the .done marker was cleared or copier
# --overwrite regenerated the scripts.
SUBMODULE_ALREADY_REGISTERED=0
if git -C "$PROJECT_PATH" submodule status "$REL_SUBMODULE_PATH" >/dev/null 2>&1; then
  SUBMODULE_ALREADY_REGISTERED=1
fi

if [[ "$SUBMODULE_ALREADY_REGISTERED" == "1" ]]; then
  log "    submodule already registered — updating"
  (
    cd "$PROJECT_PATH"
    # If the local dir is missing (e.g. previous rm -rf), re-init it
    if [[ ! -d "$REL_SUBMODULE_PATH/.git" ]]; then
      git submodule update --init "$REL_SUBMODULE_PATH" 2>&1 | tail -3
    fi
  )
else
  # Clean any leftover untracked directory before adding
  rm -rf "$RUNTIME_LOCAL"
  (
    cd "$PROJECT_PATH"
    # -f forces the add even if a repo .gitignore matches the runtime path
    # (e.g. a stray `runtime/` rule). The runtime is a tracked submodule gitlink,
    # never ignored content, so forcing past .gitignore is always correct here.
    git submodule add -f "$REMOTE_URL" "$REL_SUBMODULE_PATH" 2>&1 | tail -3
  )
fi

# 6. Fold the staging profile state into the runtime, then symlink the profile
# name AT the runtime. HERMES_HOME is the runtime submodule, but the profile
# symlink is LOAD-BEARING: hermes resolves agents launched as
# `hermes --profile <name>` through ~/.hermes/profiles/<name> (and recreates it
# as a fresh standalone dir if it's missing, disconnecting the agent from its
# runtime), so that name MUST resolve to the runtime.
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE_NAME"
if [[ -d "$PROFILE_HOME" && ! -L "$PROFILE_HOME" ]]; then
  log "    migrating profile state into the runtime submodule"
  # OAuth provider credentials are fleet-shared via HERMES_OAUTH_FILE. Reporter
  # profiles never migrate .env; other roles preserve upstream PM behavior.
  if [[ "$ROLE" == "reporter" ]]; then
    MIGRATE_FILES=(config.yaml profile.yaml)
  else
    MIGRATE_FILES=(.env config.yaml)
  fi
  for f in "${MIGRATE_FILES[@]}"; do
    [[ -f "$PROFILE_HOME/$f" && ! -e "$RUNTIME_LOCAL/$f" ]] && cp "$PROFILE_HOME/$f" "$RUNTIME_LOCAL/$f"
  done
  rm -rf "$PROFILE_HOME"
fi
ln -sfn "$RUNTIME_LOCAL" "$PROFILE_HOME"
log "    profile symlink $PROFILE_HOME -> $RUNTIME_LOCAL"

# Apply the one genuine per-repo config delta directly to the runtime config.
env HERMES_HOME="$RUNTIME_LOCAL" "$HERMES_BIN" config set terminal.cwd "$PROJECT_PATH" >/dev/null 2>&1 || true

if [[ "$ROLE" == "pm" ]]; then
  VOXXY_PLUGIN_DIR="${VOXXY_PLUGIN_DIR:-$(config_get fleet.voxxy_plugin_dir "$HOME/code/voxxy/plugins/tts/voxxy")}"
  if [[ -d "$VOXXY_PLUGIN_DIR" ]]; then
    mkdir -p "$RUNTIME_LOCAL/plugins/tts"
    ln -sfn "$VOXXY_PLUGIN_DIR" "$RUNTIME_LOCAL/plugins/tts/voxxy"
    log "    linked Voxxy plugin into runtime"
  else
    warn "    Voxxy plugin dir missing: $VOXXY_PLUGIN_DIR"
  fi

  if [[ -x "$HERMES_BIN" ]]; then
    env HERMES_HOME="$RUNTIME_LOCAL" "$HERMES_BIN" config set plugins.enabled.0 tts/voxxy >/dev/null 2>&1 || true
    env HERMES_HOME="$RUNTIME_LOCAL" "$HERMES_BIN" config set tts.provider voxxy >/dev/null 2>&1 || true
    env HERMES_HOME="$RUNTIME_LOCAL" "$HERMES_BIN" config set tts.voice rick >/dev/null 2>&1 || true
    log "    set PM runtime TTS provider -> voxxy"
  else
    warn "    Hermes bin missing; skipped PM Voxxy config"
  fi
fi

rm -rf "$TMP"
mark_done 20-runtime-repo
