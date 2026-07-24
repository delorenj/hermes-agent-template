#!/usr/bin/env python3
"""momo-unify-agent — apply the Momo/Hermes unification to a DEPLOYED agent.

Per role dir (agents/hermes/<role>) it:
  1. renders SOUL.md from momo/spec/momo-agent.spec.yaml × the agent's role.yaml
     identity (charter, tone, prime directives + WIP lease, memory hygiene, doctrine),
  2. honcho-neutralizes runtime/config.yaml (memory.provider='', disabled_toolsets
     += memory) so memory is the shared Hindsight bank only,
  3. installs the shared WIP=1 lease: copies .scripts/momo-wip-lock.py and wires
     .scripts/heartbeat.sh (if present) to acquire/release it around the reconcile pass.

Idempotent; dry-run by default (--apply mutates). This is the existing-fleet
counterpart to the copier template (future agents get the same via provisioning).
Needs PyYAML — run with ~/.hermes/hermes-agent/.venv/bin/python.

Usage: momo-unify-agent.py --role-dir <agents/hermes/pm> [--apply] [--spec P]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
try:
    import yaml
except ImportError:
    sys.exit("needs PyYAML — run with ~/.hermes/hermes-agent/.venv/bin/python")

SPEC_DEFAULT = Path("~/code/33GOD/momo/spec/momo-agent.spec.yaml").expanduser()
LOCK_SRC = Path("~/code/33GOD/momo/skill/scripts/momo-wip-lock.py").expanduser()

SOUL_TMPL = """# {display_name}

You are **{display_name}** — the Momo PM/EM orchestrator for the `{repo}`
repository, running as its autonomous **Hermes carrier**. You are the autonomous
twin of the human-drivable Momo; you share ONE board and ONE Hindsight bank with
it, so stay attributable and never split-brain the state.

<!-- Rendered from momo/spec/momo-agent.spec.yaml (role: {role}) × this repo's identity.
     Regenerate via momo-unify-agent.py / the Hermes adapter, not by hand. -->

## Identity

| | |
| --- | --- |
| Agent ID | `{agent_id}` |
| Repo | `{repo}` |
| Role | `{role}` |
| Telegram | {telegram} |
| Purpose | {purpose} |

## Scope

You operate only within the working directory of `{repo}`. Your HERMES_HOME is the
local runtime at `./runtime/`; Hermes loads its `config.yaml` directly.

## Tone

{tone}

## Role-specific behavior

{charter}

Prime directives (non-negotiable):

- **Never mutate code.** Every code change flows through a delegated worker.
- **WIP = 1**, shared with the human-drivable Momo via the driver lease
  (`.scripts/momo-wip-lock.py` → `runtime/wip-driver.lock`) — acquire before driving,
  back off if Momo holds it fresh; never double-drive one board.
- **Reviewer ≠ implementer** — independent adversarial review is the normal path.
- **Evidence over status** — a board column is a claim; repo evidence is proof.
- **Everything is an event** — record consequential calls as Bloodbank decision events.
- **Anti-stall** — never park a pass on operator sign-off.

Default execution: {default_execution}.

## Memory hygiene

Your durable memory is the shared **Hindsight bank `{repo}`** — one bank per PROJECT,
shared with the human-drivable Momo twin. Honcho and the per-agent `runtime/memories/`
store are **neutralized**: do not rely on `MEMORY.md`/`USER.md`. Retain with
`hindsight memory retain {repo} "…" --context <cat>`; recall with
`hindsight memory recall {repo} "…"`.

## Doctrine

Decide on the operator's behalf using **`~/code/33GOD/momo/PILLARS.md`** (canonical,
priority-ordered). This soul **references** that file; it does not copy it. Cite the
pillar(s) that drove a consequential call in its decision event.
"""

LEASE_BLOCK = '''# Coexistence WIP=1 lease (momo E2/S2.3): don't full-drive if the human-drivable
# Momo holds it. A crashed holder's lease expires (ttl) so the board is never wedged.
WIP_LOCK="$RUNTIME/wip-driver.lock"
if ! python3 "$ROLE_DIR/.scripts/momo-wip-lock.py" acquire "$WIP_LOCK" "hermes:$AGENT_ID" --ttl 3600 >/dev/null 2>&1; then
  printf '[heartbeat] WIP lease held by Momo — skipping full reconcile pass this tick\\n'
  maybe_checkpoint
  exit 0
fi
trap 'python3 "$ROLE_DIR/.scripts/momo-wip-lock.py" release "$WIP_LOCK" "hermes:$AGENT_ID" >/dev/null 2>&1 || true' EXIT

'''

SPAWN_ANCHOR = 'prompt="$(<"$PROMPT_FILE")"'


def load_role(role_dir: Path):
    """role.yaml at the role dir, else inside runtime/."""
    for p in (role_dir / "role.yaml", role_dir / "runtime" / "role.yaml"):
        if p.is_file():
            return yaml.safe_load(p.read_text()) or {}
    return None


def identity(r: dict, spec: dict) -> dict:
    repo = r.get("repo", "?"); role = r.get("role", "pm")
    agent_id = r.get("agent_id", f"{repo}-{role}")
    disp = r.get("display_name") or f"{repo.title()} {role.upper() if len(role) <= 3 else role.title()}"
    tel = (r.get("telegram") or {}).get("bot_username", "") if isinstance(r.get("telegram"), dict) else ""
    tone_key = r.get("soul_tone") or spec["personality"]["default_tone"]
    rs = spec["roles"].get(role, spec["roles"]["pm"])
    return {
        "repo": repo, "role": role, "agent_id": agent_id, "display_name": disp,
        "telegram": f"@{tel}" if tel else "—",
        "purpose": rs["purpose_template"].format(repo=repo),
        "tone": spec["personality"]["tones"].get(tone_key, spec["personality"]["tones"]["direct"]),
        "charter": rs.get("charter", "").strip(),
        "default_execution": rs.get("default_execution", "subagent-driven-development (WIP=1, spec + quality gates)"),
    }


def neutralize_config(cfg: Path, apply: bool) -> str:
    if not cfg.exists():
        return "no config.yaml (skip honcho)"
    base = yaml.safe_load(cfg.read_text()) or {}
    already = (isinstance(base.get("memory"), dict) and base["memory"].get("provider") == ""
               and "memory" in (base.get("agent", {}) or {}).get("disabled_toolsets", []))
    if already:
        return "honcho: already neutralized"
    overlay = {"memory": {"provider": "", "memory_enabled": False, "user_profile_enabled": False},
               "agent": {"disabled_toolsets": ["memory"]}}

    def merge(b, o):
        for k, v in o.items():
            if isinstance(b.get(k), dict) and isinstance(v, dict):
                merge(b[k], v)
            else:
                b[k] = v
    merge(base, overlay)
    if apply:
        cfg.with_suffix(cfg.suffix + ".pre-unify.bak").write_text(cfg.read_text()) if not cfg.with_suffix(cfg.suffix + ".pre-unify.bak").exists() else None
        yaml.safe_dump(base, cfg.open("w"), sort_keys=False)
    return "honcho: neutralized"


def install_lease(role_dir: Path, apply: bool) -> str:
    scripts = role_dir / ".scripts"
    msgs = []
    if not scripts.is_dir():
        return "no .scripts (skip lease)"
    dst = scripts / "momo-wip-lock.py"
    if not dst.exists() or dst.read_bytes() != LOCK_SRC.read_bytes():
        if apply:
            dst.write_bytes(LOCK_SRC.read_bytes()); dst.chmod(0o755)
        msgs.append("lock helper installed")
    else:
        msgs.append("lock helper present")
    hb = scripts / "heartbeat.sh"
    if not hb.is_file():
        msgs.append("no heartbeat.sh (reactive agent — lease unused)")
        return "; ".join(msgs)
    txt = hb.read_text()
    if "momo-wip-lock.py" in txt:
        msgs.append("heartbeat already lease-wired")
    elif SPAWN_ANCHOR in txt:
        if apply:
            hb.write_text(txt.replace(SPAWN_ANCHOR, LEASE_BLOCK + SPAWN_ANCHOR, 1))
        msgs.append("heartbeat lease-wired")
    else:
        msgs.append("WARN: heartbeat spawn anchor not found — wire manually")
    return "; ".join(msgs)


def unify(role_dir: Path, spec: dict, apply: bool, repo_override: str | None = None) -> None:
    r = load_role(role_dir)
    if r is None:
        if not repo_override:
            print(f"  SKIP: no role.yaml at {role_dir} (bare-layout agent — pass --repo to unify it)"); return
        r = {"repo": repo_override, "role": "pm", "agent_id": f"{repo_override}-pm"}
        print(f"  (synthesized identity for bare agent: {r['agent_id']})")
    idt = identity(r, spec)
    render = SOUL_TMPL.format(**idt)
    # Write BOTH the role-dir source AND the LIVE runtime/SOUL.md that hermes
    # actually loads (HERMES_HOME=runtime). Provisioning copies source->runtime,
    # but for an already-deployed agent we must update the live copy directly.
    for label, sp in (("src ", role_dir / "SOUL.md"),
                      ("live", role_dir / "runtime" / "SOUL.md")):
        if not sp.parent.exists():
            continue
        cur = sp.read_text() if sp.is_file() else ""
        if cur.strip() == render.strip():
            print(f"  SOUL[{label}]: already unified ({idt['agent_id']})")
        else:
            if apply:
                sp.write_text(render)
            print(f"  SOUL[{label}]: {'written' if apply else 'would write'} ({idt['agent_id']})")
    print("  " + neutralize_config(role_dir / "runtime" / "config.yaml", apply))
    print("  " + install_lease(role_dir, apply))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role-dir", required=True)
    ap.add_argument("--spec", default=str(SPEC_DEFAULT))
    ap.add_argument("--repo", default=None, help="synthesize identity for a bare agent with no role.yaml")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    spec = yaml.safe_load(Path(a.spec).read_text())
    role_dir = Path(a.role_dir).expanduser().resolve()
    print(f"== unify ({'APPLY' if a.apply else 'dry-run'}) :: {role_dir}")
    unify(role_dir, spec, a.apply, a.repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
