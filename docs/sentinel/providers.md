# Provider adapters

This guide is the reference for the ticket-provider abstraction: the contract
every supported adapter implements, the two adapters that ship today, and a
step-by-step process for adding a new provider or verifying an existing one
against a live board. Read [Architecture](architecture.md) first for how the
adapter fits into the engine.

## The contract

Each adapter is a standalone POSIX shell script under
`template/.scripts/providers/<name>.sh`. It reads its credentials from the
environment and its board binding from `.project.json` `ticket_provider`.
`role.yaml` holds a compatibility mirror, not provider-selection authority. It
takes an operation as its first argument and writes JSON or a status string to
standard output.

The dispatcher, `template/.scripts/lib/ticket-provider.sh`, resolves the active
provider and routes calls. Source it, then call `tp <operation> [args...]`:

```bash
. .scripts/lib/ticket-provider.sh
tp active_milestone
tp list_issues
tp transition DEL-42 completed
```

Every adapter must implement these operations.

| Operation | Arguments | Output |
| --- | --- | --- |
| `resolve` | none | JSON `{provider, board_id, board_url}`. Validates credentials and board binding. |
| `active_milestone` | none | JSON `{id, name, state}` for the date-current milestone, cycle, or board; Plane evaluates calendar dates in `ticket_provider.timezone` and returns empty ids with `state: inactive` when none is current. |
| `list_issues` | none | JSON array of `{id, key, title, state, state_type, updated_at, assignee, url, ...}`. Plane also includes `active_milestone_id`, `active_milestone_name`, and `in_active_milestone` on every project-wide row. |
| `get_issue` | `<issue-ref>` | JSON `{id, key, title, description, acceptance, state, state_type, comments, attachments}`. |
| `comment` | `<issue-ref> <body>` | Resolves an id or human key, then prints the new comment id. |
| `transition` | `<issue-ref> <normalized-state>` | Resolves an id or human key, then moves the issue to a normalized state. |
| `create_board` | `<name> <ident> <desc>` | JSON `{board_id, board_url}`. Creates or reuses the board. |
| `describe_board` | `<workspace> <board_id>` | JSON `{board_id, identifier, workspace, name}`. Read-only lookup against an explicit workspace. `identifier` is whatever the provider itself reports — empty when the provider mints none (Trello) — and is never a locally-computed guess. |
| `create_issue` | `[--if-absent] <title> [description]` | JSON `{issue_id, key, issue_url, created}`. Files a new ticket on the bound board. |

The `transition` operation accepts only the normalized states from
[Architecture: normalized states](architecture.md#normalized-states):
`backlog`, `unstarted`, `started`, `in_review`, `completed`, and `cancelled`. The adapter
maps each to its back end's concrete state.

The dispatcher resolves human issue keys to provider-native ids through the
normalized `list_issues` contract before `get_issue`, `comment`, or `transition`.
An absent or ambiguous reference fails before a write reaches the provider.
Plane transitions resolve the exact configured state name and group, PATCH it,
then read the issue back; only an exact state-id match produces `ok`.
Plane cycle boundaries are project-calendar dates, not UTC dates. Configure an
IANA name in `ticket_provider.timezone`; an empty value falls back to
`TICKET_PROVIDER_TIMEZONE`, then `TZ`, then the host's local timezone. The
adapter contains no customer-specific timezone literal.

Plane list endpoints are followed through every page (`next_page_results` /
`next_cursor`); a board answer never silently stops at page one. Reads are
idempotent and retry HTTP 429 a bounded number of times
(`PLANE_READ_MAX_ATTEMPTS`, default 4), sleeping the server's `Retry-After`
capped by `PLANE_429_MAX_DELAY` (default 30s). Mutations are never retried at
the transport layer: `transition` reads the live issue back after every PATCH
and only repeats the PATCH when the read-back proves the previous one did not
land (`PLANE_MUTATION_MAX_ATTEMPTS`, default 3). A transition whose read-back
never confirms the intended state fails explicitly and never claims completion.

The `create_issue` operation is the write counterpart to `list_issues`: it lets
an orchestrator file a ticket for a gap it discovered instead of only reading
and updating the board. The board or team it files against always comes from the
resolved binding — it is never an argument — exactly as it does for
`create_board`.

Unlike `create_board`, `create_issue` is **not** idempotent by default. Two
issues can legitimately share a title, so the adapter never silently collapses
them; every plain call creates a new ticket and reports `"created": true`. An
automated caller that may re-run the same request passes `--if-absent`, which
first looks for an existing issue whose title matches exactly
(case-insensitively) and returns that one with `"created": false` instead of
filing a duplicate. Callers should branch on `created` rather than assume it.

## The adapters that ship today

The repository supports Plane and Trello. A historical Linear binding may
remain in `.project.json`, but the dispatcher refuses it before loading an
adapter or making a request.

- **Plane** (`providers/plane.sh`) uses the Plane REST API with `X-API-Key`
  authentication. A Plane project maps to the board, a cycle maps to the
  milestone, and `state.group` maps to the state type. It's verified live. Note
  the v1 API returns an issue's `state` as a bare UUID with no `state_detail`,
  so the adapter joins each issue against the project's states map; descriptions
  come from `description_html`. `get_issue` also hydrates attachment metadata
  from Plane's issue-attachments endpoint so triage can inspect attachments
  before scoping work. `list_issues` remains project-wide and annotates exact
  membership in the date-current cycle; callers must not present the whole
  project list as the visible current-cycle board.
- **Trello** (`providers/trello.sh`) uses the Trello REST API with `key` and
  `token` query-parameter authentication. A board maps to both the project and
  the milestone, a list maps to the state, and a card maps to the issue. It's
  implemented but not yet verified against a live board. `create_issue` files
  the new card into the `backlog` list, falling back to `unstarted`.

Both supported adapters implement `create_issue`: Plane uses
`POST projects/<board>/issues/`; Trello uses `POST cards`.

### Credentials and binding by provider

Set credentials in the environment and the board binding in `.project.json`.
The provisioner mirrors supported values into `role.yaml` for legacy readers.

<details>
<summary>Plane</summary>

- Credentials: `PLANE_API_KEY`. Endpoint: `PLANE_BASE` (default
  `https://plane.delo.sh`).
- Binding:

  ```yaml
  ticket_provider:
    name: plane
    workspace: <workspace-slug>
    project: <project-uuid> # set by 42-ticket-provider.sh
  ```

</details>

<details>
<summary>Trello</summary>

- Credentials: `TRELLO_KEY` and `TRELLO_TOKEN`.
- Binding:

  ```yaml
  ticket_provider:
    name: trello
    board: <board-id> # set by 42-ticket-provider.sh
    state_map: { backlog: "Backlog", started: "In Progress", completed: "Done" }
  ```

</details>

## Adding a provider

This is future extension guidance, not an advertised third provider. To add
a provider later, first change the supported-provider allowlists and tests.

1. Create `template/.scripts/providers/<name>.sh`. Start from `plane.sh`
   for a REST API with a single key.
2. Implement every operation in [the contract](#the-contract). Return the exact
   JSON shapes shown in the table. Map your back end's states to the normalized states.
3. Fail fast and clean. Validate credentials at the top of the script, before
   any pipe, so a missing key prints one error line instead of a stack trace.
   The existing adapters call a `need_key` helper for this.
4. Add the provider to the `ticket_provider` question in `copier.yml` so it
   appears as a choice.
5. Extend `template/role.yaml.jinja` with a provider-specific binding block
   under the `{% if ticket_provider == '<name>' %}` branch.
6. Teach `42-ticket-provider.sh` and the dispatcher how to resolve or create
   the board for your provider and admit it explicitly.
7. Validate and verify with the steps below.

<!-- prettier-ignore -->
> [!WARNING]
> Don't put escaped double quotes inside a single-quoted `python3 -c '...'`
> block. A single-quoted shell string keeps backslashes literal, so `\"`
> reaches Python as a backslash-quote and fails to parse. Use a `python3 -
> <<'PY'` heredoc, or hoist values into variables, when you need quotes.

## Validating an adapter offline

Before you touch a live board, confirm the adapter parses and fails cleanly.

1. Check shell syntax:

   ```bash
   sh -n template/.scripts/providers/<name>.sh
   ```

2. Check the inline Python blocks. Extract each `<<'PY'` heredoc and each
   `python3 -c '...'` block and run it through `python3 -c "import ast;
   ast.parse(open('block').read())"`, or use the small extraction script in
   [Development guide: validating
   locally](development.md#validating-changes-locally).

3. Confirm a missing credential produces one clean error line and a non-zero
   exit, with no Python traceback:

   ```bash
   ( unset PLANE_API_KEY; sh template/.scripts/providers/plane.sh resolve )
   ```

## Testing provider reads against a disposable board

When you have credentials, exercise the read operations against a disposable board.
The following pattern stages a minimal role directory and runs the adapter. It
never closes or modifies a ticket, but it does make live read requests.

```bash
# Use only disposable boards and provider-specific credentials; never print keys.
# 1. Stage a role directory and repo manifest bound to the disposable board.
T="$(mktemp -d "${TMPDIR:?}/tp-verify.XXXXXX")"; RD="$T/agents/hermes/pm"
mkdir -p "$RD/.scripts/lib" "$RD/.scripts/providers"
cp template/.scripts/lib/ticket-provider.sh "$RD/.scripts/lib/"
cp template/.scripts/providers/trello.sh "$RD/.scripts/providers/"
python3 - "$T/.project.json" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({"ticket_provider": {"type": "trello", "board_id": "disposable-board-id"}}))
PY

# 2. Run the read operations after setting TRELLO_KEY and TRELLO_TOKEN.
LIB="$RD/.scripts/lib/ticket-provider.sh"
bash -c '. "$1"; tp resolve' _ "$LIB"
bash -c '. "$1"; tp list_issues' _ "$LIB" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)),"issues")'
```

To verify `transition` and `comment`, which modify the board, use a disposable
test ticket. Don't run write operations against production tickets during
verification.

<!-- prettier-ignore -->
> [!NOTE]
> The dispatcher needs Bash because it reads `${BASH_SOURCE[0]}` to find the
> providers directory. The adapters themselves are POSIX shell and run under
> `sh`. Run the dispatcher with `bash -c`, as shown above, not `sh -c`.

## Read next

- [Development guide](development.md): the full edit, validate, and propagate
  workflow, plus the open roadmap, which includes live-verifying Plane and
  Trello.
