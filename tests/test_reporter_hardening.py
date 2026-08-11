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
            self.assertIn('if [[ "$ROLE" == "reporter" ]]', runtime_script)
            self.assertIn('"disabled_toolsets"', runtime_script)
            self.assertIn('"no_mcp"', runtime_script)
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

    def test_runtime_bootstrap_scans_before_scaffold_lands_in_runtime(self) -> None:
        # Runtimes are local and are never pushed to a per-agent remote, so the
        # invariant is no longer "scan before commit and push". The scaffold is
        # rendered into $TMP and then copied into $RUNTIME_LOCAL; the scan has to
        # happen while a leaked literal is still confined to the temp dir.
        text = (ROOT / "template" / ".scripts" / "20-runtime-repo.sh").read_text()
        scan_positions = [index for index in range(len(text)) if text.startswith("secret-scan.py", index)]
        self.assertTrue(scan_positions, "20-runtime-repo.sh must invoke secret-scan.py")
        self.assertLess(scan_positions[0], text.index('cp -an "$TMP/." "$RUNTIME_LOCAL/"'))

    def test_runtime_bootstrap_does_not_publish_runtime_to_a_remote(self) -> None:
        # Guards the FR4 boundary the reporter branch predated: provisioning must
        # not resurrect the per-agent runtime repo, in any role.
        text = (ROOT / "template" / ".scripts" / "20-runtime-repo.sh").read_text()
        for forbidden in ("git push -u origin main", "gh repo create"):
            self.assertNotIn(forbidden, text)

    def test_reporter_never_inherits_staging_env_credentials(self) -> None:
        text = (ROOT / "template" / ".scripts" / "20-runtime-repo.sh").read_text()
        self.assertIn("MIGRATE_FILES=(config.yaml profile.yaml)", text)
        self.assertIn("MIGRATE_FILES=(.env config.yaml)", text)


if __name__ == "__main__":
    unittest.main()
