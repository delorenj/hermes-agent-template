#!/usr/bin/env sh
# Plane ticket-provider adapter.
#
# Credentials:  PLANE_API_KEY   (X-API-Key header)
# Endpoint:     PLANE_BASE       (default https://plane.delo.sh)
# Board binding (role.yaml `ticket_provider:`):
#   name: plane
#   workspace: <workspace-slug>      (or env PLANE_WORKSPACE)
#   project:   <project-uuid>        (set by create_board / 42-ticket-provider)
#   state_map: { in_review: "In Review", completed: "Done" }   optional
#
# Plane model:  project = board, cycle = milestone, state.group in
#   backlog|unstarted|started|completed|cancelled.
#
# NOTE: REST paths follow Plane's v1 public API. Verify against a live board on
# first use; state/cycle naming varies per workspace.
set -eu

OP="${1:-}"; shift 2>/dev/null || true
ROLE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ROLE_YAML="$ROLE_DIR/role.yaml"
BASE="${PLANE_BASE:-https://plane.delo.sh}"

die() { echo "plane: $*" >&2; exit 1; }
need_key() { [ -n "${PLANE_API_KEY:-}" ] || die "PLANE_API_KEY is not set"; }

tp_cfg() {
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
# SOT), walking up from the role dir. This is preferred over role.yaml so all of
# a repo's agents resolve to the same board.
pj_cfg() {
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

# Board binding: .project.json (SOT) first, then role.yaml, then env.
WS="$(pj_cfg workspace)"; [ -n "$WS" ] || WS="$(tp_cfg workspace)"; WS="${WS:-${PLANE_WORKSPACE:-}}"
PROJ="$(pj_cfg board_id)"; [ -n "$PROJ" ] || PROJ="$(tp_cfg project)"
SM_IN_REVIEW="$(tp_cfg in_review)"; SM_IN_REVIEW="${SM_IN_REVIEW:-In Review}"
SM_DONE="$(tp_cfg completed)"; SM_DONE="${SM_DONE:-Done}"
API="$BASE/api/v1/workspaces/$WS"

# api METHOD PATH [JSON_BODY] — call Plane REST, print response body.
api() {
  need_key
  method="$1"; path="$2"; body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" "$API/$path" \
      -H "X-API-Key: $PLANE_API_KEY" -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -fsS -X "$method" "$API/$path" -H "X-API-Key: $PLANE_API_KEY"
  fi
}

urlencode() {
  python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

# plane_pages_find PATH MODE NEEDLE
# Exhaust every cursor page. Return 0 when MODE finds NEEDLE, 1 when the full
# collection was read and no match exists, and 2 on lookup/pagination failure.
plane_pages_find() {
  path="$1"; mode="$2"; needle="$3"; cursor=""; page_count=0
  while :; do
    request_path="$path"
    case "$request_path" in *\?*) request_path="${request_path}&per_page=100" ;; *) request_path="${request_path}?per_page=100" ;; esac
    [ -z "$cursor" ] || request_path="${request_path}&cursor=$(urlencode "$cursor")"
    if ! page="$(api GET "$request_path" 2>/dev/null)"; then
      return 2
    fi
    match="$(printf '%s' "$page" | MODE="$mode" NEEDLE="$needle" python3 -c 'import json,os,sys
try:
    data=json.load(sys.stdin)
except Exception:
    print("invalid"); raise SystemExit(0)
rows=data.get("results",[]) if isinstance(data,dict) else data if isinstance(data,list) else []
mode=os.environ["MODE"]; needle=os.environ["NEEDLE"]
if mode=="comment":
    found=any(needle in str(row.get("comment_html","")) for row in rows)
else:
    def refs(row):
        values=[str(row.get("id","")),str(row.get("sequence_id",""))]
        ident=str(row.get("identifier",""))
        if ident: values.append(ident)
        project_identifier=str(row.get("project_identifier",""))
        if project_identifier and row.get("sequence_id") is not None:
            values.append(project_identifier+"-"+str(row["sequence_id"]))
        return values
    lookup=needle.rsplit("-",1)[-1] if "-" in needle and needle.rsplit("-",1)[-1].isdigit() else needle
    found=any(lookup.casefold() in [v.casefold() for v in refs(row)] for row in rows)
print("found" if found else "absent")')"
    [ "$match" != "invalid" ] || return 2
    [ "$match" != "found" ] || return 0
    next="$(printf '%s' "$page" | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin)
 if not isinstance(d,dict): print("__done__")
 elif not d.get("next_page_results",False): print("__done__")
 else: print(d.get("next_cursor","") or "__invalid__")
except Exception:
 print("__invalid__")')"
    [ "$next" != "__invalid__" ] || return 2
    [ "$next" != "__done__" ] || return 1
    [ "$next" != "$cursor" ] || return 2
    cursor="$next"; page_count=$((page_count + 1))
    [ "$page_count" -lt 10000 ] || return 2
  done
}

# Plane's current work-item comment API documents limit/offset pagination,
# unlike the cursor-paginated work-item collection.
plane_comments_find() {
  work_item_id="$1"; marker="$2"; limit=100; offset=0; page_count=0
  while :; do
    path="projects/$PROJ/work-items/$work_item_id/comments/?limit=$limit&offset=$offset"
    if ! page="$(api GET "$path" 2>/dev/null)"; then
      return 2
    fi
    state="$(printf '%s' "$page" | MARKER="$marker" LIMIT="$limit" OFFSET="$offset" python3 -c 'import json,os,sys
try:
 data=json.load(sys.stdin)
except Exception:
 print("invalid"); raise SystemExit(0)
rows=data.get("results",[]) if isinstance(data,dict) else data if isinstance(data,list) else None
if not isinstance(rows,list):
 print("invalid"); raise SystemExit(0)
marker=os.environ["MARKER"]; limit=int(os.environ["LIMIT"]); offset=int(os.environ["OFFSET"])
if any(marker in str(row.get("comment_html","")) for row in rows if isinstance(row,dict)):
 print("found")
elif isinstance(data,dict) and isinstance(data.get("total_results"),int):
 print("absent" if offset+len(rows)>=data["total_results"] else "more:"+str(offset+len(rows)))
elif len(rows)<limit:
 print("absent")
else:
 print("more:"+str(offset+len(rows)))')"
    case "$state" in
      found) return 0 ;;
      absent) return 1 ;;
      more:*)
        next="${state#more:}"
        [ -n "$next" ] && [ "$next" != "$offset" ] || return 2
        offset="$next"; page_count=$((page_count + 1))
        [ "$page_count" -lt 10000 ] || return 2
        ;;
      *) return 2 ;;
    esac
  done
}

canonical_uuid() {
  python3 - "$1" <<'PY'
import re, sys, unicodedata, uuid
raw=unicodedata.normalize("NFKC",sys.argv[1]).strip().casefold()
if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",raw):
    raise SystemExit(1)
try:
    value=str(uuid.UUID(raw))
except ValueError:
    raise SystemExit(1)
if raw != value:
    raise SystemExit(1)
print(value)
PY
}

# Map a normalized state -> a concrete Plane state id in this project.
resolve_state_id() {
  want="$1"
  [ -n "$PROJ" ] || die "ticket_provider.project not set"
  case "$want" in
    completed) grp=completed; nm="$SM_DONE" ;;
    in_review) grp=started;   nm="$SM_IN_REVIEW" ;;
    started)   grp=started;   nm="" ;;
    unstarted) grp=unstarted; nm="" ;;
    backlog)   grp=backlog;   nm="" ;;
    *) die "invalid normalized state: $want" ;;
  esac
  api GET "projects/$PROJ/states/" | GRP="$grp" NM="$nm" python3 -c 'import sys,json,os
d=json.load(sys.stdin); rows=d.get("results", d if isinstance(d,list) else [])
grp=os.environ["GRP"]; nm=os.environ.get("NM","")
named=[s for s in rows if nm and (s.get("name","").lower()==nm.lower())]
grouped=[s for s in rows if s.get("group")==grp]
pick=(named or grouped or [{}])[0]
print(pick.get("id",""))'
}

# All Plane ops require the API key; fail fast and clean before any pipe.
need_key

case "$OP" in
  resolve)
    [ -n "$WS" ] || die "workspace not set (role.yaml ticket_provider.workspace or PLANE_WORKSPACE)"
    [ -n "$PROJ" ] || die "project not set (run 42-ticket-provider.sh)"
    printf '{"provider":"plane","board_id":"%s","board_url":"%s/%s/projects/%s/issues/"}\n' \
      "$PROJ" "$BASE" "$WS" "$PROJ"
    ;;

  active_milestone)
    [ -n "$PROJ" ] || die "project not set"
    api GET "projects/$PROJ/cycles/" | python3 -c 'import sys,json,datetime
d=json.load(sys.stdin); rows=d.get("results", d if isinstance(d,list) else [])
now=datetime.datetime.now(datetime.timezone.utc)
def cur(c):
    s,e=c.get("start_date"),c.get("end_date")
    return bool(s and e and s<=now.date().isoformat()<=e)
active=[c for c in rows if cur(c)] or rows
m=active[0] if active else {}
print(json.dumps({"id":m.get("id",""),"name":m.get("name",""),"state":"active" if active else ""}))'
    ;;

  list_issues)
    [ -n "$PROJ" ] || die "project not set"
    # Plane v1 returns issue.state as a bare UUID, so join against the states map.
    STATES="$(api GET "projects/$PROJ/states/")"
    ISSUES="$(api GET "projects/$PROJ/issues/")"
    printf '%s\n%s\n' "$STATES" "$ISSUES" | BASE="$BASE" WS="$WS" PROJ="$PROJ" python3 -c 'import sys,json,os
parts=sys.stdin.read().split("\n",1)
srows=json.loads(parts[0] or "{}"); srows=srows.get("results", srows if isinstance(srows,list) else [])
smap={s.get("id"):(s.get("name",""),s.get("group","")) for s in srows}
d=json.loads(parts[1] or "{}"); rows=d.get("results", d if isinstance(d,list) else [])
base,ws,proj=os.environ["BASE"],os.environ["WS"],os.environ["PROJ"]
out=[]
for n in rows:
    iid=n.get("id","")
    name,group=smap.get(n.get("state",""),("",""))
    out.append({"id":iid,"key":n.get("sequence_id",iid),
                "title":n.get("name",""),"state":name,"state_type":group,
                "updated_at":n.get("updated_at",""),"assignee":"",
                "url":base+"/"+ws+"/projects/"+proj+"/issues/"+str(iid)})
print(json.dumps(out))'
    ;;

  get_issue)
    ID="${1:?usage: get_issue <id>}"
    STATES="$(api GET "projects/$PROJ/states/")"
    ISSUE="$(api GET "projects/$PROJ/issues/$ID/")"
    COMM="$(api GET "projects/$PROJ/issues/$ID/comments/" 2>/dev/null || echo '[]')"
    printf '%s\n%s\n%s\n' "$STATES" "$ISSUE" "$COMM" | python3 -c 'import sys,json,re
parts=sys.stdin.read().split("\n",2)
srows=json.loads(parts[0] or "{}"); srows=srows.get("results", srows if isinstance(srows,list) else [])
smap={s.get("id"):(s.get("name",""),s.get("group","")) for s in srows}
i=json.loads(parts[1] or "{}"); c=json.loads(parts[2] or "[]")
rows=c.get("results", c if isinstance(c,list) else [])
def strip(h): return re.sub(r"<[^>]+>","",h or "").strip()
name,group=smap.get(i.get("state",""),("",""))
desc=strip(i.get("description_html",""))
cs=[{"id":x.get("id",""),"body":strip(x.get("comment_html","")),"author":""} for x in rows]
print(json.dumps({"id":i.get("id",""),"key":i.get("sequence_id",""),"title":i.get("name",""),
                  "description":desc,"acceptance":desc,
                  "state":name,"state_type":group,"comments":cs}))'
    ;;

  comment)
    ID="${1:?usage: comment <id> <body>}"; BODY="${2:?}"
    api POST "projects/$PROJ/issues/$ID/comments/" \
      "$(python3 -c 'import json,sys; print(json.dumps({"comment_html":"<p>"+sys.argv[1]+"</p>"}))' "$BODY")" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))'
    ;;

  resolve_issue_id)
    REF="${1:?usage: resolve_issue_id <reference>}"
    NORMALIZED="$(python3 - "$REF" <<'PY'
import re,sys,unicodedata
value=unicodedata.normalize("NFKC",sys.argv[1]).strip()
if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",value)
        or any(unicodedata.category(c).startswith("C") for c in value)):
    raise SystemExit(1)
print(value)
PY
)" || die "invalid issue reference"
    ID=""
    if DIRECT="$(api GET "projects/$PROJ/work-items/$NORMALIZED/" 2>/dev/null)"; then
      ID="$(printf '%s' "$DIRECT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
    fi
    if [ -z "$ID" ]; then
      # The caller should normally pass the canonical id from list_issues. This
      # exhaustive fallback safely resolves sequence/key references.
      if plane_pages_find "projects/$PROJ/work-items/" issue "$NORMALIZED"; then
        cursor=""; page_count=0
        while :; do
          path="projects/$PROJ/work-items/?per_page=100"
          [ -z "$cursor" ] || path="${path}&cursor=$(urlencode "$cursor")"
          PAGE="$(api GET "$path")" || die "issue lookup failed"
          ID="$(printf '%s' "$PAGE" | NEEDLE="$NORMALIZED" python3 -c 'import json,os,sys
d=json.load(sys.stdin); rows=d.get("results",[]) if isinstance(d,dict) else d if isinstance(d,list) else []
n=os.environ["NEEDLE"].casefold()
lookup=n.rsplit("-",1)[-1] if "-" in n and n.rsplit("-",1)[-1].isdigit() else n
for row in rows:
 vals=[str(row.get("id","")),str(row.get("sequence_id","")),str(row.get("identifier",""))]
 p=str(row.get("project_identifier",""))
 if p and row.get("sequence_id") is not None: vals.append(p+"-"+str(row["sequence_id"]))
 if lookup in [v.casefold() for v in vals if v]:
  print(row.get("id","")); break')"
          [ -z "$ID" ] || break
          cursor="$(printf '%s' "$PAGE" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("next_cursor","") if d.get("next_page_results",False) else "")')"
          [ -n "$cursor" ] || break
          page_count=$((page_count + 1)); [ "$page_count" -lt 10000 ] || die "issue pagination failed"
        done
      else
        rc=$?
        [ "$rc" -eq 1 ] || die "issue lookup failed"
      fi
    fi
    CANONICAL="$(canonical_uuid "$ID" 2>/dev/null || true)"
    [ -n "$CANONICAL" ] || die "issue not found or provider returned a non-canonical UUID"
    printf '%s\n' "$CANONICAL"
    ;;

  ensure_comment)
    ID="${1:?usage: ensure_comment <canonical-id> <marker> <body>}"
    MARKER="${2:?usage: ensure_comment <canonical-id> <marker> <body>}"
    BODY="${3:?usage: ensure_comment <canonical-id> <marker> <body>}"
    CANONICAL="$(canonical_uuid "$ID" 2>/dev/null || true)"
    [ "$CANONICAL" = "$ID" ] || die "ensure_comment requires a canonical Plane issue UUID"
    printf '%s\n%s\n' "$MARKER" "$BODY" | python3 -c 'import re,sys
marker,body=sys.stdin.read().split("\n",1)
marker=marker.rstrip("\n"); body=body.rstrip("\n")
if not re.fullmatch(r"\[run-retro-comment:[0-9a-f]{64}\]",marker) or body.count(marker)!=1:
 raise SystemExit(1)' || die "invalid comment marker/body"
    if plane_comments_find "$ID" "$MARKER"; then
      printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\n' "$ID"
      exit 0
    else
      rc=$?
      if [ "$rc" -eq 2 ]; then
        printf '{"provider":"plane","status":"failed","target_issue":"%s","error_category":"lookup_failed","error_summary":"comment lookup failed; no post attempted"}\n' "$ID"
        exit 0
      fi
    fi
    PAYLOAD="$(python3 - "$BODY" <<'PY'
import html,json,sys
body="<p>"+html.escape(sys.argv[1]).replace("\n","<br>")+"</p>"
print(json.dumps({"comment_html":body},separators=(",",":")))
PY
)"
    if ! RESPONSE="$(api POST "projects/$PROJ/work-items/$ID/comments/" "$PAYLOAD" 2>/dev/null)"; then
      printf '{"provider":"plane","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"comment post response was not confirmed; retry ensure_comment"}\n' "$ID"
      exit 0
    fi
    COMMENT_ID="$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id",""))
except Exception: print("")')"
    if [ -z "$COMMENT_ID" ]; then
      printf '{"provider":"plane","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"comment post response was not confirmed; retry ensure_comment"}\n' "$ID"
    else
      printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\n' "$ID"
    fi
    ;;

  transition)
    ID="${1:?usage: transition <id> <normalized-state>}"; TARGET="${2:?}"
    SID="$(resolve_state_id "$TARGET")"
    [ -n "$SID" ] || die "no Plane state for normalized '$TARGET'"
    api PATCH "projects/$PROJ/issues/$ID/" "$(printf '{"state":"%s"}' "$SID")" \
      | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok "+str(d.get("sequence_id","")) )'
    ;;

  create_board)
    NAME="${1:?usage: create_board <name> <ident> <desc>}"; IDENT="${2:-}"; DESC="${3:-}"
    [ -n "$WS" ] || die "workspace not set"
    EXIST="$(api GET "projects/?per_page=200" | NAME="$NAME" IDENT="$IDENT" python3 -c 'import sys,json,os
d=json.load(sys.stdin); rows=d.get("results", d if isinstance(d,list) else [])
name=os.environ["NAME"].strip().lower(); ident=os.environ["IDENT"].upper()
# Repo NAME is the primary key — links an existing repo board even if its
# identifier differs (Plane does not enforce unique names, so this prevents
# duplicate boards). Fall back to identifier match; empty -> create new.
pid=next((p["id"] for p in rows if str(p.get("name","")).strip().lower()==name), "")
if not pid and ident:
    pid=next((p["id"] for p in rows if (p.get("identifier") or "").upper()==ident), "")
print(pid)')"
    if [ -n "$EXIST" ]; then PID="$EXIST"; else
      PID="$(api POST "projects/" \
        "$(python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1],"identifier":sys.argv[2],"description":sys.argv[3]}))' "$NAME" "$IDENT" "$DESC")" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))')"
    fi
    [ -n "$PID" ] || die "create_board failed"
    printf '{"board_id":"%s","board_url":"%s/%s/projects/%s/issues/"}\n' "$PID" "$BASE" "$WS" "$PID"
    ;;

  *) die "unknown op: $OP" ;;
esac
