# Runbook: repair a failing runtime-checkpoint service

Status: Active operations runbook
Applies to: every Hermes agent whose `runtime/` is a git submodule checkpointed
by its `hermes-<agent>-heartbeat.service` systemd unit (the heartbeat runner
calls `.scripts/checkpoint.sh` to commit+push the runtime).

This runbook fixes the failure where the runtime checkpoint dies with
**exit 128** and the agent's runtime "brain" stops being backed up. The
checkpoint now runs inside the fused heartbeat tick (board-reconciliation
sentinel pass + gated checkpoint), so the failing unit is the agent's
`heartbeat.service`. It is written so any agent or operator can copy it to
another repo and apply it safely. First captured 2026-06-01 fixing the
Drumjangler PM checkpoint; the same fault was present on other agents in the
fleet.

## Symptom

```bash
systemctl --user status hermes-<agent>-heartbeat.service
#   Active: failed (Result: exit-code) ... status=128/n/a
#   the heartbeat invokes .../agents/hermes/<role>/.scripts/checkpoint.sh
```

`checkpoint.sh` is just `cd runtime && git add -A && commit && push`. Run it by
hand and you see the real error:

```text
fatal: in unpopulated submodule 'agents/hermes/<role>/runtime'
```

## Root cause

`agents/hermes/<role>/runtime` is registered as a **git submodule** in the
project repo (`.gitmodules` has its URL; the parent index holds a `160000`
gitlink), **but the submodule is not populated** — there is no `runtime/.git`,
and `.git/modules/<path>` is gone. So any git command run *inside* `runtime/`
resolves **up to the parent repo**, which treats `runtime/` as an unpopulated
submodule path and makes `git add -A` fatal (exit 128).

Confirm it:

```bash
RT=agents/hermes/<role>/runtime
git -C "$RT" rev-parse --show-toplevel     # prints the PARENT repo, not RT  -> red flag
ls "$RT/.git"                               # missing  -> unpopulated
git ls-files -s "$RT" | head -1             # mode 160000 <sha> ...           -> gitlink
git config -f .gitmodules --get-regexp "$RT" # submodule.<path>.url git@...
ls .git/modules/"$RT" 2>/dev/null || echo "objects gone -> must re-fetch from remote"
```

Consequence: the agent's durable brain (`SOUL.md`, `memories/`, `profiles/`,
`skills/`, config) lives on disk (the running agent still reads it) but is **not
committed or pushed anywhere** — no backup until this is repaired.

## The fix (safe, in-place, preserves all on-disk state)

The key safety property: `git reset --mixed` moves `HEAD` + the index only and
**never rewrites working-tree files**, so the live brain (and a multi-hundred-MB
`state.db`) is untouched. Never use `git submodule update`/`git checkout <tree>`
here — those would overwrite live, uncommitted edits with the older committed
versions.

```bash
RT=agents/hermes/<role>/runtime
URL=$(git config -f .gitmodules --get submodule."$RT".url)

# 0. remote must be reachable (read-only)
GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15' git ls-remote "$URL" >/dev/null

cd "$RT"
# 1. re-init git in place on branch 'main' (creates .git/ only; no file changes)
git init -q
git symbolic-ref HEAD refs/heads/main
git remote add origin "$URL"
git fetch -q origin
# 2. re-base history onto the remote tip WITHOUT touching the working tree
git reset --mixed -q origin/main
git branch --set-upstream-to=origin/main main 2>/dev/null || true
# 3. the repo uses git-lfs for *.db/images (.gitattributes); register filters
git lfs install --local
# 4. restore tracked config that may be missing on disk (avoids spurious deletes)
git checkout origin/main -- .gitattributes README.md 2>/dev/null || true
```

### Make it sustainable: back up the brain, not volatile state

Even though `.gitattributes` routes `*.db` through LFS, committing a live,
constantly-growing `state.db` (often hundreds of MB) every hour is not
sustainable, and `lsp/` (node_modules), `checkpoints/` (a nested object store),
and `sessions/` (ephemeral dumps) must never be committed. The checkpoint repo's
job is the **durable brain**. Ensure `runtime/.gitignore` contains:

```gitignore
# Runtime working state — NOT part of the durable brain backup.
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite3
*.lock
.update_check
lsp/
checkpoints/
sessions/
cron/
*_cache.json
```

Then untrack anything volatile that is currently tracked (keeps the files on
disk):

```bash
git rm -r --cached --ignore-unmatch state.db state.db-wal state.db-shm cron
```

### Verify BEFORE committing (mandatory safety gate)

```bash
git add -A --dry-run | awk '{print $2}' | while read -r f; do
  [ -f "$f" ] && s=$(stat -c%s "$f") && [ "$s" -gt 1048576 ] && echo "BIG: $((s/1048576))MB $f"; done
# ^ must print nothing. Also confirm no state.db / lsp/ / sessions/ / checkpoints/,
#   and no secrets (.env, auth.json, *.key, *token*) in the staged set.
```

### Commit, push, and bring the service green

```bash
bash agents/hermes/<role>/.scripts/checkpoint.sh        # exits 0, pushes brain
systemctl --user reset-failed hermes-<agent>-heartbeat.service
systemctl --user start       hermes-<agent>-heartbeat.service
systemctl --user show hermes-<agent>-heartbeat.service -p Result -p ExecMainStatus
#   Result=success  ExecMainStatus=0
```

## One-shot

`scripts/repair-runtime-checkpoint.sh` automates all of the above. It is
**dry-run by default** (diagnoses and prints the plan) and only mutates with
`--apply`. Run it from a project repo root:

```bash
# diagnose
hermes-agent-template/scripts/repair-runtime-checkpoint.sh agents/hermes/pm/runtime
# apply the in-place re-attach + .gitignore policy + untrack (no commit)
hermes-agent-template/scripts/repair-runtime-checkpoint.sh --apply agents/hermes/pm/runtime
# then validate the service
systemctl --user reset-failed <svc> && systemctl --user start <svc>
```

## Fleet sweep — this is rarely just one repo

```bash
systemctl --user list-units --all '*-heartbeat.service' | grep -i failed
# for each failing agent, locate its runtime and run the repair script.
```

## Lessons learned (why this happened / what to remember)

- An **unpopulated submodule** silently redirects in-directory git commands to
  the parent repo; `git add -A` then fatals with exit 128. The fix is to restore
  the submodule's own `.git` (objects from the remote), not to touch the parent.
- `git reset --mixed <ref>` is the safe way to re-attach history to a populated
  working directory: it preserves every on-disk file.
- The checkpoint must **exclude volatile working state** (`state.db*`, `lsp/`,
  `checkpoints/`, `sessions/`, caches, locks). Back up identity + memory + skills.
- Core dumps from the agent will **not** appear in `coredumpctl`/`/var/crash` on
  Ubuntu: apport discards crashes from unpackaged venv-Python binaries. Use
  `PYTHONFAULTHANDLER=1` or a private `core_pattern` to capture a backtrace.
- Always run the fleet sweep — provisioning copies this layout to every agent,
  so a structural fault tends to be fleet-wide.
