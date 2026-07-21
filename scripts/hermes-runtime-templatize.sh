#!/usr/bin/env bash
# hermes-runtime-templatize — dedup a runtime's skills/ onto the shared
# hermes-base pack, losing zero agent work.
#
# Two phases (both dry-run by default; --apply to mutate):
#   WIRE      append the pack to config.yaml skills.external_dirs (additive,
#             reversible, comment-safe). Overlay still wins, so ZERO behaviour
#             change until RECONCILE removes local base copies.
#   RECONCILE classify each base-named dir in runtime/skills/ vs the pack MANIFEST:
#               - byte-identical base   -> rm (it resolves read-only from the pack)
#               - DIVERGED base         -> capture `diff -ru` to a patch, then
#                                          REPORT + LEAVE (needs human triage;
#                                          NEVER auto-removed)
#               - agent-added (non-base)-> leave in the overlay
#
# Precedence note (verified): local overlay wins the prompt INDEX, but the
# content loader (skill_view) REFUSES a divergent local<->pack name collision
# with "Ambiguous skill name". So base and overlay MUST end up name-disjoint —
# which is exactly what removing the byte-identical base copies achieves.
#
# Usage: hermes-runtime-templatize.sh [--apply] [--root DIR] [--pack DIR]
#          [--patches DIR] [--wire-only|--reconcile-only]
set -euo pipefail

APPLY=0; ROOT=""; PACK="/home/delorenj/code/skillex/packs/hermes-base/0.18.2"
PATCHES=""; DO_WIRE=1; DO_RECON=1
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --root) shift; ROOT="${1:-}" ;;
    --pack) shift; PACK="${1:-}" ;;
    --patches) shift; PATCHES="${1:-}" ;;
    --wire-only) DO_RECON=0 ;;
    --reconcile-only) DO_WIRE=0 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
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

# WIRE: comment-safe append of $PACK to skills.external_dirs in a config.yaml.
wire_cfg(){ # $1 = config.yaml
  local cfg="$1"
  [ -f "$cfg" ] || { note "no config.yaml at $cfg — skip wire"; return 0; }
  if grep -qF "$PACK" "$cfg"; then note "wire: pack already in external_dirs"; return 0; fi
  if [ "$APPLY" -eq 1 ]; then
    PACK="$PACK" python3 - "$cfg" <<'PY'
import os,sys
cfg=sys.argv[1]; pack=os.environ["PACK"]
lines=open(cfg).read().splitlines()
out=[]; i=0; n=len(lines); done=False
while i<n:
    out.append(lines[i])
    if not done and lines[i].strip()=="external_dirs:":
        key_indent=len(lines[i])-len(lines[i].lstrip())
        j=i+1; item_indent=None
        while j<n and lines[j].strip().startswith("- "):        # copy existing items
            if item_indent is None: item_indent=len(lines[j])-len(lines[j].lstrip())
            out.append(lines[j]); j+=1
        ind = item_indent if item_indent is not None else key_indent
        out.append(" "*ind+"- "+pack)                          # append ours after the last
        done=True; i=j; continue
    i+=1
if not done: sys.exit("external_dirs: key not found in "+cfg)
open(cfg,"w").write("\n".join(out)+"\n")
PY
    note "wire: appended pack to external_dirs"
  else
    note "[would] append '$PACK' to skills.external_dirs in $cfg"
  fi
}

# RECONCILE one runtime skills dir against the pack.
reconcile(){ # $1 = runtime skills dir, $2 = repo label
  local sk="$1" repo="$2" name h want rm_ct=0 div_ct=0 keep_ct=0
  [ -d "$sk" ] || { note "no skills/ dir — skip reconcile"; return 0; }
  while read -r want name; do
    [[ "$want" == \#* || -z "$want" ]] && continue
    local d="$sk/$name"
    [ -d "$d" ] || continue                      # not overlaid here; resolves from pack already
    h="$(treehash "$d")"
    if [ "$h" = "$want" ]; then
      if [ "$APPLY" -eq 1 ]; then rm -rf "$d"; else printf '  [would] rm %s (identical to pack)\n' "$d"; fi
      rm_ct=$((rm_ct+1))
    else
      # DIVERGED base skill: capture a patch, never auto-remove.
      mkdir -p "$PATCHES"
      local pf="$PATCHES/$name.$repo.patch"
      if [ "$APPLY" -eq 1 ]; then diff -ru "$PACK/$name" "$d" > "$pf" 2>/dev/null || true; fi
      note "DIVERGED base '$name' -> patch: $pf  (triage: discard drift / promote shared / rename overlay)"
      div_ct=$((div_ct+1))
    fi
  done < "$MANIFEST"
  # agent-added overlay dirs (not base-named) just stay
  keep_ct="$(find "$sk" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r d; do grep -qE "  $(basename "$d")\$" "$MANIFEST" || echo x; done | wc -l)"
  note "reconcile: identical=$rm_ct removed  diverged=$div_ct kept(patched)  overlay-adds=$keep_ct"
}

mapfile -t RUNTIMES < <(find "$ROOT/agents/hermes" -mindepth 2 -maxdepth 2 -type d -name runtime 2>/dev/null | sort)
[ "${#RUNTIMES[@]}" -gt 0 ] || die "no agents/hermes/*/runtime under $ROOT"
repo="$(basename "$ROOT")"

for rt in "${RUNTIMES[@]}"; do
  printf -- '-- %s\n' "$rt"
  [ "$DO_WIRE" -eq 1 ]  && wire_cfg "$rt/config.yaml"
  [ "$DO_RECON" -eq 1 ] && reconcile "$rt/skills" "$repo"
done

echo
if [ "$APPLY" -eq 1 ]; then
  printf 'RESULT: applied. Verify skills still load, then run hermes-base-guard.sh check-tree on each skills/ (must exit 0).\n'
  printf '        DIVERGED skills were NOT removed — triage the captured patches in %s\n' "$PATCHES"
else
  printf 'RESULT: dry-run. Re-run with --apply to wire + remove byte-identical base copies (diverged are only patch-captured).\n'
fi
