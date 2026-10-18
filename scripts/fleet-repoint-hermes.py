#!/usr/bin/env python3
"""Repoint every deployed Hermes launcher at the ONE install (surgical; idempotent).

Why a separate tool and not ``fleet-sync.sh --apply``: fleet-sync reconciles a whole role directory (profile
config, TTS, loader assets, units) and reports ~200 unrelated drift items on this host. Repointing the runtime
default must not drag any of that along. This changes exactly one thing: any embedded default that names a
``releases/<sha>`` checkout (or a dev checkout's venv) becomes the shared install's launcher.

It edits, per deployed role directory (``agents/hermes/<role>/``):

    hermes                         HERMES_BIN="${HERMES_BIN:-<default>}"
    .scripts/_lib.sh               config_get fleet.hermes_bin / fleet.hermes_repo defaults
    .scripts/01-config.sh          the last-resort [fleet] block
    .scripts/config.example.toml   hermes_bin / hermes_repo

Usage:
    fleet-repoint-hermes.py                 # report what would change
    fleet-repoint-hermes.py --apply         # write
    fleet-repoint-hermes.py --apply --commit # also commit (path-limited) and push each touched repo
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
RELEASE_RE = r"(?:\$HOME|~|" + re.escape(str(HOME)) + r")/\.local/share/hermes-agent/releases/[0-9a-f]{7,40}"
NEW_BIN = "$HOME/.hermes/hermes-agent/.hermes/bin/hermes"
NEW_REPO = "$HOME/.hermes/hermes-agent"
TARGETS = ("hermes", ".scripts/_lib.sh", ".scripts/01-config.sh", ".scripts/config.example.toml")


def rewrite(text: str, name: str) -> str:
    # The binary: <release>/.venv/bin/hermes  ->  the shared launcher. Preserve the leading spelling style.
    def bin_sub(m: re.Match) -> str:
        lead = m.group(0)
        if lead.startswith("~"):
            return "~/.hermes/hermes-agent/.hermes/bin/hermes"
        if lead.startswith("$HOME"):
            return NEW_BIN
        return str(HOME / ".hermes/hermes-agent/.hermes/bin/hermes")

    def repo_sub(m: re.Match) -> str:
        lead = m.group(0)
        if lead.startswith("~"):
            return "~/.hermes/hermes-agent"
        if lead.startswith("$HOME"):
            return NEW_REPO
        return str(HOME / ".hermes/hermes-agent")

    text = re.sub(RELEASE_RE + r"/\.venv/bin/hermes", bin_sub, text)
    text = re.sub(RELEASE_RE + r"(?![/\w.-])", repo_sub, text)
    if name == "hermes":
        # The embedded last-resort default only (never the env/config chain above it).
        text = re.sub(
            r'(HERMES_BIN="\$\{HERMES_BIN:-)\$HOME/code/hermes-agent/[^}\n]*(\}")',
            r"\1" + NEW_BIN.replace("\\", "\\\\") + r"\2",
            text,
        )
    return text


def role_dirs() -> list[Path]:
    found: set[Path] = set()
    code = HOME / "code"
    for pattern in ("*/agents/hermes/*", "*/*/agents/hermes/*"):
        for p in code.glob(pattern):
            if (p / "hermes").is_file():
                found.add(p)
    return sorted(found)


def repo_root(path: Path) -> Path | None:
    out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--commit", action="store_true", help="commit (path-limited) and push each touched repo")
    args = ap.parse_args()

    touched: dict[Path, list[Path]] = {}
    for rd in role_dirs():
        for rel in TARGETS:
            f = rd / rel
            if not f.is_file() or f.is_symlink():
                continue
            try:
                old = f.read_text(encoding="utf-8")
            except OSError:
                continue
            new = rewrite(old, rel)
            if new == old:
                continue
            print(f"{'WRITE' if args.apply else 'DRIFT'}  {f.relative_to(HOME)}")
            if args.apply:
                mode = f.stat().st_mode
                f.write_text(new, encoding="utf-8")
                f.chmod(mode)
            root = repo_root(rd)
            if root:
                touched.setdefault(root, []).append(f)
    if args.apply and args.commit:
        for root, files in sorted(touched.items()):
            rels = [str(f.relative_to(root)) for f in files]
            tracked = [r for r in rels if subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", r], capture_output=True, check=False).returncode == 0]
            if not tracked:
                print(f"SKIP   {root}: touched files are untracked/ignored ({', '.join(rels)})")
                continue
            msg = "chore(hermes): launcher defaults point at the ONE shared install (~/.hermes/hermes-agent)\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>"
            subprocess.run(["git", "-C", str(root), "add", "--", *tracked], check=False)
            c = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg, "--", *tracked], capture_output=True, text=True, check=False)
            print(f"COMMIT {root}: rc={c.returncode} {c.stdout.strip()[:100]}{c.stderr.strip()[:200]}")
            if c.returncode == 0:
                p = subprocess.run(["git", "-C", str(root), "push", "-q"], capture_output=True, text=True, check=False)
                print(f"PUSH   {root}: rc={p.returncode} {p.stderr.strip()[:200]}")
    if not touched:
        print("fleet-repoint-hermes: nothing to change")
    return 0 if args.apply or not touched else 1


if __name__ == "__main__":
    sys.exit(main())
