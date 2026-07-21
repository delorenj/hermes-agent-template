#!/usr/bin/env bash
# fleet-prune-debris — remove evolutionary debris from the Hermes fleet's
# systemd USER units, and retire now-dead runtime-checkpoint units for agents
# whose runtime is already pure-local (see degit-runtime.sh, fleet decision D2).
#
# Conservative by design. It only AUTO-removes things that are unambiguously
# dead:
#   - a hardcoded allowlist of DISABLED stale units (old singleton gateways,
#     the delodocs per-agent dashboard) — and only while they are NOT active;
#   - *.bak-* backup copies of hermes unit files;
#   - hermes-<id>-pm-checkpoint.{service,timer} whose runtime has NO .git
#     (already pure-local -> the checkpoint unit can never do anything again).
# Everything ambiguous (active/enabled redundant units, the carrie persona, the
# delonet-company-reporter archetype, FAILED units still git-backed) is REPORTED
# as MANUAL and never touched.
#
# Dry-run by default. --apply performs the removals + one daemon-reload.
#
# Usage: fleet-prune-debris.sh [--apply]
set -euo pipefail

APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) printf 'fleet-prune-debris: unknown arg: %s\n' "$a" >&2; exit 2 ;;
  esac
done

SD="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
[ -d "$SD" ] || { printf 'ERROR: no systemd user dir at %s\n' "$SD" >&2; exit 1; }

MODE="dry-run"; [ "$APPLY" -eq 1 ] && MODE="APPLY"
printf '== fleet-prune-debris (%s) :: %s ==\n' "$MODE" "$SD"
note(){ printf '  %s\n' "$*"; }
uc(){ systemctl --user "$@" 2>/dev/null; }         # user systemctl, quiet
is_active(){ uc is-active "$1" >/dev/null; }
exists(){ [ -e "$SD/$1" ]; }
DID=0

# remove_unit BASENAME... — disable+stop (if loaded) then rm the file(s).
remove_unit(){
  local u
  for u in "$@"; do
    exists "$u" || { note "absent (ok): $u"; continue; }
    if [ "$APPLY" -eq 1 ]; then
      uc disable --now "$u" || true
      rm -f "$SD/$u"
      note "removed: $u"
    else
      note "[would] disable --now + rm: $u"
    fi
    DID=1
  done
}

printf '\n[1] disabled stale units (old singleton gateways / stale dashboard)\n'
# From recon: these are DISABLED + superseded. Auto-remove only if not active.
for u in hermes-gateway.service hermes-gateway-de01182b.service hermes-dashboard-delodocs-pm.service; do
  if exists "$u"; then
    if is_active "$u"; then note "MANUAL: $u is ACTIVE — expected disabled; confirm before removing";
    else remove_unit "$u"; fi
  else note "absent (ok): $u"; fi
done

printf '\n[2] *.bak-* backup copies of hermes unit files\n'
shopt -s nullglob
BAKS=("$SD"/hermes-*.bak-*)   # matches *.service.bak-* / *.timer.bak-* too (glob * spans dots)
shopt -u nullglob
if [ "${#BAKS[@]}" -eq 0 ]; then note "none"; else
  for f in "${BAKS[@]}"; do
    if [ "$APPLY" -eq 1 ]; then rm -f "$f"; note "removed: $(basename "$f")"; else note "[would] rm: $(basename "$f")"; fi
    DID=1
  done
fi

printf '\n[3] dead checkpoint units for pure-local runtimes\n'
shopt -s nullglob
CKPTS=("$SD"/hermes-*-pm-checkpoint.service)
shopt -u nullglob
if [ "${#CKPTS[@]}" -eq 0 ]; then note "none"; else
  for svc in "${CKPTS[@]}"; do
    base="$(basename "$svc")"
    # Derive the runtime from ExecStart=<role_dir>/.scripts/checkpoint.sh
    exec_line="$(grep -m1 '^ExecStart=' "$svc" 2>/dev/null | sed 's/^ExecStart=//')"
    script="${exec_line%% *}"                    # strip any args
    role_dir="$(dirname "$(dirname "$script")")" # .../pm/.scripts/x.sh -> .../pm
    runtime="$role_dir/runtime"
    if [ ! -d "$runtime" ]; then
      note "MANUAL: $base -> runtime not found ($runtime); leaving"
      continue
    fi
    if [ -e "$runtime/.git" ]; then
      note "SKIP: $base -> runtime still git-backed (degit-runtime.sh first): $runtime"
    else
      note "pure-local -> retiring: $base (+ .timer)"
      remove_unit "$base" "${base%.service}.timer"
    fi
  done
fi

printf '\n[4] MANUAL review (not auto-touched)\n'
# Running-redundant / ambiguous units surfaced for a human decision.
for u in hermes-gateway-intelliforia-voice-agent.service hermes-carrie-backend.service hermes-carrie-telegram-gateway.service; do
  exists "$u" && note "MANUAL: $u ($(uc is-active "$u" || true)/$(uc is-enabled "$u" || true)) — redundant/legacy persona; decide keep vs retire"
done
# delonet-company-reporter is a different archetype (not a PM).
shopt -s nullglob
for u in "$SD"/hermes-delonet-company-reporter-*.service; do
  b="$(basename "$u")"; note "MANUAL: $b ($(uc is-active "$b" || true)) — non-PM reporter archetype; repair or remove as a unit"
done
shopt -u nullglob
# Any remaining failed hermes units.
FAILED="$(uc --plain --no-legend list-units --state=failed 'hermes-*' | awk '{print $1}')"
if [ -n "$FAILED" ]; then
  printf '%s\n' "$FAILED" | while read -r f; do [ -n "$f" ] && note "FAILED: $f — investigate (degit fixes git-push failures; missing-script units are orphans to remove)"; done
else note "no failed hermes units"; fi

echo
if [ "$APPLY" -eq 1 ]; then
  uc daemon-reload || true
  printf 'RESULT: applied + daemon-reload. Re-run to confirm idempotent (should be all "absent/none/SKIP").\n'
elif [ "$DID" -eq 1 ]; then
  printf 'RESULT: dry-run. Re-run with --apply to remove the [would] items above.\n'
else
  printf 'RESULT: nothing to prune (already clean) — only MANUAL items remain.\n'
fi
