"""An executable resolver-shaped runtime for disposable provisioning tests."""
from pathlib import Path
import sys


def install_supported_runtime(runtime: Path) -> None:
    for directory in ("agent", "tools", "hermes_cli", ".venv/bin"):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    (runtime / "hermes_cli/config_defaults.py").write_text(
        "DEFAULT_CONFIG = {'skills': {'project_discovery': True, 'trusted_project_dirs': []}}\n"
    )
    (runtime / "agent/skill_utils.py").write_text('''import os
from pathlib import Path
import yaml

def _load_raw_config():
    return yaml.safe_load((Path(os.environ["HERMES_HOME"]) / "config.yaml").read_text()) or {}

def find_project_root(start=None):
    cur = Path(start or os.environ.get("TERMINAL_CWD") or os.getcwd()).resolve()
    for _ in range(64):
        if (cur / ".git").exists():
            return None if cur == Path.home().resolve() else cur
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None

def is_project_root_trusted(root):
    return Path(root).resolve() in {
        Path(p).resolve() for p in _load_raw_config().get("skills", {}).get("trusted_project_dirs", [])
    }

def get_project_skills_dirs():
    if _load_raw_config().get("skills", {}).get("project_discovery") is False:
        return []
    root = find_project_root()
    if root is None or not is_project_root_trusted(root):
        return []
    skill_dir = root / ".agents/skills"
    return [skill_dir] if skill_dir.is_dir() else []

def get_scan_ordered_skills_dirs():
    return [*get_project_skills_dirs(), Path(os.environ["HERMES_HOME"]) / "skills"]
''')
    (runtime / "tools/skills_tool.py").write_text('''import json
from agent.skill_utils import get_scan_ordered_skills_dirs

def skill_view(name, file_path=None, task_id=None, preprocess=True):
    for directory in get_scan_ordered_skills_dirs():
        candidate = directory / name / (file_path or "SKILL.md")
        if candidate.is_file():
            return json.dumps({"success": True, "name": name, "_source_path": str(candidate), "content": candidate.read_text()})
    return json.dumps({"success": False, "error": "missing skill"})

def _find_all_skills():
    names = []
    for directory in get_scan_ordered_skills_dirs():
        for p in sorted(directory.glob("*/SKILL.md")):
            if p.parent.name not in names:
                names.append(p.parent.name)
    return [{"name": name} for name in names]
''')
    binary = runtime / ".venv/bin/hermes"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    python = runtime / ".venv/bin/python"
    if not python.exists():
        python.symlink_to(sys.executable)


def supported_case(case):
    install_supported_runtime(case["runtime"])
    (case["project"] / ".git").mkdir()
    for directory in (case["project"] / ".agents/skills/proof", case["profile"] / "skills/proof"):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: proof\ndescription: Test project precedence.\n---\nFixture.\n")
    return case
