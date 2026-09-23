# Architecture

## Tracked role and local runtime split

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│   gh:delorenj/hermes-agent-template       ← Copier template (this repo)  │
│   ─────────────────────────────────       used once per role            │
│                                                                          │
│        copier copy ... ./agents/hermes/<role>                           │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                  ┌────────────────────────────────┐
                  │  your-project/                  │
                  │   agents/hermes/<role>/         │  ← in YOUR project git
                  │     ├── role.yaml               │
                  │     ├── SOUL.md                 │
                  │     ├── hermes (launcher)       │
                  │     ├── .scripts/               │
                  │     └── runtime/                        │ ← ignored owned state
                  │          ├── config.yaml                │
                  │          ├── memories/                  │
                  │          ├── sessions/                  │
                  │          └── .env                       │
                  └─────────────────────────────────────────┘
```

## Why tracked role and local state are separate

The **template** is the contract / the bootstrap recipe — it doesn't change
when an agent learns something. The ignored **runtime directory** is the
agent's accumulating local state — it changes every conversation. Separating
them means:

- The template repo is small, stable, easy to update fleet-wide
- Project commits cannot accidentally publish runtime credentials or sessions
- Each agent uses a real named HERMES_HOME under `~/.hermes/profiles/`; Flume
  links the profile's owned state back to runtime and renders its config from
  the fleet base plus a per-profile delta
- Provisioning can refresh tracked launchers and scaffolds without overwriting
  existing local state

A third file ties the fleet together: `~/.hermes/fleet.env`.
It is the single source-of-truth pointer for the shared Hermes executable/repo
that every generated launcher uses.

The template never rewrites the named profile. `.scripts/20-runtime-repo.sh`
delegates that topology to `flume remediate hermes.runtime-singleton` (dry-run
audit, then idempotent apply). This prevents a stale local bootstrap from
replacing a real named profile with the legacy profile-to-runtime symlink.

The named profile's `config.yaml` is generated —
`deep_merge(~/.hermes/config.yaml, <profile>/config.delta.yaml)` — so every line
in it is either fleet truth or an override someone had to justify in the delta.
A project-specific `terminal.cwd` is neither, so provisioning never writes one.
The manual wrapper, the gateway unit, and the credential launcher instead export
`TERMINAL_CWD` as a process-local value resolved from the role's Git root.

## Per-agent gateway route and secret references

`role.yaml` may set `model.name`, `provider`, `base_url`, `api_mode`, and
`key_env`. The generated systemd gateway launcher translates only those
non-secret values into explicit `hermes gateway run` flags. Session `/model`
and channel overrides still take precedence. `key_env` is a variable name,
never a key value.

For systemd user services, an optional encrypted model credential named
`<agent>-model-api-key.cred` under `~/.config/hermes-agent/credentials/` may be
loaded with `LoadCredentialEncrypted`. Chat-channel credentials are stored in
1Password and resolved natively by Hermes from the named profile's
`secrets.onepassword.env` mapping. Raw chat tokens are forbidden in every
dotenv file, including ignored `runtime/.env`.

## Durability boundary

Ignored local state is not automatically durable. The operator must configure
an encrypted filesystem backup or snapshot for each exact runtime path. A
project clone restores only tracked role files and the empty scaffold.
Hindsight retains only memories/events explicitly written to its bank, while a
secret manager retains only credentials explicitly stored there; neither is a
complete runtime backup. See [Operations](operations.md#back-up-and-restore-an-agent).

## Board-reconciliation cadence

There is no per-agent heartbeat timer. One used to run `.scripts/heartbeat.sh`
about once a minute on every agent; it was retired on 2026-09-17 because the
reconciliation pass behind it is gated on `role.yaml`'s `reconcile.enabled`,
true in exactly one repo fleet-wide, so ~20,000 ticks a day fell through to a
no-op. `70-systemd.sh` now removes any heartbeat unit it finds, and the gateway
is the only per-agent unit. The three jobs the timer appeared to do are owned
elsewhere:

- **liveness** — the gateway unit's `Restart=on-failure`
- **scheduling** — Bloodbank; krebs pull-subscribes
  `bloodbank.cmd.lifecycle.task.invoke`
- **persistence** — krebs attempts/leases, which park a stalled ticket in
  "Needs Attention" instead of ticking forever

`.scripts/heartbeat.sh` still renders into every role and is still the
board-reconciliation sentinel's entrypoint — invoked on demand rather than on a
timer. Its own cooldown/lock logic still decides whether a full, LLM-backed
reconciliation pass is worth running; see [the sentinel
docs](sentinel/README.md).

Sensitive state — `.env`, `auth.json`, OAuth tokens — never enters project Git.
It lives only in ignored local storage unless the operator separately places a
credential in the secret manager or includes the runtime in an encrypted
filesystem backup.

## One bot per agent (Telegram)

Each agent gets its own BotFather bot and runs its own gateway daemon.
The BotFather token is an invocation-only provisioning input: shared
`fleet.env` may carry the non-secret allow-list policy but is never allowed to
supply `TELEGRAM_BOT_TOKEN`. Provisioning verifies `getMe`, rejects a token or
bot identity already owned anywhere in the local fleet, atomically writes the
credential in 1Password, maps only its `op://` reference into the named profile,
and records safe identity metadata in `role.yaml` and the registry. Hermes'
scoped runtime lock remains a second line of defense against duplicate pollers.

## One app and bot per Slack-enabled agent

Slack is opt-in and remains deferred for newly provisioned agents unless the
operator explicitly enables it or supplies both required tokens. An enabled
agent owns one dedicated `xapp-` Socket Mode token and one dedicated `xoxb-`
bot token; provisioning rejects token reuse and a verified bot identity already
owned by another registry entry.

The bot token is verified through Slack's read-only `auth.test` endpoint.
Credentials live only in 1Password; the named profile contains `op://`
references under `secrets.onepassword.env`. The shared `~/.hermes/.env`,
`fleet.env`, ignored `runtime/.env`, `role.yaml`, and fleet registry never
contain Slack tokens. The non-secret allowed-user policy may be inherited from
fleet config.

## One Plane project per agent

A Plane "project" is the natural unit of work isolation. Mixing agents into a
shared project would conflate decisions and break filters. 1:1 also makes
archive-on-retire clean.

## Bloodbank wiring

Bloodbank command ingress is owned by one fleet-shared official Hermes gateway,
not by a consumer in every runtime. Each registry entry advertises:

```yaml
bloodbank:
  enabled: true        # default on; absent also means enabled; false quarantines
  gateway_scope: fleet
  target_agent_id: <agent-id>
```

The shared gateway subscribes once, resolves `data.target_agent_id` through the
fleet registry, and routes the turn into that Hermes profile. Each profile's own
messaging gateway remains independent; there is no per-profile NATS process,
systemd consumer unit, or filesystem inbox bridge.

No key means enabled. New roles render `bloodbank.enabled: true`, and an
ABSENT `bloodbank.enabled` in `role.yaml` or in the registry row also means
enabled: `80-registry.sh` projects it as `true`, flume's parity rules read it as
`true`, and the fleet gateway routes it. Only an explicit YAML `false`
quarantines an agent; provisioning preserves that explicit value. A present
value that is not a strict YAML boolean (`yes`, `"true"`, `1`) is invalid:
`80-registry.sh` refuses it without touching the registry and the gateway
treats the row as disabled and logs an ERROR. (Until 2026-09-22 an absent key
read as `false`; an accidental re-provision that dropped the key silently
disabled 33god-pm's grooming, which is why the default flipped.)

Each agent emits CloudEvents 1.0 envelopes with `actor.agent_id`,
`producer = hermes-agent:<id>`, `source = hermes://agent/<id>`. The naming
contract is owned by Bloodbank (`~/code/33GOD/bloodbank/docs/event-naming.md`).
Repo and agent identifiers belong in envelope data, actor, or source fields,
never in type or subject tokens.

The gateway uses the canonical lifecycle already defined by those schemas:

- `bloodbank.conversation.turn.started`
- `bloodbank.agent.invocation.started`
- one terminal invocation event: `bloodbank.agent.invocation.completed` or
  `bloodbank.agent.invocation.failed`
- `bloodbank.conversation.turn.completed`

The type carries no version token. Schema revision is tracked out of band, in
`dataschema` / `schemaref`; a breaking payload change earns a new action or
entity, never a `v<n>` segment in the type or the subject.

There are no separate `received` or `accepted` lifecycle events. A JetStream
command is acknowledged only after Hermes processing completion and terminal
event publication.
