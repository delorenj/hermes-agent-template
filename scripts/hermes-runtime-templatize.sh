#!/usr/bin/env bash
# hermes-runtime-templatize — dedup a runtime's skills/ onto the shared
# hermes-base pack, losing zero agent work and never leaving a broken state.
#
# SAFETY MODEL (verified against hermes source): once the pack is in
# config.yaml skills.external_dirs, EVERY base-named local skill collides with
# the pack by frontmatter name. The prompt INDEX tolerates it (local wins), but
# the content loader (skill_view) REFUSES a collision it can't resolve to one
# path ("Ambiguous skill name"). So a runtime is only safe to WIRE once its
# base-named locals are name-disjoint from the pack — i.e. every base local is
# either byte-IDENTICAL (safe to delete → resolves from the pack) or has been
# triaged away (discarded / promoted / renamed). While any DIVERGED base local
# remains, this tool REFUSES to wire and makes NO changes (it only captures a
# diff patch for triage).
#
# Order per runtime: classify → (gate) → wire → delete identical.
#   IDENTICAL base local   -> deleted (resolves read-only from the pack)
#   DIVERGED base local    -> BLOCKS wiring; patch captured; left in place for
#                             triage. `--discard-drift` deletes them instead
#                             (accept the pack version — only for pure drift).
#   agent-added (non-base) -> left in the overlay, untouched.
#
# Dry-run by default. --apply mutates. Idempotent.
# Usage: hermes-runtime-templatize.sh [--apply] [--discard-drift] [--root DIR]
#          [--pack DIR] [--patches DIR]
set -euo pipefail

APPLY=0; DISCARD_DRIFT=0; ROOT=""; PATCHES=""
PACK="/home/delorenj/code/skillex/packs/hermes-base/0.18.2"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --discard-drift) DISCARD_DRIFT=1 ;;
    --root) shift; ROOT="${1:-}" ;;
    --pack) shift; PACK="${1:-}" ;;
    --patches) shift; PATCHES="${1:-}" ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) printf 'templatize: unknown arg: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note(){ printf '  %s\n' "$*"; }
ROOT="${ROOT:-$PWD}"; ROOT="$(cd "$ROOT" && pwd)"
MANIFEST="$PACK/MANIFEST.sha256"
[ -f "$MANIFEST" ] || die "no pack manifest at $MANIFEST (build the pack first)"
[ -d "$ROOT/agents/hermes" ] || die "no agents/hermes under $ROOT"
PATCHES="${PATCHES:-$ROOT/.templatize-patches}"

MODE="dry-run"; [ "$APPLY" -eq 1 ] && MODE="APPLY"
printf '== templatize (%s) :: %s :: pack=%s ==\n' "$MODE" "$ROOT" "$PACK"
treehash(){ ( cd "$1" && find . -type f | LC_ALL=C sort | while read -r f; do sha256sum "$f"; done | sha256sum | cut -d' ' -f1 ); }

wire_cfg(){ # $1 = config.yaml — comment-safe append of $PACK to skills.external_dirs
  local cfg="$1"
  [ -f "$cfg" ] || { note "no config.yaml at $cfg — cannot wire"; return 1; }
  if grep -qF "$PACK" "$cfg"; then note "wire: pack already in external_dirs"; return 0; fi
  # A config with no skills.external_dirs (bare-layout agents) CANNOT be wired
  # safely — refuse (return 1) so the caller leaves skills untouched. Do NOT
  # delete local skills that would then resolve from nowhere.
  grep -qE '^[[:space:]]*external_dirs:' "$cfg" || {
    note "BLOCKED: $cfg has no skills.external_dirs (bare config) — provision a skills block before dedup"; return 1; }
  if [ "$APPLY" -eq 0 ]; then note "[would] append '$PACK' to skills.external_dirs in $cfg"; return 0; fi
  if PACK="$PACK" python3 - "$cfg" <<'PY'
import os,sys
cfg=sys.argv[1]; pack=os.environ["PACK"]
lines=open(cfg).read().splitlines()
out=[]; i=0; n=len(lines); done=False
while i<n:
    out.append(lines[i])
    if not done and lines[i].strip()=="external_dirs:":
        key_indent=len(lines[i])-len(lines[i].lstrip())
        j=i+1; item_indent=None
        while j<n and lines[j].strip().startswith("- "):
            if item_indent is None: item_indent=len(lines[j])-len(lines[j].lstrip())
            out.append(lines[j]); j+=1
        ind = item_indent if item_indent is not None else key_indent
        out.append(" "*ind+"- "+pack)
        done=True; i=j; continue
    i+=1
if not done: sys.exit(3)
open(cfg,"w").write("\n".join(out)+"\n")
PY
  then note "wire: appended pack to external_dirs"; return 0
  else note "wire FAILED (python could not place the entry) — skills left untouched"; return 1; fi
}

process(){ # $1 = runtime dir, $2 = repo label
  local rt="$1" repo="$2" sk="$rt/skills" cfg="$rt/config.yaml"
  [ -d "$sk" ] || { note "no skills/ — nothing to reconcile"; return 0; }
  local -a IDENT=() DIVERGED=()
  local want name d h
  while read -r want name; do
    [[ "$want" == \#* || -z "$want" ]] && continue
    d="$sk/$name"; [ -d "$d" ] || continue
    h="$(treehash "$d")"
    if [ "$h" = "$want" ]; then IDENT+=("$name"); else
      DIVERGED+=("$name")
      mkdir -p "$PATCHES"
      [ "$APPLY" -eq 1 ] && diff -ru "$PACK/$name" "$d" > "$PATCHES/$name.$repo.patch" 2>/dev/null || true
    fi
  done < "$MANIFEST"
  local overlay; overlay="$(find "$sk" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r x; do grep -qE "  $(basename "$x")\$" "$MANIFEST" || echo x; done | wc -l)"
  note "classify: identical=${#IDENT[@]}  diverged=${#DIVERGED[@]}  overlay-adds=$overlay"

  # GATE: diverged base locals block a clean wire (would go ambiguous).
  if [ "${#DIVERGED[@]}" -gt 0 ] && [ "$DISCARD_DRIFT" -eq 0 ]; then
    note "BLOCKED: ${#DIVERGED[@]} diverged base skill(s) must be triaged before wiring:"
    note "  ${DIVERGED[*]}"
    note "  patches: $PATCHES/<name>.$repo.patch  → per skill: discard drift / promote to pack / rename overlay(+frontmatter name)"
    note "  (no wire, no deletions — safe. Re-run after triage, or --discard-drift to accept the pack for pure-drift skills.)"
    return 0
  fi

  # Reconcilable: wire, then delete the base locals that now resolve from the pack.
  wire_cfg "$cfg" || { note "abort: could not wire; leaving skills untouched"; return 0; }
  local n
  for n in "${IDENT[@]}"; do
    if [ "$APPLY" -eq 1 ]; then rm -rf "$sk/$n"; else printf '  [would] rm %s (identical → pack)\n' "$sk/$n"; fi
  done
  if [ "$DISCARD_DRIFT" -eq 1 ]; then
    for n in "${DIVERGED[@]}"; do
      if [ "$APPLY" -eq 1 ]; then rm -rf "$sk/$n"; else printf '  [would] rm %s (diverged, --discard-drift; patch saved)\n' "$sk/$n"; fi
    done
  fi
  note "reconciled: removed ${#IDENT[@]} identical$([ "$DISCARD_DRIFT" -eq 1 ] && echo " + ${#DIVERGED[@]} drift") base local(s); base now resolves from pack"
}

mapfile -t RUNTIMES < <(find "$ROOT/agents/hermes" -mindepth 2 -maxdepth 2 -type d -name runtime 2>/dev/null | sort)
[ "${#RUNTIMES[@]}" -gt 0 ] || die "no agents/hermes/*/runtime under $ROOT"
repo="$(basename "$ROOT")"
for rt in "${RUNTIMES[@]}"; do printf -- '-- %s\n' "$rt"; process "$rt" "$repo"; done

echo
if [ "$APPLY" -eq 1 ]; then
  printf 'RESULT: applied where safe. For any BLOCKED runtime, triage the captured patches then re-run.\n'
  printf '        Verify: hermes-base-guard.sh check-tree <runtime>/skills must exit 0.\n'
else
  printf 'RESULT: dry-run. --apply wires + removes identical base copies where no diverged remain.\n'
fi
