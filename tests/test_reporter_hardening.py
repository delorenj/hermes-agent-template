from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = ROOT / "template" / ".scripts" / "secret-scan.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("secret_scan", SCANNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReporterTemplateHardeningTests(unittest.TestCase):
    def test_reporter_render_is_delta_only_and_gateway_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "render"
            subprocess.run(
                [
                    "copier",
                    "copy",
                    "--skip-tasks",
                    "--defaults",
                    "--trust",
                    "-d",
                    "target_repo=delonet-company",
                    "-d",
                    "role=reporter",
                    "-d",
                    "model_provider=openai-codex",
                    "-d",
                    "model_name=gpt-5.4",
                    str(ROOT),
                    str(target),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            systemd_script = (target / ".scripts" / "70-systemd.sh").read_text()
            runtime_script = (target / ".scripts" / "20-runtime-repo.sh").read_text()
            ignore = (target / ".runtime-scaffold" / ".gitignore").read_text()
            self.assertIn('[[ "$ROLE" == "reporter" ]]', systemd_script)
            self.assertNotIn('cp "$HOME/.hermes/config.yaml"', runtime_script)
            self.assertIn("delta-only runtime config", runtime_script)
            self.assertIn(".env.*", ignore)
            self.assertIn(".scripts/.done-*", (target / ".gitignore").read_text())

    def test_secret_scanner_rejects_literal_and_accepts_references(self) -> None:
        scanner = load_scanner()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "safe.json").write_text(
                json.dumps({"api_key": "openrouter", "secret_env": "PLANE_API_KEY"})
            )
            self.assertEqual(scanner.findings(root), [])
            (root / "unsafe.yaml").write_text("secret: literal-sensitive-value\n")
            self.assertTrue(scanner.findings(root))

    def test_runtime_bootstrap_scans_before_commit_and_push(self) -> None:
        text = (ROOT / "template" / ".scripts" / "20-runtime-repo.sh").read_text()
        scan_positions = [index for index in range(len(text)) if text.startswith("secret-scan.py", index)]
        self.assertGreaterEqual(len(scan_positions), 3)
        self.assertLess(scan_positions[0], text.index("git add -A"))
        self.assertLess(scan_positions[-1], text.index("git push -u origin main"))


if __name__ == "__main__":
    unittest.main()
