#!/usr/bin/env sh
# Trello ticket-provider adapter.
#
# Credentials:  TRELLO_KEY  TRELLO_TOKEN   (query-param auth)
# Board binding (role.yaml `ticket_provider:`):
#   name: trello
#   board: <board-id>                 (set by create_board / 42-ticket-provider)
#   state_map: { backlog:"Backlog", unstarted:"To Do", started:"In Progress",
#                in_review:"Review", completed:"Done" }   optional
#
# Trello model:  board = project & milestone, list = state, card = issue.
# Trello has no milestone primitive, so active_milestone returns the board.
#
# NOTE: list names are matched case-insensitively against state_map; override
# state_map in role.yaml if the board uses different column names.
set -eu

OP="${1:-}"; shift 2>/dev/null || true
if [ "${HERMES_BOUND_PROVIDER_CONFIG:-0}" = 1 ]; then
  ROLE_DIR=""; ROLE_YAML=""
else
  ROLE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
  ROLE_YAML="$ROLE_DIR/role.yaml"
fi
API="${TRELLO_API_URL:-https://api.trello.com/1}"
HTTP_MAX_BYTES=131072
HTTP_TIMEOUT_SECONDS=120
actions_file=""; response_file=""

cleanup_http_files() {
  [ -z "$actions_file" ] || rm -f "$actions_file"
  [ -z "$response_file" ] || rm -f "$response_file"
  case "${TMPDIR:-}" in
    /var/tmp/hermes-provider-*) rmdir "$TMPDIR" 2>/dev/null || true ;;
  esac
}
trap cleanup_http_files EXIT HUP INT TERM

die() { echo "trello: $*" >&2; exit 1; }
need_key() { [ -n "${TRELLO_KEY:-}" ] && [ -n "${TRELLO_TOKEN:-}" ] || die "TRELLO_KEY and TRELLO_TOKEN must be set"; }

tp_cfg() {
  if [ "${HERMES_BOUND_PROVIDER_CONFIG:-0}" = 1 ]; then
    pj_cfg "$1"
    return
  fi
  [ -f "$ROLE_YAML" ] || return 0
  python3 - "$ROLE_YAML" "$1" <<'PY'
import sys, re, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
m = re.search(r'(?ms)^ticket_provider:\s*$(.*?)(?=^\S)', text + "\n\x00")
block = m.group(1) if m else ""
mm = re.search(rf'(?m)^\s*{re.escape(sys.argv[2])}:\s*"?([^"\n]*)"?\s*$', block)
print(mm.group(1).strip() if mm else "")
PY
}

# pj_cfg KEY — read ticket_provider.<KEY> from the repo-root .project.json (the
# SOT), walking up from the role dir. Preferred over role.yaml.
pj_cfg() {
  if [ "${HERMES_BOUND_PROVIDER_CONFIG:-0}" = 1 ]; then
    python3 - "$1" <<'PY'
import json, os, sys
try:
    config = json.loads(os.environ["HERMES_BOUND_TICKET_PROVIDER_JSON"])
    value = config.get(sys.argv[1], "") if isinstance(config, dict) else ""
except Exception:
    raise SystemExit(1)
if value is None:
    value = ""
if not isinstance(value, (str, int, float, bool)):
    raise SystemExit(1)
print(str(value))
PY
    return
  fi
  python3 - "$ROLE_DIR" "$1" <<'PY'
import sys, json, pathlib
start = pathlib.Path(sys.argv[1]).resolve(); key = sys.argv[2]
for parent in [start, *start.parents]:
    f = parent / ".project.json"
    if f.is_file():
        try: tp = (json.loads(f.read_text()).get("ticket_provider") or {})
        except Exception: tp = {}
        print(tp.get(key, "") if isinstance(tp, dict) else ""); break
else:
    print("")
PY
}

BOARD="$(pj_cfg board_id)"; [ -n "$BOARD" ] || BOARD="$(tp_cfg board)"
# Normalized -> Trello list name (overridable via role.yaml state_map keys).
list_name_for() {
  case "$1" in
    backlog)   v="$(tp_cfg backlog)";   printf '%s' "${v:-Backlog}" ;;
    unstarted) v="$(tp_cfg unstarted)"; printf '%s' "${v:-To Do}" ;;
    started)   v="$(tp_cfg started)";   printf '%s' "${v:-In Progress}" ;;
    in_review) v="$(tp_cfg in_review)"; printf '%s' "${v:-Review}" ;;
    completed) v="$(tp_cfg completed)"; printf '%s' "${v:-Done}" ;;
    *) die "invalid normalized state: $1" ;;
  esac
}

# api METHOD PATH [extra-query] — call Trello, auth appended, print body.
api() {
  need_key
  method="$1"; path="$2"; extra="${3:-}"
  sep="?"; case "$path" in *\?*) sep="&" ;; esac
  url="$API/$path${sep}key=$TRELLO_KEY&token=$TRELLO_TOKEN${extra:+&$extra}"
  curl -fsS --max-filesize "$HTTP_MAX_BYTES" --max-time "$HTTP_TIMEOUT_SECONDS" \
    -X "$method" "$url"
}

new_http_body_file() {
  umask 077
  mktemp "${TMPDIR:-/tmp}/hermes-trello-http.XXXXXX"
}

canonical_card_id() {
  python3 - "$1" <<'PY'
import re,sys,unicodedata
value=unicodedata.normalize("NFKC",sys.argv[1]).strip().casefold()
if not re.fullmatch(r"[0-9a-f]{24}",value):
    raise SystemExit(1)
print(value)
PY
}

# Exhaust Trello comment actions in 1000-row pages. Return 0 when found, 1
# after an exhaustive miss, and 2 when lookup/pagination is not trustworthy.
trello_comment_marker_state() {
  card_id="$1"; marker="$2"; before=""; pages=0
  while :; do
    query="filter=commentCard&limit=1000"
    [ -z "$before" ] || query="$query&before=$before"
    actions_file="$(new_http_body_file)" || return 2
    if ! api GET "cards/$card_id/actions" "$query" >"$actions_file" 2>/dev/null; then
      rm -f "$actions_file"
      return 2
    fi
    state="$(MARKER="$marker" HTTP_MAX_BYTES="$HTTP_MAX_BYTES" python3 - "$actions_file" <<'PY'
import json,os,re,sys
try:
 raw=open(sys.argv[1],"rb").read(int(os.environ["HTTP_MAX_BYTES"])+1)
 if len(raw)>int(os.environ["HTTP_MAX_BYTES"]): raise ValueError
 rows=json.loads(raw.decode("utf-8"))
except Exception: print("invalid"); raise SystemExit(0)
if not isinstance(rows,list): print("invalid"); raise SystemExit(0)
marker=os.environ["MARKER"]
ids=[]
for row in rows:
 if not isinstance(row,dict) or not isinstance(row.get("id"),str):
  print("invalid"); raise SystemExit(0)
 if not re.fullmatch(r"[0-9a-f]{24}",row["id"]):
  print("invalid"); raise SystemExit(0)
 data=row.get("data")
 if not isinstance(data,dict) or not isinstance(data.get("text"),str):
  print("invalid"); raise SystemExit(0)
 ids.append(row["id"])
if len(ids)!=len(set(ids)):
 print("invalid"); raise SystemExit(0)
if any(marker in row["data"]["text"] for row in rows):
 print("found")
elif len(rows)<1000:
 print("absent")
else:
 print("more:"+rows[-1]["id"])
PY
)"
    rm -f "$actions_file"
    case "$state" in
      found) return 0 ;;
      absent) return 1 ;;
      more:*)
        next="${state#more:}"
        [ -n "$next" ] && [ "$next" != "$before" ] || return 2
        before="$next"; pages=$((pages + 1)); [ "$pages" -lt 10000 ] || return 2
        ;;
      *) return 2 ;;
    esac
  done
}

# Resolve a list id on the board by (normalized) state.
list_id_for() {
  [ -n "$BOARD" ] || die "ticket_provider.board not set"
  want="$(list_name_for "$1")"
  api GET "boards/$BOARD/lists" | NM="$want" python3 -c 'import sys,json,os
rows=json.load(sys.stdin); nm=os.environ["NM"].lower()
print(next((l["id"] for l in rows if l.get("name","").lower()==nm), ""))'
}

# All Trello ops require credentials; fail fast and clean before any pipe.
need_key

case "$OP" in
  resolve)
    [ -n "$BOARD" ] || die "board not set (run 42-ticket-provider.sh)"
    api GET "boards/$BOARD" "fields=name,url" | python3 -c 'import sys,json
b=json.load(sys.stdin); print(json.dumps({"provider":"trello","board_id":b.get("id",""),"board_url":b.get("url","")}))'
    ;;

  active_milestone)
    [ -n "$BOARD" ] || die "board not set"
    api GET "boards/$BOARD" "fields=name" | python3 -c 'import sys,json
b=json.load(sys.stdin); print(json.dumps({"id":b.get("id",""),"name":b.get("name",""),"state":"active"}))'
    ;;

  list_issues)
    [ -n "$BOARD" ] || die "board not set"
    # cards + lists, then label each card with its list name as the state.
    LISTS="$(api GET "boards/$BOARD/lists" "fields=name")"
    CARDS="$(api GET "boards/$BOARD/cards" "fields=name,idList,dateLastActivity,url,shortLink")"
    printf '%s\n%s\n' "$LISTS" "$CARDS" | python3 -c 'import sys,json
parts=sys.stdin.read().split("\n",1)
lists={l["id"]:l.get("name","") for l in json.loads(parts[0] or "[]")}
out=[]
for c in json.loads(parts[1] or "[]"):
    nm=lists.get(c.get("idList",""),"")
    out.append({"id":c.get("id",""),"key":c.get("shortLink",""),"title":c.get("name",""),
                "state":nm,"state_type":nm.lower().replace(" ","_"),
                "updated_at":c.get("dateLastActivity",""),"assignee":"","url":c.get("url","")})
print(json.dumps(out))'
    ;;

  get_issue)
    ID="${1:?usage: get_issue <id>}"
    CARD="$(api GET "cards/$ID" "fields=name,desc,idList")"
    COMM="$(api GET "cards/$ID/actions" "filter=commentCard")"
    printf '%s\n%s\n' "$CARD" "$COMM" | python3 -c 'import sys,json
parts=sys.stdin.read().split("\n",1)
c=json.loads(parts[0] or "{}"); acts=json.loads(parts[1] or "[]")
cs=[{"id":a.get("id",""),"body":(a.get("data") or {}).get("text",""),"author":(a.get("memberCreator") or {}).get("fullName","")} for a in acts]
print(json.dumps({"id":c.get("id",""),"key":c.get("id",""),"title":c.get("name",""),
                  "description":c.get("desc",""),"acceptance":c.get("desc",""),
                  "state":"","state_type":"","comments":cs}))'
    ;;

  comment)
    ID="${1:?usage: comment <id> <body>}"; BODY="${2:?}"
    api POST "cards/$ID/actions/comments" "text=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$BODY")" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))'
    ;;

  resolve_issue_id)
    REF="${1:?usage: resolve_issue_id <reference>}"
    NORMALIZED="$(python3 - "$REF" <<'PY'
import re,sys,unicodedata
value=unicodedata.normalize("NFKC",sys.argv[1]).strip()
if not re.fullmatch(r"[A-Za-z0-9]+",value):
    raise SystemExit(1)
print(value)
PY
)" || die "invalid issue reference"
    if ! CARD="$(api GET "cards/$NORMALIZED" "fields=id" 2>/dev/null)"; then
      die "issue lookup failed"
    fi
    ID="$(printf '%s' "$CARD" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
    CANONICAL="$(canonical_card_id "$ID" 2>/dev/null || true)"
    [ -n "$CANONICAL" ] || die "issue not found or provider returned a non-canonical card id"
    printf '%s\n' "$CANONICAL"
    ;;

  ensure_comment)
    ID="${1:?usage: ensure_comment <canonical-id> <marker> <body>}"
    MARKER="${2:?usage: ensure_comment <canonical-id> <marker> <body>}"
    BODY="${3:?usage: ensure_comment <canonical-id> <marker> <body>}"
    CANONICAL="$(canonical_card_id "$ID" 2>/dev/null || true)"
    [ "$CANONICAL" = "$ID" ] || die "ensure_comment requires a canonical Trello card id"
    printf '%s\n%s\n' "$MARKER" "$BODY" | python3 -c 'import re,sys
marker,body=sys.stdin.read().split("\n",1)
marker=marker.rstrip("\n"); body=body.rstrip("\n")
if not re.fullmatch(r"\[run-retro-comment:[0-9a-f]{64}\]",marker) or body.count(marker)!=1:
 raise SystemExit(1)' || die "invalid comment marker/body"
    if trello_comment_marker_state "$ID" "$MARKER"; then
      printf '{"provider":"trello","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\n' "$ID"
      exit 0
    else
      rc=$?
      if [ "$rc" -eq 2 ]; then
        printf '{"provider":"trello","status":"failed","target_issue":"%s","error_category":"lookup_failed","error_summary":"comment lookup failed; no post attempted"}\n' "$ID"
        exit 0
      fi
    fi
    ENCODED="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$BODY")"
    response_file="$(new_http_body_file)" || {
      printf '{"provider":"trello","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"comment post response was not confirmed; retry ensure_comment"}\n' "$ID"
      exit 0
    }
    if ! api POST "cards/$ID/actions/comments" "text=$ENCODED" >"$response_file" 2>/dev/null; then
      rm -f "$response_file"
      printf '{"provider":"trello","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"comment post response was not confirmed; retry ensure_comment"}\n' "$ID"
      exit 0
    fi
    COMMENT_ID="$(HTTP_MAX_BYTES="$HTTP_MAX_BYTES" python3 - "$response_file" <<'PY'
import json,os,re,sys
try:
 raw=open(sys.argv[1],"rb").read(int(os.environ["HTTP_MAX_BYTES"])+1)
 if len(raw)>int(os.environ["HTTP_MAX_BYTES"]): raise ValueError
 data=json.loads(raw.decode("utf-8"))
 value=data.get("id") if isinstance(data,dict) else None
 print(value if isinstance(value,str) and re.fullmatch(r"[0-9a-f]{24}",value) else "")
except Exception:
 print("")
PY
)"
    rm -f "$response_file"
    if [ -z "$COMMENT_ID" ]; then
      printf '{"provider":"trello","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"comment post response was not confirmed; retry ensure_comment"}\n' "$ID"
    else
      printf '{"provider":"trello","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\n' "$ID"
    fi
    ;;

  transition)
    ID="${1:?usage: transition <id> <normalized-state>}"; TARGET="${2:?}"
    LID="$(list_id_for "$TARGET")"
    [ -n "$LID" ] || die "no Trello list mapped for normalized '$TARGET' (check state_map)"
    api PUT "cards/$ID" "idList=$LID" | python3 -c 'import sys,json; c=json.load(sys.stdin); print("ok "+c.get("id",""))'
    ;;

  create_board)
    NAME="${1:?usage: create_board <name> <ident> <desc>}"
    EXIST="$(api GET "members/me/boards" "fields=name" | NM="$NAME" python3 -c 'import sys,json,os
rows=json.load(sys.stdin); nm=os.environ["NM"].lower()
print(next((b["id"] for b in rows if b.get("name","").lower()==nm), ""))')"
    if [ -n "$EXIST" ]; then BID="$EXIST"; else
      BID="$(api POST "boards/" "name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$NAME")&defaultLists=true" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))')"
    fi
    [ -n "$BID" ] || die "create_board failed"
    api GET "boards/$BID" "fields=url" | BID="$BID" python3 -c 'import sys,json,os
b=json.load(sys.stdin); print(json.dumps({"board_id":os.environ["BID"],"board_url":b.get("url","")}))'
    ;;

  *) die "unknown op: $OP" ;;
esac
