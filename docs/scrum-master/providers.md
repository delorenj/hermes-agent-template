# Provider adapters

This guide is the reference for the ticket-provider abstraction: the contract
every adapter implements, the three adapters that ship today, and a
step-by-step process for adding a new provider or verifying an existing one
against a live board. Read [Architecture](architecture.md) first for how the
adapter fits into the engine.

## The contract

Each adapter is a standalone POSIX shell script under
`template/.scripts/providers/<name>.sh`. It reads its credentials from the
environment and its board binding from `role.yaml` under `ticket_provider`. It
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
| `active_milestone` | none | JSON `{id, name, state}` for the current milestone, cycle, or board. |
| `list_issues` | none | JSON array of `{id, key, title, state, state_type, updated_at, assignee, url}`. |
| `get_issue` | `<id>` | JSON `{id, key, title, description, acceptance, state, state_type, comments}`. |
| `comment` | `<id> <body>` | Prints the new comment id. |
| `transition` | `<id> <normalized-state>` | Moves the issue to a normalized state. |
| `create_board` | `<name> <ident> <desc>` | JSON `{board_id, board_url}`. Creates or reuses the board. |

The `transition` operation accepts only the normalized states from
[Architecture: normalized states](architecture.md#normalized-states):
`backlog`, `unstarted`, `started`, `in_review`, and `completed`. The adapter
maps each to its back end's concrete state.

## The adapters that ship today

The repository includes three adapters with different verification status.

- **Linear** (`providers/linear.sh`) is the reference implementation. It uses
  the Linear GraphQL API. It's verified live against a real board. Linear's
  `issue(id:)` field accepts both the UUID and the human identifier (for
  example, `DEL-42`), so the engine can pass identifiers through.
- **Plane** (`providers/plane.sh`) uses the Plane REST API with `X-API-Key`
  authentication. A Plane project maps to the board, a cycle maps to the
  milestone, and `state.group` maps to the state type. It's implemented but not
  yet verified against a live board.
- **Trello** (`providers/trello.sh`) uses the Trello REST API with `key` and
  `token` query-parameter authentication. A board maps to both the project and
  the milestone, a list maps to the state, and a card maps to the issue. It's
  implemented but not yet verified against a live board.

### Credentials and binding by provider

Set credentials in the environment and the board binding in `role.yaml`.

<details>
<summary>Linear</summary>

- Credentials: `LINEAR_API_KEY`.
- Binding:

  ```yaml
  ticket_provider:
    name: linear
    team: DEL # Linear team key
    project: "" # optional, scopes milestone and issue queries
  ```

</details>

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

To add a fourth provider, for example GitHub Issues or Jira, follow these steps.

1. Create `template/.scripts/providers/<name>.sh`. Start from `linear.sh` if the
   back end uses GraphQL, or `plane.sh` if it uses REST with a single API key.
2. Implement every operation in [the contract](#the-contract). Return the exact
   JSON shapes shown in the table. Map your back end's states to the five
   normalized states.
3. Fail fast and clean. Validate credentials at the top of the script, before
   any pipe, so a missing key prints one error line instead of a stack trace.
   The existing adapters call a `need_key` helper for this.
4. Add the provider to the `ticket_provider` question in `copier.yml` so it
   appears as a choice.
5. Extend `template/role.yaml.jinja` with a provider-specific binding block
   under the `{% if ticket_provider == '<name>' %}` branch.
6. Teach `42-ticket-provider.sh` how to resolve or create the board for your
   provider, if it differs from the existing `linear`, `plane`, and `trello`
   cases.
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
   ( unset LINEAR_API_KEY; sh template/.scripts/providers/linear.sh resolve )
   ```

## Verifying an adapter against a live board

When you have credentials, exercise the read operations against a real board.
The following pattern stages a minimal role directory and runs the adapter. It
never closes or modifies a ticket, so it's safe to run.

```bash
# 1. Load the credential into the environment only (never print it).
export LINEAR_API_KEY="$(grep -E '^(export )?LINEAR_API_KEY=' \
  ~/.hermes/<agent>.env | head -1 | sed -E 's/^(export )?LINEAR_API_KEY=//; s/^"//; s/"$//')"

# 2. Stage a role directory bound to the real board.
T=$(mktemp -d); RD="$T/agents/hermes/scrum-master"
mkdir -p "$RD/.scripts/lib" "$RD/.scripts/providers"
cp template/.scripts/lib/ticket-provider.sh "$RD/.scripts/lib/"
cp template/.scripts/providers/linear.sh "$RD/.scripts/providers/"
printf 'repo: demo\nrole: scrum-master\nticket_provider:\n  name: linear\n  team: DEL\n' \
  > "$RD/role.yaml"

# 3. Run the read operations.
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
