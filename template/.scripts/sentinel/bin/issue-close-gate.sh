#!/usr/bin/env sh
# Provider-agnostic close gate. Verifies an issue's evidence file is complete
# before any closure (manual or autonomous). Pure gate: it reports PASS/FAIL on
# stdout/stderr and via the exit code, and publishes nothing.
#
# Usage: issue-close-gate.sh ISSUE_ID [REPO_ROOT]
set -eu

if [ "${1:-}" = "" ]; then
  printf 'Usage: %s ISSUE_ID [REPO_ROOT]\n' "$0" >&2
  exit 2
fi
ISSUE="$1"
case "$ISSUE" in *[!A-Za-z0-9_-]*) printf 'Invalid issue id: %s\n' "$ISSUE" >&2; exit 2 ;; esac

BIN_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROLE_YAML="$BIN_DIR/../../../role.yaml"
ROLE_REPO="$(sed -n 's/^repo:[[:space:]]*//p' "$ROLE_YAML" 2>/dev/null | head -n1 | tr -d '"' | tr -d '\r')"
ROOT="${2:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
cd "$ROOT"
ROOT="$(pwd -P)"
PROJECT_MANIFEST="$ROOT/.project.json"
if [ -e "$PROJECT_MANIFEST" ] || [ -L "$PROJECT_MANIFEST" ]; then
  [ -f "$PROJECT_MANIFEST" ] || {
    printf 'Project manifest is not a regular file: %s\n' "$PROJECT_MANIFEST" >&2
    exit 1
  }
  REPO_SLUG="$(python3 - "$PROJECT_MANIFEST" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    document = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"close gate: malformed project manifest {path}: {exc}")
slug = document.get("project_slug") if isinstance(document, dict) else None
if not isinstance(slug, str) or not slug.strip():
    raise SystemExit(f"close gate: project manifest {path} has no non-blank project_slug")
print(slug.strip())
PY
)" || exit 1
else
  REPO_SLUG="$(basename "$ROOT")"
fi
if [ -n "$ROLE_REPO" ] && [ "$ROLE_REPO" != "$REPO_SLUG" ]; then
  printf 'Installed role repo %s disagrees with target project slug %s.\n' "$ROLE_REPO" "$REPO_SLUG" >&2
  exit 1
fi

FILE="_bmad-output/implementation-artifacts/issue-evidence/$ISSUE.md"
FAIL=0
check() { grep -q "$1" "$FILE" || { printf 'Missing required evidence: %s\n' "$2" >&2; FAIL=1; }; }

if [ ! -f "$FILE" ]; then
  printf 'Missing issue evidence file: %s\n' "$FILE" >&2
  exit 1
fi

check '^## Issue' 'Issue'
check '^## Acceptance Criteria' 'Acceptance Criteria'
check '^## Repo Changes' 'Repo Changes'
check '^## Verification' 'Verification'
check '^## Ledger Update' 'Ledger Update'
check '^## Known Gaps' 'Known Gaps'
check '^## Close Recommendation' 'Close Recommendation'

if grep -Eiq 'TBD|TODO|not run|pending|unknown' "$FILE"; then
  printf 'Evidence file still contains unresolved placeholders or unverified work.\n' >&2
  FAIL=1
fi
grep -q 'Ledger updated: yes' "$FILE" || { printf 'Ledger update is not marked yes.\n' >&2; FAIL=1; }
grep -q 'Close recommendation: ready' "$FILE" || { printf 'Close recommendation is not ready.\n' >&2; FAIL=1; }

if [ "$FAIL" -ne 0 ]; then
  printf '\nCLOSE GATE: FAIL for %s\n' "$ISSUE" >&2
  exit 1
fi

printf 'CLOSE GATE: PASS for %s (repo: %s)\n' "$ISSUE" "$REPO_SLUG"
