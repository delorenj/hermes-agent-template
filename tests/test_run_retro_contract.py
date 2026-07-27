import concurrent.futures
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "template" / ".scripts" / "sentinel" / "bin" / "run-retro.py"
SCHEMA_PATH = (
    ROOT / "template" / ".scripts" / "sentinel" / "schemas" / "run-retro.v4.schema.json"
)
PROMPT_PATH = ROOT / "template" / ".scripts" / "sentinel.prompt.md.jinja"
DOC_PATH = (
    ROOT
    / "template"
    / ".scripts"
    / "sentinel"
    / "docs"
    / "continuous-ticket-orchestration.md"
)
ADAPTER_PATH = ROOT / "template" / ".scripts" / "lib" / "ticket-provider.sh"
PLANE_PATH = ROOT / "template" / ".scripts" / "providers" / "plane.sh"
ISSUE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ISSUE_ID = "22222222-2222-4222-8222-222222222222"


def load_helper():
    spec = importlib.util.spec_from_file_location("hermes_run_retro", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RETRO = load_helper()


def base_intent(run_id="00000000-0000-4000-8000-000000000001", source=ISSUE_ID):
    return {
        "run_id": run_id,
        "correlation_id": run_id,
        "source_issue": source,
        "local_tracking_reference": None,
        "decisions": {
            "what_hurt": {"category": "testing", "summary": "Slow fixture setup"},
            "what_should_change": {
                "category": "automation",
                "summary": "Cache safe fixtures",
            },
            "fix_scope": "repo-local",
        },
        "protected_evidence_refs": ["evidence:retro-1"],
        "sanitization": {
            "status": "sanitized",
            "omitted_categories": ["raw_logs"],
        },
    }


class RepoFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = pathlib.Path(self.temp.name)
        (self.root / ".project.json").write_text(
            json.dumps(
                {
                    "project_name": "ＰＪＡＮＧＬＥＲ",
                    "ticket_provider": {"type": "PLANE"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def close(self):
        self.temp.cleanup()

    def artifact(self, fingerprint):
        return (
            self.root
            / "_bmad-output"
            / "implementation-artifacts"
            / "run-retros"
            / f"{fingerprint}.json"
        )


class RunRetroSerialContractTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()

    def tearDown(self):
        self.repo.close()

    def test_1_crash_after_prepared_intent_reuses_same_run_same_content(self):
        first = RETRO.prepare(self.repo.root, base_intent())
        second = RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["artifact_fingerprint"], second["artifact_fingerprint"])
        document = RETRO.read_artifact(
            self.repo.artifact(first["artifact_fingerprint"])
        )
        self.assertEqual(document["routing"]["status"], "prepared")

    def test_2_same_run_changed_content_stalls_without_overwrite(self):
        first = RETRO.prepare(self.repo.root, base_intent())
        path = self.repo.artifact(first["artifact_fingerprint"])
        original = path.read_bytes()
        changed = base_intent()
        changed["decisions"]["what_hurt"]["summary"] = "Different safe summary"
        with self.assertRaisesRegex(RETRO.RetroError, "immutable_intent_mismatch"):
            RETRO.prepare(self.repo.root, changed)
        self.assertEqual(path.read_bytes(), original)

    def test_3_cross_run_identical_content_has_distinct_artifacts_shared_marker(self):
        first = RETRO.prepare(self.repo.root, base_intent())
        second = RETRO.prepare(
            self.repo.root,
            base_intent("00000000-0000-4000-8000-000000000002"),
        )
        self.assertNotEqual(
            first["artifact_fingerprint"], second["artifact_fingerprint"]
        )
        self.assertEqual(
            first["comment_fingerprint_marker"],
            second["comment_fingerprint_marker"],
        )

    def test_4_cross_run_different_content_has_distinct_artifacts_and_markers(self):
        first = RETRO.prepare(self.repo.root, base_intent())
        changed = base_intent("00000000-0000-4000-8000-000000000002")
        changed["decisions"]["what_should_change"]["summary"] = "Use isolated fixtures"
        second = RETRO.prepare(self.repo.root, changed)
        self.assertNotEqual(
            first["artifact_fingerprint"], second["artifact_fingerprint"]
        )
        self.assertNotEqual(
            first["comment_fingerprint_marker"],
            second["comment_fingerprint_marker"],
        )

    def test_5_corrupt_artifact_stalls_without_replacement(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        path.write_text("{corrupt\n", encoding="utf-8")
        original = path.read_bytes()
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_artifact"):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(path.read_bytes(), original)

    def test_6_lost_response_can_finalize_already_present_on_retry(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        RETRO.finalize(
            self.repo.root,
            fingerprint,
            {
                "status": "failed",
                "target_issue": ISSUE_ID,
                "error_category": "response_unknown",
                "error_summary": "Comment response not confirmed",
            },
        )
        RETRO.prepare(self.repo.root, base_intent())
        RETRO.finalize(
            self.repo.root,
            fingerprint,
            {
                "status": "already_present",
                "target_issue": ISSUE_ID,
                "error_category": None,
                "error_summary": None,
            },
        )
        document = RETRO.read_artifact(
            self.repo.artifact(fingerprint), require_final=True
        )
        self.assertEqual(document["routing"]["status"], "already_present")

    def test_7_no_source_finalizes_without_a_target(self):
        prepared = RETRO.prepare(self.repo.root, base_intent(source=None))
        RETRO.finalize(
            self.repo.root,
            prepared["artifact_fingerprint"],
            {
                "status": "no_target_issue",
                "target_issue": None,
                "error_category": None,
                "error_summary": None,
            },
        )
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"]),
            require_final=True,
        )
        self.assertIsNone(document["target_issue"])
        self.assertTrue(document["routing"]["operator_action_required"])


class RunRetroDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()

    def tearDown(self):
        self.repo.close()

    def test_concurrent_writers_create_once_and_reuse(self):
        intent_path = self.repo.root / "intent.json"
        intent_path.write_text(json.dumps(base_intent()), encoding="utf-8")
        command = [
            sys.executable,
            str(HELPER_PATH),
            "prepare",
            "--repo-root",
            str(self.repo.root),
            "--intent",
            str(intent_path),
        ]
        env = {**os.environ, "PYTHONPYCACHEPREFIX": "/var/tmp/pjan21-pycache"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: subprocess.run(
                        command,
                        text=True,
                        capture_output=True,
                        check=False,
                        env=env,
                    ),
                    range(8),
                )
            )
        self.assertTrue(all(result.returncode == 0 for result in results))
        statuses = [json.loads(result.stdout)["status"] for result in results]
        self.assertEqual(statuses.count("prepared"), 1)
        self.assertEqual(statuses.count("reused"), 7)
        artifacts = list(
            (
                self.repo.root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
            ).glob("*.json")
        )
        self.assertEqual(len(artifacts), 1)
        RETRO.read_artifact(artifacts[0])

    def test_fsyncs_file_and_directory_and_uses_no_replace_creation(self):
        with (
            mock.patch.object(
                RETRO, "_fsync_file", wraps=RETRO._fsync_file
            ) as file_sync,
            mock.patch.object(
                RETRO, "_fsync_directory", wraps=RETRO._fsync_directory
            ) as directory_sync,
            mock.patch.object(RETRO.os, "link", wraps=RETRO.os.link) as no_replace,
        ):
            prepared = RETRO.prepare(self.repo.root, base_intent())
        self.assertGreaterEqual(file_sync.call_count, 1)
        self.assertGreaterEqual(directory_sync.call_count, 2)
        no_replace.assert_called_once()
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        self.assertFalse(list(path.parent.glob(f".{path.name}.tmp.*")))

    def test_wrong_target_is_rejected_without_mutation(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        original = path.read_bytes()
        with self.assertRaisesRegex(RETRO.RetroError, "wrong_comment_target"):
            RETRO.finalize(
                self.repo.root,
                prepared["artifact_fingerprint"],
                {
                    "status": "posted",
                    "target_issue": OTHER_ISSUE_ID,
                    "error_category": None,
                    "error_summary": None,
                },
            )
        self.assertEqual(path.read_bytes(), original)

    def test_schema_is_valid_and_matches_helper_mutability_contract(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        prepared = RETRO.prepare(self.repo.root, base_intent())
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"])
        )
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(document)
        self.assertEqual(
            schema["x-hermes-immutable-json-pointers"],
            [f"/{field}" for field in RETRO.IMMUTABLE_FIELDS],
        )
        self.assertEqual(
            set(schema["x-hermes-mutable-json-pointers"]),
            {f"/routing/{field}" for field in RETRO.ROUTING_FIELDS},
        )

    def test_repository_and_issue_identity_normalization_is_exact(self):
        self.assertEqual(RETRO.canonical_repo_identity(self.repo.root), "pjangler")
        self.assertEqual(RETRO.canonical_issue_id("plane", ISSUE_ID.upper()), ISSUE_ID)
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_issue_identity"):
            RETRO.canonical_issue_id("plane", "PJAN-21")
        (self.repo.root / ".project.json").write_text(
            json.dumps(
                {
                    "project_name": "private project",
                    "ticket_provider": {"type": "plane"},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_repository_identity"):
            RETRO.canonical_repo_identity(self.repo.root)

    def test_private_material_is_rejected_before_artifact_creation(self):
        unsafe = base_intent()
        unsafe["decisions"]["what_hurt"]["summary"] = "Token=secret-value-123456"
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_summary"):
            RETRO.prepare(self.repo.root, unsafe)
        raw_log = base_intent()
        raw_log["decisions"]["what_hurt"]["summary"] = "line one\nline two"
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_summary"):
            RETRO.prepare(self.repo.root, raw_log)
        self.assertFalse(
            (
                self.repo.root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
            ).exists()
        )


class AdapterEnsureCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = pathlib.Path(self.temp.name)
        self.providers = self.root / "providers"
        self.providers.mkdir()
        self.store = self.root / "comments"
        self.store.touch()
        self.marker = "[run-retro-comment:" + ("a" * 64) + "]"
        fake = self.providers / "fake.sh"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                op="$1"; shift
                [ "$op" = ensure_comment ] || exit 2
                issue="$1"; marker="$2"; body="$3"
                if grep -Fq "$marker" "$FAKE_COMMENT_STORE"; then
                  printf '{"status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                sleep 0.1
                printf '%s\\n' "$marker" >> "$FAKE_COMMENT_STORE"
                if [ "${FAKE_LOST_RESPONSE_ONCE:-0}" = 1 ] && [ ! -e "$FAKE_LOST_MARK" ]; then
                  : > "$FAKE_LOST_MARK"
                  printf '{"status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"response not confirmed"}\\n' "$issue"
                else
                  printf '{"status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                fi
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        self.env = {
            **os.environ,
            "TICKET_PROVIDER": "fake",
            "TP_PROVIDERS_DIR": str(self.providers),
            "TP_COMMENT_LOCK_ROOT": str(self.root / "locks"),
            "FAKE_COMMENT_STORE": str(self.store),
        }

    def tearDown(self):
        self.temp.cleanup()

    def call_adapter(self, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        script = (
            f'source "{ADAPTER_PATH}"; '
            f'tp ensure_comment "{ISSUE_ID}" "{self.marker}" '
            f'"safe summary {self.marker}"'
        )
        return subprocess.run(
            ["bash", "-c", script],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_concurrent_cross_run_identical_comments_post_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.call_adapter(), range(2)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        statuses = [json.loads(result.stdout)["status"] for result in results]
        self.assertCountEqual(statuses, ["posted", "already_present"])
        self.assertEqual(self.store.read_text(encoding="utf-8").count(self.marker), 1)

    def test_crash_after_external_post_is_already_present_on_retry(self):
        lost_mark = self.root / "lost-once"
        extra = {
            "FAKE_LOST_RESPONSE_ONCE": "1",
            "FAKE_LOST_MARK": str(lost_mark),
        }
        first = self.call_adapter(extra)
        second = self.call_adapter(extra)
        self.assertEqual(json.loads(first.stdout)["status"], "failed")
        self.assertEqual(json.loads(second.stdout)["status"], "already_present")
        self.assertEqual(self.store.read_text(encoding="utf-8").count(self.marker), 1)

    def test_serialization_failure_is_safe_and_does_not_post(self):
        unusable = self.root / "not-a-directory"
        unusable.write_text("occupied\n", encoding="utf-8")
        result = self.call_adapter({"TP_COMMENT_LOCK_ROOT": str(unusable)})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "serialization_failed")
        self.assertEqual(self.store.read_text(encoding="utf-8"), "")


class PlanePaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.root = pathlib.Path(self.temp.name)
        self.role = self.root / "role"
        provider_dir = self.role / ".scripts" / "providers"
        provider_dir.mkdir(parents=True)
        shutil.copy2(PLANE_PATH, provider_dir / "plane.sh")
        (self.role / "role.yaml").write_text(
            textwrap.dedent(
                """\
                ticket_provider:
                  name: plane
                  workspace: demo
                  project: 99999999-9999-4999-8999-999999999999
                """
            ),
            encoding="utf-8",
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "curl.log"
        self.marker = "[run-retro-comment:" + ("b" * 64) + "]"

    def tearDown(self):
        self.temp.cleanup()

    def write_curl(self, body):
        curl = self.bin / "curl"
        curl.write_text(body, encoding="utf-8")
        curl.chmod(0o755)

    def run_plane(self):
        return subprocess.run(
            [
                "sh",
                str(self.role / ".scripts" / "providers" / "plane.sh"),
                "ensure_comment",
                ISSUE_ID,
                self.marker,
                f"safe summary {self.marker}",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "PLANE_API_KEY": "test-only",
                "PLANE_BASE": "https://plane.invalid",
                "CURL_LOG": str(self.log),
                "RETRO_MARKER": self.marker,
            },
        )

    def test_plane_comment_lookup_exhausts_cursor_pages(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *cursor=100%3A1%3A0*)
                    printf '{"results":[{"comment_html":"safe %s"}],"next_page_results":false,"next_cursor":""}\\n' "$RETRO_MARKER"
                    ;;
                  *)
                    printf '{"results":[],"next_page_results":true,"next_cursor":"100:1:0"}\\n'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "already_present")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("cursor=100%3A1%3A0", log)
        self.assertNotIn("-X POST", log)

    def test_plane_lookup_failure_is_safe_and_does_not_post(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                exit 22
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "lookup_failed")
        self.assertNotIn("-X POST", self.log.read_text(encoding="utf-8"))


class ProtocolParityTests(unittest.TestCase):
    def test_prompt_docs_schema_and_helper_share_exact_contract(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        docs = DOC_PATH.read_text(encoding="utf-8")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        required = [
            "hermes.run-retro.artifact",
            "hermes.run-retro.comment",
            "run-retro.v4.schema.json",
            "tp ensure_comment",
            "resolve_issue_id",
            "Unicode NFKC",
            "no-replace",
            "parent-directory fsync",
            "lookup_failed",
            "response_unknown",
            "wrong-target",
        ]
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, prompt)
                self.assertIn(token, docs)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 4)
        self.assertEqual(RETRO.SCHEMA_VERSION, 4)
        self.assertEqual(RETRO.COMMENT_FINGERPRINT_VERSION, 3)
        self.assertNotIn("PJAN-", prompt)
        self.assertNotIn("PJAN-", docs)
        prompt_contract = (
            "Each of the first two answers"
            + prompt.split("    Each of the first two answers", 1)[1].split(
                "12. **Final retro checkpoint.**", 1
            )[0]
        )
        docs_contract = (
            "Each of the first two answers"
            + docs.split("Each of the first two answers", 1)[1].split(
                "## Final retro checkpoint", 1
            )[0]
        )
        self.assertEqual(
            " ".join(prompt_contract.split()),
            " ".join(docs_contract.split()),
        )

    def test_step_11_has_exactly_three_numbered_decisions_and_step_12_is_final(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        step_11 = prompt.split(
            "11. **Post-loop improvement (end-of-batch retro).**", 1
        )[1].split("12. **Final retro checkpoint.**", 1)[0]
        decisions = [
            line.strip()
            for line in step_11.splitlines()
            if line.strip().startswith(("1. ", "2. ", "3. "))
        ]
        self.assertEqual(
            decisions,
            [
                "1. What hurt this batch?",
                "2. What should change?",
                "3. Is the fix repo-local or external/template/fleet?",
            ],
        )
        self.assertEqual(prompt.count("12. **Final retro checkpoint.**"), 1)


if __name__ == "__main__":
    unittest.main()
