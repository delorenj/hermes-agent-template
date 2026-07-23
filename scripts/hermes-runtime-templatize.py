#!/usr/bin/env python3
"""hermes-runtime-templatize — dedup a runtime's skills onto the shared
hermes-base pack, at PER-SKILL granularity, with scoped-name overrides.

Why per-skill: 14 of the 18 base dirs are CATEGORY dirs (apple, creative,
github, …) holding 73 sub-skills total. hermes keys skill identity on each
SKILL.md's frontmatter `name:` (and the containing dir name), so dedup and
override must happen at the sub-skill level, not the category-dir level.

Policy (fleet decision, 2026-07-23):
  * a base sub-skill IDENTICAL to the pack  -> delete the local copy (it then
    resolves read-only from the pack via config.yaml skills.external_dirs).
  * a base sub-skill that was OVERWRITTEN   -> SCOPED-NAME override: rename its
    dir AND its frontmatter `name:` to `<name>-<slug>`, promoting it from a
    shared-base skill to an unambiguous agent-scoped copy. The pristine base
    stays in the pack for every other agent; no collision, no data loss.
  * an agent-ADDED skill (not in the pack)  -> left untouched in the overlay.
Then the pack is wired into config.yaml (if not already). Empty category dirs
left after deletions are pruned.

A runtime whose config has no skills.external_dirs (bare-layout agents) is
BLOCKED (can't resolve deleted skills from the pack) and left untouched.

Dry-run by default; --apply mutates. Idempotent (a second run finds nothing to
do). `verify` checks a runtime is name-disjoint from the pack.

Usage:
  hermes-runtime-templatize.py dedup  [--apply] [--root DIR] [--slug S] [--pack DIR]
  hermes-runtime-templatize.py verify [--root DIR] [--pack DIR]
"""
from __future__ import annotations
import argparse, hashlib, os, re, shutil, sys
from pathlib import Path

DEFAULT_PACK = Path("/home/delorenj/code/skillex/packs/hermes-base/0.18.2")

# Mirror hermes agent/skill_utils.py: only these dirs are pruned from skill
# scanning, and a SKILL.md inside a skill's support dir is progressive-
# disclosure data, not an active skill root. Keeping this identical means we
# classify EXACTLY the skills hermes treats as active (never .archive, .git, …).
EXCLUDED_SKILL_DIRS = frozenset((
    ".archive", ".git", ".github", ".hub", ".mypy_cache", ".nox", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "__pycache__", "node_modules", "site-packages", "venv",
))
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def iter_skill_roots(skills_dir: Path):
    """Yield each ACTIVE SKILL.md path (exact copy of hermes iter_skill_index_files)."""
    matches = []
    for root, dirs, files in os.walk(str(skills_dir)):
        has = "SKILL.md" in files
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS and not (has and d in SKILL_SUPPORT_DIRS)]
        if "SKILL.md" in files:
            matches.append(os.path.join(root, "SKILL.md"))
    for p in sorted(matches):
        yield Path(p)


def treehash(d: Path) -> str:
    lines = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            lines.append(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + str(p.relative_to(d)))
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def frontmatter_name(smd: Path) -> str:
    try:
        t = smd.read_text(encoding="utf-8")
    except Exception:
        return smd.parent.name
    m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
    if m:
        mm = re.search(r"^name:\s*(.+?)\s*$", m.group(1), re.M)
        if mm:
            return mm.group(1).strip().strip("'\"")
    return smd.parent.name


def set_frontmatter_name(smd: Path, newname: str) -> None:
    t = smd.read_text(encoding="utf-8")
    m = re.match(r"^(---\s*\n)(.*?)(\n---)", t, re.S)
    if m:
        head, fm, fence = m.group(1), m.group(2), m.group(3)
        if re.search(r"^name:\s*.+$", fm, re.M):
            fm = re.sub(r"^name:\s*.+$", f"name: {newname}", fm, count=1, flags=re.M)
        else:
            fm = f"name: {newname}\n" + fm
        smd.write_text(head + fm + fence + t[m.end():], encoding="utf-8")
    else:
        smd.write_text(f"---\nname: {newname}\n---\n" + t, encoding="utf-8")


def pack_index(pack: Path) -> dict[str, str]:
    """frontmatter-name -> treehash of the skill's own dir, across the pack."""
    idx: dict[str, str] = {}
    for smd in iter_skill_roots(pack):
        idx[frontmatter_name(smd)] = treehash(smd.parent)
    return idx


def runtimes(root: Path):
    base = root / "agents" / "hermes"
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*/runtime") if p.is_dir())


def config_wireable(cfg: Path, pack: Path) -> tuple[bool, bool]:
    """(wireable, already). wireable=False for a config with no external_dirs."""
    if not cfg.is_file():
        return (False, False)
    txt = cfg.read_text(encoding="utf-8")
    if str(pack) in txt:
        return (True, True)
    return (bool(re.search(r"^\s*external_dirs:", txt, re.M)), False)


def wire(cfg: Path, pack: Path, apply: bool) -> bool:
    ok, already = config_wireable(cfg, pack)
    if already:
        return True
    if not ok:
        return False
    if not apply:
        return True
    lines = cfg.read_text(encoding="utf-8").splitlines()
    out, i, done = [], 0, False
    while i < len(lines):
        m = re.match(r"^(\s*)external_dirs:\s*(.*)$", lines[i])
        if not done and m:
            indent, inline = m.group(1), m.group(2).strip()
            if inline == "":                      # block form: keep existing items, append ours
                out.append(lines[i])
                j, item_indent = i + 1, None
                while j < len(lines) and lines[j].strip().startswith("- "):
                    if item_indent is None:
                        item_indent = len(lines[j]) - len(lines[j].lstrip())
                    out.append(lines[j]); j += 1
                out.append(" " * (item_indent if item_indent is not None else indent) + "- " + str(pack))
                done, i = True, j
                continue
            else:                                 # inline form ([] or [a, b]) -> convert to block
                inner = inline.strip("[]").strip()
                items = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                out.append(f"{indent}external_dirs:")
                for it in items:
                    out.append(f"{indent}- {it}")
                out.append(f"{indent}- {pack}")
                done, i = True, i + 1
                continue
        out.append(lines[i]); i += 1
    if not done:
        return False
    cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def prune_empty_categories(sk: Path, apply: bool) -> int:
    """Remove immediate child dirs of skills/ that hold no SKILL.md anymore."""
    n = 0
    for child in sorted(p for p in sk.iterdir() if p.is_dir()):
        if not any(child.rglob("SKILL.md")):
            n += 1
            if apply:
                shutil.rmtree(child)
    return n


def dedup(root: Path, slug: str, pack: Path, apply: bool) -> None:
    idx = pack_index(pack)
    for rt in runtimes(root):
        sk = rt / "skills"
        print(f"-- {rt}")
        if not sk.is_dir():
            print("   no skills/ — nothing to do"); continue
        ok, already = config_wireable(rt / "config.yaml", pack)
        if not ok:
            print("   BLOCKED: config.yaml has no skills.external_dirs (bare-layout) — provision a skills block first; untouched")
            continue
        ident, scoped, adds = [], [], []
        for smd in iter_skill_roots(sk):
            d = smd.parent
            name = frontmatter_name(smd)
            if name in idx:
                (ident if treehash(d) == idx[name] else scoped).append((d, name))
            else:
                adds.append(name)
        print(f"   classify (per-skill): base-identical={len(ident)}  overwritten={len(scoped)}  agent-adds={len(adds)}")
        if not apply:
            for d, name in scoped:
                print(f"   [would] scoped-name override: {d.relative_to(sk)} -> name '{name}-{slug}'")
            for d, name in ident:
                print(f"   [would] rm {d.relative_to(sk)} (identical → pack)")
            print(f"   [would] wire pack into config.yaml{' (already)' if already else ''}")
            continue
        if not wire(rt / "config.yaml", pack, True):
            print("   ABORT: wire failed — leaving skills untouched"); continue
        for d, name in scoped:
            newdir = d.parent / f"{d.name}-{slug}"
            if newdir.exists():
                print(f"   SKIP scoped rename (target exists): {newdir.relative_to(sk)}"); continue
            set_frontmatter_name(d / "SKILL.md", f"{name}-{slug}")
            d.rename(newdir)
        for d, name in ident:
            shutil.rmtree(d)
        pruned = prune_empty_categories(sk, True)
        print(f"   reconciled: {len(ident)} identical removed, {len(scoped)} scoped→*-{slug}, {pruned} empty categories pruned; base resolves from pack")


def verify(root: Path, pack: Path) -> int:
    idx = pack_index(pack)
    bad = 0
    for rt in runtimes(root):
        sk = rt / "skills"
        if not sk.is_dir():
            continue
        for smd in iter_skill_roots(sk):
            name = frontmatter_name(smd)
            if name in idx and treehash(smd.parent) != idx[name]:
                print(f"   NOT DISJOINT: {rt.name}: local '{name}' shares a pack name but differs (scope-rename it)")
                bad += 1
            if name in idx and treehash(smd.parent) == idx[name]:
                print(f"   REDUNDANT: {rt.name}: local '{name}' is an identical copy of the pack (delete it)")
                bad += 1
    print("verify:", "CLEAN ✓ (runtime is name-disjoint from the pack)" if bad == 0 else f"{bad} issue(s)")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("dedup", "verify"):
        s = sub.add_parser(name)
        s.add_argument("--root", default=".")
        s.add_argument("--pack", default=str(DEFAULT_PACK))
        if name == "dedup":
            s.add_argument("--apply", action="store_true")
            s.add_argument("--slug", default=None, help="agent scope token (default: repo dir basename, lowercased)")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    pack = Path(a.pack)
    if not (pack / "SKILL.md").exists() and not any(pack.rglob("SKILL.md")):
        print(f"ERROR: no skills under pack {pack}", file=sys.stderr); return 2
    if a.cmd == "verify":
        return verify(root, pack)
    slug = (a.slug or root.name).lower()
    mode = "APPLY" if a.apply else "dry-run"
    print(f"== templatize dedup ({mode}) :: {root} :: slug={slug} :: pack={pack} ==")
    dedup(root, slug, pack, a.apply)
    print("\nRESULT:", "applied where safe (BLOCKED runtimes untouched)." if a.apply
          else "dry-run. --apply to reconcile. Bare-config runtimes are reported BLOCKED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
