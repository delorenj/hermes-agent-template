import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import jsonschema


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "template" / ".scripts" / "sentinel" / "bin" / "run-retro.py"
SCHEMA_PATH = (
    ROOT / "template" / ".scripts" / "sentinel" / "schemas" / "run-retro.v6.schema.json"
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
SYNTHETIC_SLACK_TOKEN = "".join(
    ("xo", "xb-", "123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwx")
)
SYNTHETIC_GOOGLE_KEY = "".join(("AI", "za", "SyA", "123456789012345678901234567890123"))
SYNTHETIC_STRIPE_SECRET = "".join(("sk", "_live_", "123456789012345678901234"))
SYNTHETIC_AWS_ACCESS_KEY = "".join(("AK", "IA", "1234567890ABCDEF"))


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
            "what_hurt": {
                "category": "testing",
                "summary": "signal=flaky_validation; action=add_test",
            },
            "what_should_change": {
                "category": "automation",
                "summary": "signal=manual_rework; action=automate_check",
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
        changed["decisions"]["what_hurt"]["summary"] = (
            "signal=review_rework; action=tighten_review"
        )
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
        changed["decisions"]["what_should_change"]["summary"] = (
            "signal=environment_drift; action=stabilize_environment"
        )
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
                "provider": "plane",
                "status": "failed",
                "target_issue": ISSUE_ID,
                "error_category": "response_unknown",
                "error_summary": "provider response not confirmed",
            },
        )
        RETRO.prepare(self.repo.root, base_intent())
        RETRO.finalize(
            self.repo.root,
            fingerprint,
            {
                "provider": "plane",
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
                "provider": "plane",
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
        self.assertTrue(document["operator_action_required"])


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
                    "provider": "plane",
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
        self.assertEqual(
            schema["$defs"]["safeSummary"]["pattern"],
            RETRO.SAFE_SUMMARY_RE.pattern,
        )
        self.assertEqual(
            schema["properties"]["protected_evidence_refs"]["items"]["pattern"],
            RETRO.SAFE_REF_RE.pattern,
        )
        self.assertEqual(
            schema["$defs"]["utcTimestamp"]["pattern"],
            RETRO.UTC_TIMESTAMP_RE.pattern,
        )

    def test_schema_and_runtime_share_exact_representable_acceptance_rules(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        prepared = RETRO.prepare(self.repo.root, base_intent())
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"])
        )
        validator.validate(document)
        RETRO.validate_document(document)

        null_prepared = RETRO.prepare(
            self.repo.root,
            base_intent("00000000-0000-4000-8000-000000000002", source=None),
        )
        null_document = RETRO.read_artifact(
            self.repo.artifact(null_prepared["artifact_fingerprint"])
        )
        validator.validate(null_document)
        RETRO.validate_document(null_document)
        invalid_null = json.loads(json.dumps(null_document))
        invalid_null["operator_action_required"] = False
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(invalid_null)
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_artifact"):
            RETRO.validate_document(invalid_null)

        uuid_v7 = "0194697e-e7d7-7a2b-8e4c-0123456789ab"
        self.assertEqual(RETRO.canonical_issue_id("plane", uuid_v7), uuid_v7)
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_issue_identity"):
            RETRO.canonical_issue_id("plane", "00000000-0000-0000-0000-000000000000")

        adversarial = {
            "offset_timestamp": ("recorded_at", "2026-07-27T00:00:00+00:00"),
            "arbitrary_summary": (
                "decisions.what_hurt.summary",
                f"Slack {SYNTHETIC_SLACK_TOKEN}",
            ),
            "dotdot_reference": ("protected_evidence_refs", ["a/../b"]),
        }
        for label, (pointer, value) in adversarial.items():
            mutated = json.loads(json.dumps(document))
            target = mutated
            parts = pointer.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            with self.subTest(label=label):
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(mutated)
                with self.assertRaises(RETRO.RetroError):
                    RETRO.validate_document(mutated)

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

    def test_all_protected_material_shapes_fail_closed_before_artifact(self):
        unsafe_values = [
            "Token=secret-value-123456",
            f"Slack {SYNTHETIC_SLACK_TOKEN}",
            f"Google {SYNTHETIC_GOOGLE_KEY}",
            f"Stripe {SYNTHETIC_STRIPE_SECRET}",
            "Password is correct-horse-battery-staple",
            "Authorization: Basic dXNlcjpwYXNz",
            "Customer SSN 123-45-6789",
            "Customer SSN 123–45–6789",
            "Customer SSN 123 45 6789",
            "Customer SSN 123456789",
            f"AWS access key {SYNTHETIC_AWS_ACCESS_KEY}",
            "Evidence:/home/alice/private/customer.log",
            "Evidence `/home/alice/private/customer.log`",
            "Contact alice@example.test",
            "Call 212-555-0199",
            "Call +44 20 7946 0958",
            "Card 4111 1111 1111 1111",
            "ERROR: raw provider response",
            "2026-07-27T00:00:00Z INFO request completed",
            "-----BEGIN PRIVATE KEY-----",
            "Bearer abcdefghijklmnop",
            "line one\nline two",
            "ordinary prose outside the closed vocabulary",
        ]
        for index, value in enumerate(unsafe_values):
            unsafe = base_intent(f"00000000-0000-4000-8000-{index + 10:012d}")
            unsafe["decisions"]["what_hurt"]["summary"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(RETRO.RetroError, "unsafe_summary"):
                    RETRO.prepare(self.repo.root, unsafe)
        self.assertFalse(
            (
                self.repo.root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
            ).exists()
        )

    def test_stored_issue_ids_must_be_canonical_and_byte_equal(self):
        mixed_issue = "abcdef12-abcd-4abc-8abc-abcdef123456"
        prepared = RETRO.prepare(
            self.repo.root, base_intent(source=mixed_issue.upper())
        )
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        document = RETRO.read_artifact(path)
        self.assertEqual(document["source_issue"], mixed_issue)
        document["source_issue"] = mixed_issue.upper()
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_artifact"):
            RETRO.read_artifact(path)

    def test_stale_failure_cannot_overwrite_terminal_success(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        success = {
            "provider": "plane",
            "status": "posted",
            "target_issue": ISSUE_ID,
            "error_category": None,
            "error_summary": None,
        }
        stale = {
            "provider": "plane",
            "status": "failed",
            "target_issue": ISSUE_ID,
            "error_category": "response_unknown",
            "error_summary": "provider response not confirmed",
        }
        RETRO.finalize(self.repo.root, fingerprint, success)
        result = RETRO.finalize(self.repo.root, fingerprint, stale)
        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["transition"], "preserved_terminal")
        document = RETRO.read_artifact(
            self.repo.artifact(fingerprint), require_final=True
        )
        self.assertEqual(document["routing"]["status"], "posted")

    def test_non_utf8_input_and_artifact_fail_with_sanitized_json(self):
        bad_input = self.repo.root / "bad-input.json"
        bad_input.write_bytes(b"\xff")
        prepare = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "prepare",
                "--repo-root",
                str(self.repo.root),
                "--intent",
                str(bad_input),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepare.returncode, 3)
        self.assertEqual(json.loads(prepare.stdout)["error_category"], "invalid_input")
        self.assertEqual(prepare.stderr, "")
        self.assertNotIn(str(self.repo.root), prepare.stdout)

        prepared = RETRO.prepare(self.repo.root, base_intent())
        self.repo.artifact(prepared["artifact_fingerprint"]).write_bytes(b"\xff")
        validate = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "validate",
                "--repo-root",
                str(self.repo.root),
                "--artifact-fingerprint",
                prepared["artifact_fingerprint"],
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validate.returncode, 3)
        self.assertEqual(
            json.loads(validate.stdout)["error_category"], "invalid_artifact"
        )
        self.assertEqual(validate.stderr, "")
        self.assertNotIn(str(self.repo.root), validate.stdout)

    def test_symlinked_artifact_directory_is_rejected_without_escape(self):
        external = self.repo.root / "external"
        external.mkdir()
        artifacts = self.repo.root / "_bmad-output" / "implementation-artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "run-retros").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_lock_is_rejected_without_truncating_target(self):
        retro_dir = (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )
        locks = retro_dir / ".locks" / "artifacts"
        locks.mkdir(parents=True)
        fingerprint = RETRO.artifact_fingerprint("pjangler", base_intent()["run_id"])
        victim = self.repo.root / "victim"
        victim.write_text("DO NOT TRUNCATE\n", encoding="utf-8")
        (locks / f"{fingerprint}.lock").symlink_to(victim)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TRUNCATE\n")

    def test_symlinked_artifact_file_is_rejected_without_reading_target(self):
        retro_dir = (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )
        retro_dir.mkdir(parents=True)
        fingerprint = RETRO.artifact_fingerprint("pjangler", base_intent()["run_id"])
        victim = self.repo.root / "artifact-victim"
        victim.write_text(json.dumps({"secret": "must-not-be-read"}), encoding="utf-8")
        (retro_dir / f"{fingerprint}.json").symlink_to(victim)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(
            victim.read_text(encoding="utf-8"),
            json.dumps({"secret": "must-not-be-read"}),
        )

    def test_run_retros_swap_after_lock_never_writes_external_and_stalls_safely(self):
        external = self.repo.root / "external"
        external.mkdir()
        intent_path = self.repo.root / "intent.json"
        intent_path.write_text(json.dumps(base_intent()), encoding="utf-8")
        original_flock = RETRO.fcntl.flock
        swapped = False

        def swap_after_lock(descriptor, operation):
            nonlocal swapped
            result = original_flock(descriptor, operation)
            if operation == RETRO.fcntl.LOCK_EX and not swapped:
                retro = (
                    self.repo.root
                    / "_bmad-output"
                    / "implementation-artifacts"
                    / "run-retros"
                )
                held = retro.with_name("run-retros-held")
                retro.rename(held)
                retro.symlink_to(external, target_is_directory=True)
                swapped = True
            return result

        stdout = io.StringIO()
        with (
            mock.patch.object(RETRO.fcntl, "flock", side_effect=swap_after_lock),
            contextlib.redirect_stdout(stdout),
        ):
            status = RETRO.main(
                [
                    "prepare",
                    "--repo-root",
                    str(self.repo.root),
                    "--intent",
                    str(intent_path),
                ]
            )
        self.assertEqual(status, 3)
        self.assertEqual(
            json.loads(stdout.getvalue())["error_category"], "unsafe_artifact_path"
        )
        self.assertEqual(list(external.iterdir()), [])


class AdapterEnsureCommentTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()
        self.root = self.repo.root
        self.providers = self.root / "providers"
        self.providers.mkdir()
        self.store = self.root / "comments"
        self.store.touch()
        self.calls = self.root / "calls"
        self.calls.touch()
        fake = self.providers / "plane.sh"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                op="$1"; shift
                [ "$op" = ensure_comment ] || exit 2
                issue="$1"; marker="$2"; body="$3"
                printf '%s|%s|%s\\n' "${TICKET_PROVIDER:-unset}" "$issue" "$marker" >> "$FAKE_CALL_STORE"
                if grep -Fq "$marker" "$FAKE_COMMENT_STORE"; then
                  printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                sleep 0.1
                printf '%s\\n' "$body" >> "$FAKE_COMMENT_STORE"
                if [ "${FAKE_LOST_RESPONSE_ONCE:-0}" = 1 ] && [ ! -e "$FAKE_LOST_MARK" ]; then
                  : > "$FAKE_LOST_MARK"
                  printf '{"provider":"plane","status":"failed","target_issue":"%s","error_category":"response_unknown","error_summary":"response not confirmed"}\\n' "$issue"
                else
                  printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                fi
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)
        wrong_provider = self.providers / "trello.sh"
        wrong_provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf 'WRONG_PROVIDER_CALLED\\n' >> "$FAKE_CALL_STORE"
                exit 99
                """
            ),
            encoding="utf-8",
        )
        wrong_provider.chmod(0o755)
        self.prepared = RETRO.prepare(self.root, base_intent())
        self.fingerprint = self.prepared["artifact_fingerprint"]

    def tearDown(self):
        self.repo.close()

    def call_delivery(self, extra_env=None):
        with mock.patch.dict(
            os.environ,
            {
                "TICKET_PROVIDER": "trello",
                "FAKE_COMMENT_STORE": str(self.store),
                "FAKE_CALL_STORE": str(self.calls),
                **(extra_env or {}),
            },
            clear=False,
        ):
            return RETRO.deliver(
                self.root,
                self.fingerprint,
                providers_dir=self.providers,
            )

    def test_concurrent_cross_run_identical_comments_post_once(self):
        second = RETRO.prepare(
            self.root,
            base_intent("00000000-0000-4000-8000-000000000002"),
        )

        def deliver(fingerprint):
            return RETRO.deliver(self.root, fingerprint, providers_dir=self.providers)

        with mock.patch.dict(
            os.environ,
            {
                "TICKET_PROVIDER": "trello",
                "FAKE_COMMENT_STORE": str(self.store),
                "FAKE_CALL_STORE": str(self.calls),
            },
            clear=False,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        deliver,
                        [self.fingerprint, second["artifact_fingerprint"]],
                    )
                )
        statuses = [result["status"] for result in results]
        self.assertCountEqual(statuses, ["posted", "already_present"])
        marker = self.prepared["comment_fingerprint_marker"]
        self.assertEqual(self.store.read_text(encoding="utf-8").count(marker), 1)
        self.assertTrue(
            all(
                line.startswith(f"plane|{ISSUE_ID}|")
                for line in self.calls.read_text().splitlines()
            )
        )

    def test_crash_after_external_post_is_already_present_on_retry(self):
        lost_mark = self.root / "lost-once"
        extra = {
            "FAKE_LOST_RESPONSE_ONCE": "1",
            "FAKE_LOST_MARK": str(lost_mark),
        }
        first = self.call_delivery(extra)
        second = self.call_delivery(extra)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "already_present")
        marker = self.prepared["comment_fingerprint_marker"]
        self.assertEqual(self.store.read_text(encoding="utf-8").count(marker), 1)

    def test_sigkill_controller_keeps_lock_for_provider_subtree(self):
        provider = self.providers / "plane.sh"
        started = self.root / "provider-started"
        first_claim = self.root / "provider-first-claim"
        posts = self.root / "external-posts"
        posts.touch()
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                op="$1"; shift
                [ "$op" = ensure_comment ] || exit 2
                issue="$1"; marker="$2"; body="$3"
                if grep -Fq "$marker" "$FAKE_COMMENT_STORE"; then
                  printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                if mkdir "$FAKE_FIRST_CLAIM" 2>/dev/null; then
                  : > "$FAKE_PROVIDER_STARTED"
                  sleep 1.5
                  printf '%s\\n' "$body" >> "$FAKE_COMMENT_STORE"
                  printf 'post\\n' >> "$FAKE_POST_STORE"
                  printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                printf 'duplicate-post-attempt\\n' >> "$FAKE_POST_STORE"
                printf '%s\\n' "$body" >> "$FAKE_COMMENT_STORE"
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        command = [
            sys.executable,
            str(HELPER_PATH),
            "deliver",
            "--repo-root",
            str(self.root),
            "--artifact-fingerprint",
            self.fingerprint,
            "--providers-dir",
            str(self.providers),
        ]
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_COMMENT_STORE": str(self.store),
            "FAKE_PROVIDER_STARTED": str(started),
            "FAKE_FIRST_CLAIM": str(first_claim),
            "FAKE_POST_STORE": str(posts),
        }
        controller = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started.exists(), "provider did not enter its post window")
        os.kill(controller.pid, signal.SIGKILL)
        controller.wait(timeout=5)

        retry_started = time.monotonic()
        retry = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
        elapsed = time.monotonic() - retry_started
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["status"], "already_present")
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(posts.read_text(encoding="utf-8").splitlines(), ["post"])
        marker = self.prepared["comment_fingerprint_marker"]
        self.assertEqual(self.store.read_text(encoding="utf-8").count(marker), 1)

    def test_provider_override_cannot_redirect_prepared_intent(self):
        result = self.call_delivery()
        self.assertEqual(result["status"], "posted")
        self.assertEqual(
            self.calls.read_text(encoding="utf-8").split("|", 2)[:2],
            ["plane", ISSUE_ID],
        )
        self.assertNotIn(
            "WRONG_PROVIDER_CALLED", self.calls.read_text(encoding="utf-8")
        )

    def test_same_marker_has_one_immutable_body_across_failure_and_retry(self):
        path = self.repo.artifact(self.fingerprint)
        before = RETRO.read_artifact(path)
        body = before["comment_body"]
        marker = before["comment_fingerprint_marker"]
        self.call_delivery(
            {
                "FAKE_LOST_RESPONSE_ONCE": "1",
                "FAKE_LOST_MARK": str(self.root / "lost"),
            }
        )
        failed = RETRO.read_artifact(path)
        self.assertEqual(failed["comment_body"], body)
        self.assertEqual(failed["comment_fingerprint_marker"], marker)
        self.assertEqual(failed["operator_action_required"], False)
        self.call_delivery()
        retried = RETRO.read_artifact(path)
        self.assertEqual(retried["comment_body"], body)
        self.assertEqual(retried["comment_fingerprint_marker"], marker)

    def test_wrong_extra_issue_argument_is_rejected_before_provider_call(self):
        fixture = self.root / "role"
        lib = fixture / ".scripts" / "lib"
        sentinel = fixture / ".scripts" / "sentinel" / "bin"
        lib.mkdir(parents=True)
        sentinel.mkdir(parents=True)
        shutil.copy2(ADAPTER_PATH, lib / "ticket-provider.sh")
        shutil.copy2(HELPER_PATH, sentinel / "run-retro.py")
        script = (
            f'source "{lib / "ticket-provider.sh"}"; '
            f'tp ensure_comment "{self.fingerprint}" "{OTHER_ISSUE_ID}"'
        )
        result = subprocess.run(
            ["bash", "-c", script],
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "TP_PROVIDERS_DIR": str(self.providers),
                "FAKE_COMMENT_STORE": str(self.store),
                "FAKE_CALL_STORE": str(self.calls),
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_wrong_prepared_target_is_rejected_before_provider_call(self):
        path = self.repo.artifact(self.fingerprint)
        document = RETRO.read_artifact(path)
        document["target_issue"] = OTHER_ISSUE_ID
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(RETRO.RetroError, "wrong_comment_target"):
            self.call_delivery()
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_wrong_prepared_provider_is_rejected_before_provider_call(self):
        path = self.repo.artifact(self.fingerprint)
        document = RETRO.read_artifact(path)
        document["provider"] = "trello"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_issue_identity"):
            self.call_delivery()
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_no_source_delivery_finalizes_without_provider_call(self):
        no_source = RETRO.prepare(
            self.root,
            base_intent("00000000-0000-4000-8000-000000000003", source=None),
        )
        result = RETRO.deliver(
            self.root,
            no_source["artifact_fingerprint"],
            providers_dir=self.providers,
        )
        self.assertEqual(result["status"], "no_target_issue")
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_symlinked_comment_lock_fails_before_provider_without_truncation(self):
        document = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        key = RETRO._sha256_lines(
            [
                document["provider"],
                document["source_issue"],
                document["comment_fingerprint_marker"],
            ]
        )
        lock_dir = self.repo.artifact(self.fingerprint).parent / ".locks" / "comments"
        lock_dir.mkdir(parents=True)
        victim = self.root / "comment-lock-victim"
        victim.write_text("DO NOT TRUNCATE\n", encoding="utf-8")
        (lock_dir / f"{key}.lock").symlink_to(victim)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            self.call_delivery()
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TRUNCATE\n")
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")


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

    def test_plane_comment_lookup_exhausts_limit_offset_pages(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *offset=100*)
                    printf '{"results":[{"comment_html":"safe %s"}],"total_results":101}\\n' "$RETRO_MARKER"
                    ;;
                  *)
                    python3 -c 'import json; print(json.dumps({"results":[{"comment_html":"safe"}]*100,"total_results":101}))'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "already_present")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("offset=100", log)
        self.assertIn(f"/work-items/{ISSUE_ID}/comments/", log)
        self.assertNotIn("/issues/", log)
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

    def test_plane_malformed_success_envelopes_fail_closed_without_post(self):
        malformed_pages = [
            '{"detail":"temporary backend envelope"}',
            '{"results":"not-a-list","total_results":0}',
            '{"results":[]}',
            '{"results":[],"total_results":"0"}',
            '{"results":[],"total_results":true}',
            '{"results":[],"total_results":-1}',
            '{"results":[null],"total_results":1}',
            '{"results":[{}],"total_results":1}',
            '{"results":[{"comment_html":null}],"total_results":1}',
            '{"results":[],"total_results":1}',
            '{"results":[{"comment_html":"safe"}],"total_results":0}',
        ]
        for index, page in enumerate(malformed_pages):
            with self.subTest(index=index, page=page):
                self.log.write_text("", encoding="utf-8")
                self.write_curl(
                    textwrap.dedent(
                        f"""\
                        #!/usr/bin/env sh
                        printf '%s\\n' "$*" >> "$CURL_LOG"
                        printf '%s\\n' '{page}'
                        """
                    )
                )
                result = self.run_plane()
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["error_category"], "lookup_failed")
                self.assertNotIn("-X POST", self.log.read_text(encoding="utf-8"))

    def test_plane_post_uses_current_work_item_comment_endpoint(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"-X POST"*) printf '{"id":"33333333-3333-4333-8333-333333333333"}\\n' ;;
                  *) printf '{"results":[],"total_results":0}\\n' ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "plane")
        self.assertEqual(payload["status"], "posted")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn(f"/work-items/{ISSUE_ID}/comments/", log)
        self.assertNotIn("/issues/", log)


class ProtocolParityTests(unittest.TestCase):
    def test_prompt_docs_schema_and_helper_share_exact_contract(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        docs = DOC_PATH.read_text(encoding="utf-8")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        required = [
            "hermes.run-retro.artifact",
            "hermes.run-retro.comment",
            "run-retro.v6.schema.json",
            "tp ensure_comment",
            "resolve_issue_id",
            "Unicode NFKC",
            "closed safe-summary vocabulary",
            "no-replace",
            "parent-directory fsync",
            "descriptor-relative",
            "provider subtree",
            "lookup_failed",
            "response_unknown",
            "TICKET_PROVIDER",
            "limit`/`offset",
            "symlinks",
            "monotonic",
        ]
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, prompt)
                self.assertIn(token, docs)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 6)
        self.assertEqual(RETRO.SCHEMA_VERSION, 6)
        self.assertEqual(RETRO.COMMENT_FINGERPRINT_VERSION, 5)
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
