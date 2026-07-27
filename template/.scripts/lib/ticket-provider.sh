# shellcheck shell=bash
# Ticket-provider adapter dispatcher — the single seam between the heartbeat
# sentinel engine and a concrete ticket system (Linear | Plane | Trello).
#
# The engine NEVER calls a provider directly. It calls `tp <op> [args...]`,
# which dispatches to providers/<provider>.sh. Swapping providers is a one-line
# config change in role.yaml (ticket_provider.name) — no engine edits.
#
# Contract (operations every provider must implement):
#   resolve                       -> JSON {provider, board_id, board_url}
#   active_milestone              -> JSON {id, name, state}
#   list_issues                   -> JSON [ {id,key,title,state,state_type,
#                                            updated_at,assignee,url}, ... ]
#   get_issue <id>                -> JSON {id,key,title,description,acceptance,
#                                          state,state_type,comments:[...]}
#   comment <id> <body>           -> prints comment id
#   resolve_issue_id <reference>  -> canonical provider issue UUID/ID
#   ensure_comment <id> <marker> <body>
#                                  -> exhaustively checks then posts under a
#                                     local cross-run lock; JSON {status,
#                                     target_issue,error_category,error_summary}
#   transition <id> <normalized>  -> moves issue; normalized in
#                                     backlog|unstarted|started|in_review|completed
#   create_board <name> <id> <d>  -> JSON {board_id, board_url}
#
# Each provider reads its credentials from the environment (see providers/*.sh
# headers) and the board binding from role.yaml under `ticket_provider:`.

# Resolve the provider name: explicit env wins, then repo-root .project.json
# (the SOT), then role.yaml (self-parsed so this works even when _lib.sh /
# yaml_get is not loaded), then default.
tp_provider_name() {
  if [ -n "${TICKET_PROVIDER:-}" ]; then
    printf '%s\n' "$TICKET_PROVIDER"
    return 0
  fi
  # .project.json ticket_provider.type — walk up from the role dir to repo root.
  local role_dir
  role_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
  if [ -n "$role_dir" ]; then
    local sot_type
    sot_type="$(python3 - "$role_dir" <<'PY' 2>/dev/null
import sys, json, pathlib
start = pathlib.Path(sys.argv[1]).resolve()
for parent in [start, *start.parents]:
    f = parent / ".project.json"
    if f.is_file():
        try:
            print((json.loads(f.read_text()).get("ticket_provider") or {}).get("type", ""))
        except Exception:
            print("")
        break
PY
)"
    [ -n "$sot_type" ] && { printf '%s\n' "$sot_type"; return 0; }
  fi
  local role_yaml
  role_yaml="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/role.yaml"
  if [ -f "$role_yaml" ]; then
    local name
    name="$(python3 - "$role_yaml" <<'PY'
import re, sys, pathlib
t = pathlib.Path(sys.argv[1]).read_text()
m = re.search(r'(?ms)^ticket_provider:\s*$(.*?)(?=^\S)', t + "\n\x00")
block = m.group(1) if m else ""
mm = re.search(r'(?m)^\s*name:\s*"?([^"\n]*)"?\s*$', block)
print(mm.group(1).strip() if mm else "")
PY
)"
    [ -n "$name" ] && { printf '%s\n' "$name"; return 0; }
  fi
  printf 'linear\n'
}

# Directory holding provider implementations (sibling of this lib).
tp_providers_dir() {
  if [ -n "${TP_PROVIDERS_DIR:-}" ]; then
    printf '%s\n' "$TP_PROVIDERS_DIR"
    return 0
  fi
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  printf '%s/../providers\n' "$here"
}

# Find the installing repo root from the role directory. The exact repository
# identity is still read from .project.json.project_name by run-retro.py.
tp_repo_root() {
  local role_dir
  role_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
  python3 - "$role_dir" <<'PY'
import pathlib, sys
start = pathlib.Path(sys.argv[1]).resolve()
for parent in (start, *start.parents):
    if (parent / ".project.json").is_file():
        print(parent)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

# Dispatch one operation to the active provider.
tp() {
  local op="${1:-}"; shift || true
  [ -n "$op" ] || { echo "tp: missing operation" >&2; return 2; }

  local name impl
  name="$(tp_provider_name)"
  impl="$(tp_providers_dir)/${name}.sh"

  if [ ! -f "$impl" ]; then
    echo "tp: unknown ticket provider '$name' (no $impl)" >&2
    return 2
  fi

  if [ "$op" = "ensure_comment" ]; then
    local issue_id="${1:-}" marker="${2:-}"
    [ -n "$issue_id" ] && [ -n "$marker" ] || {
      echo "tp: usage: ensure_comment <canonical-id> <marker> <body>" >&2
      return 2
    }
    local lock_root lock_key lock_path
    if [ -n "${TP_COMMENT_LOCK_ROOT:-}" ]; then
      lock_root="$TP_COMMENT_LOCK_ROOT"
    else
      lock_root="$(tp_repo_root)/_bmad-output/implementation-artifacts/run-retros/.locks/comments" || {
        printf '{"status":"failed","target_issue":"%s","error_category":"serialization_failed","error_summary":"repository binding unavailable; no post attempted"}\n' "$issue_id"
        return 0
      }
    fi
    mkdir -p "$lock_root" 2>/dev/null || {
      printf '{"status":"failed","target_issue":"%s","error_category":"serialization_failed","error_summary":"comment lock unavailable; no post attempted"}\n' "$issue_id"
      return 0
    }
    lock_key="$(printf '%s\n%s\n%s\n' "$name" "$issue_id" "$marker" | sha256sum | awk '{print $1}')"
    lock_path="$lock_root/$lock_key.lock"
    : >> "$lock_path" 2>/dev/null || {
      printf '{"status":"failed","target_issue":"%s","error_category":"serialization_failed","error_summary":"comment lock unavailable; no post attempted"}\n' "$issue_id"
      return 0
    }
    (
      flock -x 9 || {
        printf '{"status":"failed","target_issue":"%s","error_category":"serialization_failed","error_summary":"comment lock unavailable; no post attempted"}\n' "$issue_id"
        exit 0
      }
      TICKET_PROVIDER="$name" sh "$impl" "$op" "$@"
    ) 9>"$lock_path"
    return
  fi

  TICKET_PROVIDER="$name" sh "$impl" "$op" "$@"
}

# Normalized states the engine reasons in. Adapters map these to provider terms.
TP_STATES="backlog unstarted started in_review completed"

tp_is_valid_state() {
  case " $TP_STATES " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}
