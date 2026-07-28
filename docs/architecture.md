# Architecture

## Two-artifact split

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
                  │     └── runtime/    ────────────┼─── git submodule
                  └────────────────────────────────┘     │
                                                          ▼
                  ┌────────────────────────────────────────┐
                  │  gh:delorenj/agent-hm-<project>-<role> │ ← NEW repo per agent
                  │  ───────────────────────────────────── │   private
                  │     ├── config.yaml                    │
                  │     ├── SOUL.md (evolving)             │
                  │     ├── memories/                      │   auto-checkpointed
                  │     ├── sessions/sessions.db  (LFS)    │   by the heartbeat
                  │     ├── decisions/                     │   + on session end
                  │     └── bloodbank-consumer.py          │
                  └────────────────────────────────────────┘
```

## Why two artifacts, not one

The **template** is the contract / the bootstrap recipe — it doesn't change
when an agent learns something. The **runtime** is the agent's accumulating
state — it changes every conversation. Bundling them would mean every memory
update churns the template's commit log; separating them means:

- The template repo is small, stable, easy to update fleet-wide
- The runtime repo is per-agent, fast-moving, auditable in isolation
- You can fork an agent (branch the runtime repo) without touching others
- You can wipe an agent (delete the runtime repo) without affecting the template

A third file ties the fleet together: `~/.hermes/fleet.env`.
It is the single source-of-truth pointer for the shared Hermes executable/repo
that every generated launcher uses.

## Why git-tracked runtime

The runtime is the agent's "subjective experience" — its memory of every
conversation, the SOUL refinements it has absorbed, the decisions it has
emitted. Putting it in git gives:

- **Durability**: nothing lost when the host dies. `git clone` restores it.
- **Auditability**: `git log` is a full trace of how the agent evolved.
- **Reversibility**: if the agent develops bad habits, `git revert` rolls back.
- **Forkability**: experiment with a copy on a branch, merge if it works out.
- **Cross-machine**: same agent state on big-chungus and on the laptop.

## Heartbeat cadence (reconcile + checkpoint)

A systemd `--user` timer runs `.scripts/heartbeat.sh` frequently (about once a
minute). Each tick fuses two jobs into one:

1. **Board-reconciliation sentinel pass** — the PM's continuous ticket sentinel.
   The runner's own cooldown/lock logic decides whether a full, LLM-backed
   reconciliation pass is worth running (it rate-limits the expensive Hermes
   call); see [the sentinel docs](sentinel/README.md).
2. **Gated runtime checkpoint** — after the sentinel decision, the runner calls
   `.scripts/checkpoint.sh`, gated to at most once an hour. The checkpoint:
   `cd`s into the runtime submodule, `git add -A`, commits only if dirty (exits
   clean otherwise), and pushes to `origin`.

On session end, a hermes hook (TBD path) checkpoints immediately so nothing
in-flight is lost between heartbeat ticks.

Sensitive state — `.env`, `auth.json`, OAuth tokens — never enters git.
They're in `.gitignore` and live only on the host machine.

## One bot per agent (Telegram)

Each agent gets its own BotFather bot and runs its own gateway daemon.
Hermes' `gateway/status.py:acquire_scoped_lock(scope="telegram", identity=<token>)`
already enforces "one token per gateway process" — so even if two profiles
happen to share a token, the second one's startup fails fast. The N×M cost
(N BotFather sessions per fleet) is the price we accept for zero custom
routing code.

## One Plane project per agent

A Plane "project" is the natural unit of work isolation. Mixing agents into a
shared project would conflate decisions and break filters. 1:1 also makes
archive-on-retire clean.

## Bloodbank wiring

Each consumer subscribes to two lanes:
- `bloodbank.evt.v1.repo.>` — canonical repo-domain events, filtered by `data.repo`
- `bloodbank.cmd.v1.agent.>` — canonical agent-domain commands, filtered by `data.target_agent_id`

Each agent emits CloudEvents 1.0 envelopes with `actor.agent_id`,
`producer = hermes-agent:<id>`, `source = hermes://agent/<id>`. The naming
contract is owned by Bloodbank (`~/code/33GOD/bloodbank/docs/event-naming.md`).
Repo and agent identifiers belong in envelope data, actor, or source fields,
never in type or subject tokens.
