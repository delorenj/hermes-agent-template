#!/usr/bin/env bash
# Retrofit existing fleet systemd unit files to load runtime/.env.
#
# Companion to the fix in commit 3b7d2e5 (template/.scripts/70-systemd.sh).
# Future agents pick the fix up automatically via pjangler; existing agents
# already have unit files installed at ~/.config/systemd/user/ that won't
# change until we rewrite them in place.
#
# What this does:
#   - Finds every hermes-<agent>-{gateway,consumer}.service unit
#   - Inserts `EnvironmentFile=-<runtime>/.env` after `Environment=HERMES_HOME=...`
#     if not already present
#   - daemon-reloads once
#   - restarts each unit it modified (skippable with --no-restart)
#
# Idempotent: re-running is a no-op if the directive is already present.
# Dry-run by default; pass --apply to actually write/restart.
#
# Usage:
#   scripts/retrofit-70-envfile.sh             # dry-run: show what would change
#   scripts/retrofit-70-envfile.sh --apply     # write + daemon-reload + restart
#   scripts/retrofit-70-envfile.sh --apply --no-restart
#
set -euo pipefail

APPLY=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --apply)      APPLY=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

SYS_DIR="$HOME/.config/systemd/user"
DIRECTIVE_PREFIX="EnvironmentFile=-"

shopt -s nullglob
UNITS=( "$SYS_DIR"/hermes-*-gateway.service "$SYS_DIR"/hermes-*-consumer.service )
shopt -u nullglob

if [[ ${#UNITS[@]} -eq 0 ]]; then
  echo "no hermes-*-{gateway,consumer}.service units found under $SYS_DIR"
  exit 0
fi

mode="DRY-RUN"; [[ "$APPLY" == "1" ]] && mode="APPLY"
echo "[$mode] scanning ${#UNITS[@]} unit(s) under $SYS_DIR"

MODIFIED=()
SKIPPED_OK=()
SKIPPED_BAD=()

for unit in "${UNITS[@]}"; do
  base="$(basename "$unit")"

  # Parse the runtime path from the Environment=HERMES_HOME= line.
  runtime="$(grep -E '^Environment=HERMES_HOME=' "$unit" | head -1 | cut -d= -f3-)"
  if [[ -z "$runtime" ]]; then
    echo "  SKIP  $base — no Environment=HERMES_HOME= line; not a template-generated unit"
    SKIPPED_BAD+=( "$base" )
    continue
  fi

  expected="${DIRECTIVE_PREFIX}${runtime}/.env"

  if grep -qxF "$expected" "$unit"; then
    echo "  OK    $base — already has $expected"
    SKIPPED_OK+=( "$base" )
    continue
  fi

  echo "  PATCH $base"
  echo "          + $expected"

  if [[ "$APPLY" == "1" ]]; then
    # Insert the directive on the line AFTER the Environment=HERMES_HOME= line.
    # Use a tmp file + atomic replace to avoid half-written units.
    tmp="$(mktemp "$unit.XXXXXX")"
    awk -v ins="$expected" '
      { print }
      /^Environment=HERMES_HOME=/ && !done { print ins; done=1 }
    ' "$unit" > "$tmp"
    chmod --reference="$unit" "$tmp"
    mv "$tmp" "$unit"
  fi
  MODIFIED+=( "$base" )
done

echo
echo "summary:"
echo "  ${#MODIFIED[@]} patched, ${#SKIPPED_OK[@]} already correct, ${#SKIPPED_BAD[@]} skipped (non-template)"

if [[ "$APPLY" != "1" ]]; then
  echo
  echo "dry-run only. re-run with --apply to write the changes."
  exit 0
fi

if [[ ${#MODIFIED[@]} -eq 0 ]]; then
  echo "nothing to reload."
  exit 0
fi

echo
echo "systemctl --user daemon-reload"
systemctl --user daemon-reload

if [[ "$RESTART" != "1" ]]; then
  echo "skipping restarts (--no-restart). reload only."
  exit 0
fi

echo
echo "restarting ${#MODIFIED[@]} modified unit(s)..."
for unit in "${MODIFIED[@]}"; do
  echo "  systemctl --user restart $unit"
  if ! systemctl --user restart "$unit"; then
    echo "    WARN: restart failed for $unit (status below)" >&2
    systemctl --user --no-pager status "$unit" | head -10 >&2 || true
  fi
done

echo "done."
