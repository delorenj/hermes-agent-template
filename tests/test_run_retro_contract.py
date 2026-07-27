import concurrent.futures
import contextlib
import importlib.util
import http.server
import io
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

import jinja2
import jsonschema
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "template" / ".scripts" / "sentinel" / "bin" / "run-retro.py"
SCHEMA_PATH = (
    ROOT / "template" / ".scripts" / "sentinel" / "schemas" / "run-retro.v8.schema.json"
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
LINEAR_PATH = ROOT / "template" / ".scripts" / "providers" / "linear.sh"
TRELLO_PATH = ROOT / "template" / ".scripts" / "providers" / "trello.sh"
CLOSE_GATE_PATH = (
    ROOT / "template" / ".scripts" / "sentinel" / "bin" / "issue-close-gate.sh"
)
COPIER_CONFIG_PATH = ROOT / "copier.yml"
ISSUE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ISSUE_ID = "22222222-2222-4222-8222-222222222222"
SYNTHETIC_SLACK_TOKEN = "".join(
    ("xo", "xb-", "123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwx")
)
SYNTHETIC_GOOGLE_KEY = "".join(("AI", "za", "SyA", "123456789012345678901234567890123"))
SYNTHETIC_STRIPE_SECRET = "".join(("sk", "_live_", "123456789012345678901234"))
SYNTHETIC_AWS_ACCESS_KEY = "".join(("AK", "IA", "1234567890ABCDEF"))


def rendered_copier_task(target_repo="pjangler", role="pm"):
    config = yaml.safe_load(COPIER_CONFIG_PATH.read_text(encoding="utf-8"))
    task_template = config["_tasks"][0]
    environment = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,
    )
    return environment.from_string(task_template).render(
        target_repo=target_repo,
        role=role,
    )


def load_helper():
    spec = importlib.util.spec_from_file_location("hermes_run_retro", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RETRO = load_helper()


def trusted_finalize(repo_root, fingerprint, result):
    """Exercise the internal one-shot transition seam without public finalize."""
    if not hasattr(RETRO, "_issue_trusted_transition"):
        return RETRO.finalize(repo_root, fingerprint, result)
    with RETRO._repository(repo_root) as repository:
        with RETRO._retro_store(repository, create=False) as store:
            stored = RETRO._read_artifact_at(store, fingerprint)
            transition = RETRO._issue_trusted_transition(
                stored,
                fingerprint,
                result,
            )
            return RETRO._finalize_at(store, fingerprint, transition)


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
        "protected_evidence_refs": ["evidence:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        "sanitization": {
            "status": "sanitized",
            "omitted_categories": ["raw_logs"],
        },
    }


class RepoFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = pathlib.Path(self.temp.name)
        controller_bin = self.root / ".scripts" / "sentinel" / "bin"
        controller_bin.mkdir(parents=True)
        for path in (
            self.root / ".scripts",
            self.root / ".scripts" / "sentinel",
            controller_bin,
        ):
            path.chmod(0o755)
        self.controller = controller_bin / "run-retro.py"
        shutil.copy2(HELPER_PATH, self.controller)
        self.controller.chmod(0o755)
        RETRO.__file__ = str(self.controller)
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
        (self.root / ".project.json").chmod(0o644)

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
        trusted_finalize(
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
        trusted_finalize(
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
        trusted_finalize(
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
        self.assertIsNone(document["source_issue"])
        self.assertTrue(document["operator_action_required"])


class RunRetroV12RegressionTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()
        self._restores = []

    def tearDown(self):
        for restore in reversed(self._restores):
            restore()
        self.repo.close()

    def _retro_path(self):
        return (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )

    def _swap_retro_to_external(self, suffix):
        retro = self._retro_path()
        held = self.repo.root.parent / f"{self.repo.root.name}-{suffix}-held"
        external = self.repo.root.parent / f"{self.repo.root.name}-{suffix}-external"
        retro.rename(held)
        external.mkdir()
        retro.symlink_to(external, target_is_directory=True)

        def restore():
            if retro.is_symlink():
                retro.unlink()
            if retro.exists():
                shutil.rmtree(retro)
            if held.exists():
                held.rename(retro)
            shutil.rmtree(external, ignore_errors=True)

        self._restores.append(restore)
        return held, external

    def test_public_finalize_rejects_fabricated_posted_result(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        result_path = self.repo.root / "fabricated-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": ISSUE_ID,
                    "error_category": None,
                    "error_summary": None,
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "finalize",
                "--repo-root",
                str(self.repo.root),
                "--artifact-fingerprint",
                prepared["artifact_fingerprint"],
                "--result",
                str(result_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            RETRO.read_artifact(self.repo.artifact(prepared["artifact_fingerprint"]))[
                "routing"
            ]["status"],
            "prepared",
        )
        validate = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "validate",
                "--repo-root",
                str(self.repo.root),
                "--artifact-fingerprint",
                prepared["artifact_fingerprint"],
                "--final",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validate.returncode, 3)
        evidence = (
            self.repo.root
            / "_bmad-output"
            / "implementation-artifacts"
            / "issue-evidence"
            / "PJAN-21.md"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            textwrap.dedent(
                """\
                ## Issue
                PJAN-21
                ## Acceptance Criteria
                Locked.
                ## Repo Changes
                Scoped.
                ## Verification
                Verified.
                ## Ledger Update
                Ledger updated: yes
                ## Known Gaps
                None material.
                ## Close Recommendation
                Close recommendation: ready
                """
            ),
            encoding="utf-8",
        )
        gate = subprocess.run(
            ["sh", str(CLOSE_GATE_PATH), "PJAN-21", str(self.repo.root)],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "TMPDIR": str(self.repo.root)},
        )
        self.assertEqual(gate.returncode, 1)
        self.assertIn("Retro finalization proof failed.", gate.stderr)

    def test_trusted_transition_is_bound_single_use_and_not_self_attested(self):
        first = RETRO.prepare(self.repo.root, base_intent())
        second = RETRO.prepare(
            self.repo.root,
            base_intent("00000000-0000-4000-8000-000000000002"),
        )
        success = {
            "provider": "plane",
            "status": "posted",
            "target_issue": ISSUE_ID,
            "error_category": None,
            "error_summary": None,
        }
        with RETRO._repository(self.repo.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                first_stored = RETRO._read_artifact_at(
                    store,
                    first["artifact_fingerprint"],
                )
                transition = RETRO._issue_trusted_transition(
                    first_stored,
                    first["artifact_fingerprint"],
                    success,
                )
                with self.assertRaisesRegex(
                    RETRO.RetroError,
                    "untrusted_finalization",
                ):
                    RETRO._finalize_at(
                        store,
                        second["artifact_fingerprint"],
                        transition,
                    )
                RETRO._finalize_at(
                    store,
                    first["artifact_fingerprint"],
                    transition,
                )
                with self.assertRaisesRegex(
                    RETRO.RetroError,
                    "untrusted_finalization",
                ):
                    RETRO._finalize_at(
                        store,
                        first["artifact_fingerprint"],
                        transition,
                    )
                fabricated = RETRO._TrustedTransition(
                    artifact_fingerprint=second["artifact_fingerprint"],
                    immutable_sha256=transition.immutable_sha256,
                    transition_id="33333333-3333-4333-8333-333333333333",
                    result_json=transition.result_json,
                    seal="0" * 64,
                )
                with self.assertRaisesRegex(
                    RETRO.RetroError,
                    "untrusted_finalization",
                ):
                    RETRO._finalize_at(
                        store,
                        second["artifact_fingerprint"],
                        fabricated,
                    )

    def test_intermediate_replacement_cannot_redirect_binding_create(self):
        original_open = RETRO.os.open
        swapped = False
        external = None

        def swap_before_binding_create(path, flags, *args, **kwargs):
            nonlocal swapped, external
            directory_fd = kwargs.get("dir_fd")
            try:
                directory_path = os.readlink(f"/proc/self/fd/{directory_fd}")
            except (OSError, TypeError):
                directory_path = ""
            if (
                not swapped
                and flags & RETRO.os.O_CREAT
                and (
                    ".sha256" in str(path)
                    or pathlib.Path(directory_path).name == ".bindings"
                )
            ):
                _, external = self._swap_retro_to_external("binding-create")
                (external / ".bindings").mkdir()
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            RETRO.os,
            "open",
            side_effect=swap_before_binding_create,
        ):
            with contextlib.suppress(RETRO.RetroError):
                RETRO.prepare(self.repo.root, base_intent())
        self.assertTrue(swapped)
        self.assertEqual(list((external / ".bindings").iterdir()), [])

    def test_intermediate_replacement_cannot_redirect_artifact_link(self):
        original_link = RETRO.os.link
        swapped = False
        external = None

        def swap_before_link(source, target, *args, **kwargs):
            nonlocal swapped, external
            if not swapped:
                held, external = self._swap_retro_to_external("artifact-link")
                source_name = pathlib.Path(str(source)).name
                if "/" in str(source):
                    shutil.copy2(held / source_name, external / source_name)
                swapped = True
            return original_link(source, target, *args, **kwargs)

        with mock.patch.object(RETRO.os, "link", side_effect=swap_before_link):
            with contextlib.suppress(RETRO.RetroError):
                RETRO.prepare(self.repo.root, base_intent())
        self.assertTrue(swapped)
        self.assertEqual(list(external.glob("*.json")), [])

    def test_intermediate_replacement_cannot_redirect_artifact_replace(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        original_replace = RETRO.os.replace
        swapped = False
        external = None

        def swap_before_replace(source, target, *args, **kwargs):
            nonlocal swapped, external
            if not swapped:
                held, external = self._swap_retro_to_external("artifact-replace")
                source_name = pathlib.Path(str(source)).name
                if "/" in str(source):
                    shutil.copy2(held / source_name, external / source_name)
                swapped = True
            return original_replace(source, target, *args, **kwargs)

        with RETRO._repository(self.repo.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                document = RETRO._read_artifact_at(store, fingerprint)
                updated = json.loads(json.dumps(document))
                updated["routing"] = {
                    "status": "failed",
                    "error_category": "response_unknown",
                    "updated_at_epoch_us": (
                        document["routing"]["updated_at_epoch_us"] + 1
                    ),
                    "proof": {"status": "unverified", "transition_id": None},
                }
                with mock.patch.object(
                    RETRO.os,
                    "replace",
                    side_effect=swap_before_replace,
                ):
                    with contextlib.suppress(RETRO.RetroError):
                        RETRO._durable_replace(store, fingerprint, updated)
        self.assertTrue(swapped)
        self.assertEqual(list(external.glob("*.json")), [])

    def test_intermediate_replacement_cannot_redirect_binding_replace(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        original_replace = RETRO.os.replace
        swapped = False
        external = None

        def swap_before_binding_replace(source, target, *args, **kwargs):
            nonlocal swapped, external
            directory_fd = kwargs.get("src_dir_fd")
            try:
                directory_path = os.readlink(f"/proc/self/fd/{directory_fd}")
            except (OSError, TypeError):
                directory_path = ""
            if not swapped and pathlib.Path(directory_path).name == ".bindings":
                held, external = self._swap_retro_to_external("binding-replace")
                (external / ".bindings").mkdir()
                source_name = pathlib.Path(str(source)).name
                if "/" in str(source):
                    shutil.copy2(
                        held / ".bindings" / source_name,
                        external / ".bindings" / source_name,
                    )
                swapped = True
            return original_replace(source, target, *args, **kwargs)

        with mock.patch.object(
            RETRO.os,
            "replace",
            side_effect=swap_before_binding_replace,
        ):
            with contextlib.suppress(RETRO.RetroError):
                trusted_finalize(
                    self.repo.root,
                    prepared["artifact_fingerprint"],
                    {
                        "provider": "plane",
                        "status": "posted",
                        "target_issue": ISSUE_ID,
                        "error_category": None,
                        "error_summary": None,
                    },
                )
        self.assertTrue(swapped)
        self.assertEqual(list((external / ".bindings").iterdir()), [])

    def test_initial_binding_crash_leaves_no_permanent_poison_and_retries(self):
        original_fdopen = RETRO.os.fdopen
        crashed = False

        def crash_before_binding_write(descriptor, *args, **kwargs):
            nonlocal crashed
            try:
                descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                descriptor_path = ""
            mode = args[0] if args else kwargs.get("mode", "r")
            if (
                not crashed
                and "/.bindings/" in descriptor_path
                and ".sha256" in descriptor_path
                and "w" in mode
            ):
                crashed = True
                raise RuntimeError("simulated crash before binding write")
            return original_fdopen(descriptor, *args, **kwargs)

        with (
            mock.patch.object(
                RETRO.os,
                "fdopen",
                side_effect=crash_before_binding_write,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertTrue(crashed)
        bindings = self._retro_path() / ".bindings"
        self.assertEqual(
            [path for path in bindings.iterdir() if path.name.endswith(".sha256")],
            [],
        )
        self.assertEqual(list(bindings.iterdir()), [])
        retry = RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(retry["status"], "prepared")

    def test_absolute_close_gate_does_not_trust_unrelated_cwd(self):
        installed = self.repo.root / "installed-role"
        bin_dir = installed / ".scripts" / "sentinel" / "bin"
        schema_dir = installed / ".scripts" / "sentinel" / "schemas"
        bin_dir.mkdir(parents=True)
        schema_dir.mkdir(parents=True)
        shutil.copy2(CLOSE_GATE_PATH, bin_dir / "issue-close-gate.sh")
        shutil.copy2(HELPER_PATH, bin_dir / "run-retro.py")
        shutil.copy2(
            ROOT / "template" / ".scripts" / "sentinel" / "bin" / "emit-event.py",
            bin_dir / "emit-event.py",
        )
        shutil.copy2(SCHEMA_PATH, schema_dir / SCHEMA_PATH.name)
        (installed / "role.yaml").write_text("repo: installed\n", encoding="utf-8")
        (installed / ".project.json").write_text(
            json.dumps(
                {
                    "project_name": "installed",
                    "ticket_provider": {"type": "plane"},
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(installed)], check=True)

        unrelated = self.repo.root / "unrelated"
        subprocess.run(
            ["git", "init", "-q", str(unrelated)],
            check=True,
        )
        evidence = (
            unrelated
            / "_bmad-output"
            / "implementation-artifacts"
            / "issue-evidence"
            / "PJAN-21.md"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            textwrap.dedent(
                """\
                ## Issue
                PJAN-21
                ## Acceptance Criteria
                Locked.
                ## Repo Changes
                Fabricated.
                ## Verification
                Fabricated.
                ## Ledger Update
                Ledger updated: yes
                ## Known Gaps
                None material.
                ## Close Recommendation
                Close recommendation: ready
                """
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["sh", str(bin_dir / "issue-close-gate.sh"), "PJAN-21"],
            cwd=unrelated,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing issue evidence file", result.stderr)


class RunRetroV13RegressionTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()

    def tearDown(self):
        self.repo.close()

    def _retro_path(self):
        return (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )

    def _snapshot(self, path):
        result = {}
        for item in sorted(path.rglob("*")):
            relative = str(item.relative_to(path))
            result[relative] = None if item.is_dir() else item.read_bytes()
        return result

    def _binding_path(self, fingerprint):
        return self._retro_path() / ".bindings" / f"{fingerprint}.sha256"

    def test_retro_relocation_before_mutation_fails_without_any_tree_write(self):
        retro = self._retro_path()
        outside = self.repo.root.parent / f"{self.repo.root.name}-retro-outside"
        with RETRO._repository(self.repo.root) as repository:
            proposed = RETRO._build_prepared(
                repository.repo,
                repository.provider,
                base_intent(),
            )
            fingerprint = RETRO._document_fingerprint(proposed)
            with RETRO._retro_store(repository, create=True) as store:
                retro.rename(outside)
                retro.mkdir()
                try:
                    outside_before = self._snapshot(outside)
                    replacement_before = self._snapshot(retro)
                    with self.assertRaisesRegex(
                        RETRO.RetroError,
                        "unsafe_artifact_path",
                    ):
                        RETRO._ensure_binding(store, fingerprint, proposed)
                    self.assertEqual(self._snapshot(outside), outside_before)
                    self.assertEqual(self._snapshot(retro), replacement_before)
                finally:
                    shutil.rmtree(retro)
                    outside.rename(retro)

    def test_bindings_replacement_before_mutation_is_rejected_without_writes(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        bindings = self._retro_path() / ".bindings"
        outside = self.repo.root.parent / f"{self.repo.root.name}-bindings-outside"
        with RETRO._repository(self.repo.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                document = RETRO._read_artifact_at(store, fingerprint)
                bindings.rename(outside)
                bindings.mkdir()
                try:
                    outside_before = self._snapshot(outside)
                    replacement_before = self._snapshot(bindings)
                    with self.assertRaisesRegex(
                        RETRO.RetroError,
                        "unsafe_artifact_path",
                    ):
                        RETRO._ensure_binding(store, fingerprint, document)
                    self.assertEqual(self._snapshot(outside), outside_before)
                    self.assertEqual(self._snapshot(bindings), replacement_before)
                finally:
                    shutil.rmtree(bindings)
                    outside.rename(bindings)

    def test_retry_fsyncs_binding_after_crash_between_link_and_barriers(self):
        original_link = RETRO.os.link
        crashed = False

        def crash_after_binding_link(source, target, *args, **kwargs):
            nonlocal crashed
            result = original_link(source, target, *args, **kwargs)
            if not crashed and str(target).endswith(".sha256"):
                crashed = True
                raise RuntimeError("simulated crash after binding link")
            return result

        with (
            mock.patch.object(
                RETRO.os,
                "link",
                side_effect=crash_after_binding_link,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertTrue(crashed)

        with RETRO._repository(self.repo.root) as repository:
            proposed = RETRO._build_prepared(
                repository.repo,
                repository.provider,
                base_intent(),
            )
            fingerprint = RETRO._document_fingerprint(proposed)
        binding = self._binding_path(fingerprint)
        self.assertTrue(binding.is_file())
        self.assertGreater(binding.stat().st_size, 0)
        self.assertFalse(self.repo.artifact(fingerprint).exists())

        original_validate = RETRO._validate_binding_at
        original_file_sync = RETRO._fsync_file
        original_directory_sync = RETRO._fsync_directory
        original_assert_path = RETRO._assert_store_path
        events = []

        def is_bindings_fd(descriptor):
            held = os.fstat(descriptor)
            current = os.stat(binding.parent, follow_symlinks=False)
            return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)

        def record_validate(descriptor, *args, **kwargs):
            if is_bindings_fd(descriptor):
                events.append("validate")
            return original_validate(descriptor, *args, **kwargs)

        def record_file_sync(descriptor, name):
            if is_bindings_fd(descriptor):
                events.append("file-fsync")
            return original_file_sync(descriptor, name)

        def record_directory_sync(descriptor):
            if is_bindings_fd(descriptor):
                events.append("directory-fsync")
            return original_directory_sync(descriptor)

        def record_path_check(store):
            events.append("path-check")
            return original_assert_path(store)

        with (
            mock.patch.object(
                RETRO,
                "_validate_binding_at",
                side_effect=record_validate,
            ),
            mock.patch.object(
                RETRO,
                "_fsync_file",
                side_effect=record_file_sync,
            ),
            mock.patch.object(
                RETRO,
                "_fsync_directory",
                side_effect=record_directory_sync,
            ),
            mock.patch.object(
                RETRO,
                "_assert_store_path",
                side_effect=record_path_check,
            ),
        ):
            retry = RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(retry["status"], "prepared")

        expected = [
            "validate",
            "file-fsync",
            "directory-fsync",
            "validate",
            "path-check",
        ]
        cursor = 0
        for event in events:
            if event == expected[cursor]:
                cursor += 1
                if cursor == len(expected):
                    break
        self.assertEqual(cursor, len(expected), events)

    def test_prepared_binding_rejects_boolean_schema_versions(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        binding = self._binding_path(fingerprint)
        original = json.loads(binding.read_text(encoding="utf-8"))
        with RETRO._repository(self.repo.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                document = RETRO._read_artifact_at(store, fingerprint)
        for boolean in (True, False):
            with self.subTest(schema_version=boolean):
                value = dict(original)
                value["schema_version"] = boolean
                binding.write_text(
                    json.dumps(value, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with RETRO._repository(self.repo.root) as repository:
                    with RETRO._retro_store(repository, create=False) as store:
                        with self.assertRaises(RETRO.RetroError):
                            RETRO._validate_binding(
                                store,
                                fingerprint,
                                document,
                            )
        binding.write_text(
            json.dumps(original, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_final_binding_rejects_boolean_schema_versions(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        fingerprint = prepared["artifact_fingerprint"]
        trusted_finalize(
            self.repo.root,
            fingerprint,
            {
                "provider": "plane",
                "status": "posted",
                "target_issue": ISSUE_ID,
                "error_category": None,
                "error_summary": None,
            },
        )
        binding = self._binding_path(fingerprint)
        original = json.loads(binding.read_text(encoding="utf-8"))
        with RETRO._repository(self.repo.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                document = RETRO._read_artifact_at(
                    store, fingerprint, require_final=True
                )
        for boolean in (True, False):
            with self.subTest(schema_version=boolean):
                value = dict(original)
                value["schema_version"] = boolean
                binding.write_text(
                    json.dumps(value, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with RETRO._repository(self.repo.root) as repository:
                    with RETRO._retro_store(repository, create=False) as store:
                        with self.assertRaises(RETRO.RetroError):
                            RETRO._validate_binding(
                                store,
                                fingerprint,
                                document,
                                require_final=True,
                            )
        binding.write_text(
            json.dumps(original, sort_keys=True) + "\n",
            encoding="utf-8",
        )


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
            str(self.repo.controller),
            "prepare",
            "--repo-root",
            str(self.repo.root),
            "--intent",
            str(intent_path),
        ]
        env = {
            **os.environ,
            "PYTHONPYCACHEPREFIX": str(
                pathlib.Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
                / "pjan21-pycache"
            ),
        }
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
        self.assertEqual(no_replace.call_count, 2)
        for call in no_replace.call_args_list:
            self.assertNotIn("/", str(call.args[0]))
            self.assertNotIn("/", str(call.args[1]))
            self.assertEqual(
                call.kwargs["src_dir_fd"],
                call.kwargs["dst_dir_fd"],
            )
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        self.assertFalse(list(path.parent.glob(f".{path.name}.tmp.*")))

    def test_crash_after_immutable_binding_retries_without_content_drift(self):
        with (
            mock.patch.object(
                RETRO,
                "_durable_create",
                side_effect=RuntimeError("simulated crash after binding"),
            ),
            self.assertRaisesRegex(RuntimeError, "simulated crash"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        prepared = RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(prepared["status"], "prepared")
        changed = base_intent()
        changed["decisions"]["what_hurt"]["summary"] = (
            "signal=review_rework; action=tighten_review"
        )
        with self.assertRaisesRegex(RETRO.RetroError, "immutable_intent_mismatch"):
            RETRO.prepare(self.repo.root, changed)

    def test_wrong_target_is_rejected_without_mutation(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        path = self.repo.artifact(prepared["artifact_fingerprint"])
        original = path.read_bytes()
        with self.assertRaisesRegex(RETRO.RetroError, "wrong_comment_target"):
            trusted_finalize(
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
            set(schema["properties"]),
            RETRO.ROOT_FIELDS,
        )
        self.assertEqual(
            set(schema["properties"]["routing"]["properties"]),
            RETRO.ROUTING_FIELDS,
        )
        self.assertEqual(
            set(schema["properties"]["routing"]["properties"]["proof"]["properties"]),
            RETRO.DELIVERY_PROOF_FIELDS,
        )
        self.assertFalse(any(key.startswith("x-hermes") for key in schema))
        self.assertEqual(
            schema["$defs"]["safeSummary"]["pattern"],
            RETRO.SAFE_SUMMARY_RE.pattern,
        )
        self.assertEqual(
            schema["$defs"]["evidenceRef"]["pattern"],
            RETRO.SAFE_REF_RE.pattern,
        )
        self.assertEqual(
            schema["properties"]["repo"]["pattern"],
            RETRO.SAFE_REPO_RE.pattern,
        )
        self.assertEqual(
            schema["$defs"]["rfcUuid"]["pattern"],
            RETRO.RFC_UUID_RE.pattern,
        )
        self.assertEqual(
            schema["$defs"]["trelloId"]["pattern"],
            RETRO.TRELLO_ID_RE.pattern,
        )
        self.assertEqual(
            schema["$defs"]["epochMicroseconds"]["maximum"],
            RETRO.MAX_EPOCH_US,
        )
        duplicated_computed_fields = {
            "artifact_fingerprint",
            "comment_fingerprint",
            "target_issue",
            "comment_fingerprint_marker",
            "comment_body",
        }
        self.assertTrue(duplicated_computed_fields.isdisjoint(document))

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
            "string_timestamp": (
                "routing.updated_at_epoch_us",
                "2026-07-27T00:00:00+00:00",
            ),
            "arbitrary_summary": (
                "decisions.what_hurt.summary",
                f"Slack {SYNTHETIC_SLACK_TOKEN}",
            ),
            "path_reference": (
                "protected_evidence_refs",
                ["home/alice/private/customer.log"],
            ),
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

    def test_standard_schema_rejects_every_runtime_rejected_serialized_document(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(any(key.startswith("x-hermes") for key in schema))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        prepared = RETRO.prepare(self.repo.root, base_intent())
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"])
        )
        mutations = {
            "wrong_issue_shape": ("source_issue", "PJAN-21"),
            "unsafe_reference": (
                "protected_evidence_refs",
                ["evidence:password:correct-horse-battery-staple"],
            ),
            "wrong_schema_version_type": ("schema_version", True),
            "operator_mismatch": ("operator_action_required", True),
        }
        for label, (field, value) in mutations.items():
            mutated = json.loads(json.dumps(document))
            mutated[field] = value
            with self.subTest(label=label):
                with self.assertRaises(RETRO.RetroError):
                    RETRO.validate_document(mutated)
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(mutated)

    def test_schema_runtime_parity_for_integral_numbers_and_final_newlines(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        prepared = RETRO.prepare(self.repo.root, base_intent())
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"])
        )

        integral_number = json.loads(json.dumps(document))
        integral_number["routing"]["updated_at_epoch_us"] = float(
            integral_number["routing"]["updated_at_epoch_us"]
        )
        self.assertTrue(validator.is_valid(integral_number))
        RETRO.validate_document(integral_number)

        for pointer in (
            ("repo",),
            ("decisions", "what_hurt", "summary"),
            ("protected_evidence_refs", 0),
        ):
            mutated = json.loads(json.dumps(document))
            target = mutated
            for part in pointer[:-1]:
                target = target[part]
            target[pointer[-1]] += "\n"
            with self.subTest(pointer=pointer):
                self.assertFalse(validator.is_valid(mutated))
                with self.assertRaisesRegex(RETRO.RetroError, "invalid_artifact"):
                    RETRO.validate_document(mutated)

    def test_schema_runtime_parity_for_closed_finalization_proof(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        prepared = RETRO.prepare(self.repo.root, base_intent())
        document = RETRO.read_artifact(
            self.repo.artifact(prepared["artifact_fingerprint"])
        )
        transition_id = "33333333-3333-4333-8333-333333333333"
        cases = []

        valid_terminal = json.loads(json.dumps(document))
        valid_terminal["routing"]["status"] = "posted"
        valid_terminal["routing"]["proof"] = {
            "status": "verified",
            "transition_id": transition_id,
        }
        cases.append(("valid_terminal", valid_terminal, True))

        unproved_terminal = json.loads(json.dumps(valid_terminal))
        unproved_terminal["routing"]["proof"] = {
            "status": "unverified",
            "transition_id": None,
        }
        cases.append(("unproved_terminal", unproved_terminal, False))

        proved_prepared = json.loads(json.dumps(document))
        proved_prepared["routing"]["proof"] = {
            "status": "verified",
            "transition_id": transition_id,
        }
        cases.append(("proved_prepared", proved_prepared, False))

        for label, candidate, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(validator.is_valid(candidate), expected)
                try:
                    RETRO.validate_document(candidate)
                except RETRO.RetroError:
                    runtime_valid = False
                else:
                    runtime_valid = True
                self.assertEqual(runtime_valid, expected)

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

    def test_repository_identity_rejects_protected_credential_shapes(self):
        protected = [
            SYNTHETIC_SLACK_TOKEN,
            SYNTHETIC_GOOGLE_KEY,
            SYNTHETIC_STRIPE_SECRET,
            SYNTHETIC_AWS_ACCESS_KEY,
            "".join(("gh", "p_", "1234567890abcdefghijklmnopqrstuvwxyz")),
        ]
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        repo_validator = jsonschema.Draft202012Validator(
            {"type": "string", "pattern": schema["properties"]["repo"]["pattern"]}
        )
        for value in protected:
            (self.repo.root / ".project.json").write_text(
                json.dumps(
                    {
                        "project_name": value,
                        "ticket_provider": {"type": "plane"},
                    }
                ),
                encoding="utf-8",
            )
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RETRO.RetroError, "invalid_repository_identity"
                ):
                    RETRO.canonical_repo_identity(self.repo.root)
                self.assertFalse(repo_validator.is_valid(value.casefold()))

    def test_repository_identity_rejects_embedded_protected_credential_shapes(self):
        protected = [
            f"repo-{SYNTHETIC_SLACK_TOKEN}",
            f"label-{SYNTHETIC_GOOGLE_KEY}",
            f"service-{SYNTHETIC_STRIPE_SECRET}",
            f"account-{SYNTHETIC_AWS_ACCESS_KEY}",
            "mirror-" + "".join(("gh", "p_", "1234567890abcdefghijklmnopqrstuvwxyz")),
        ]
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        repo_validator = jsonschema.Draft202012Validator(
            {"type": "string", "pattern": schema["properties"]["repo"]["pattern"]}
        )
        for value in protected:
            (self.repo.root / ".project.json").write_text(
                json.dumps(
                    {
                        "project_name": value,
                        "ticket_provider": {"type": "plane"},
                    }
                ),
                encoding="utf-8",
            )
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RETRO.RetroError, "invalid_repository_identity"
                ):
                    RETRO.canonical_repo_identity(self.repo.root)
                self.assertFalse(repo_validator.is_valid(value.casefold()))

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
        trusted_finalize(self.repo.root, fingerprint, success)
        result = trusted_finalize(self.repo.root, fingerprint, stale)
        self.assertEqual(result["status"], "posted")
        self.assertEqual(result["transition"], "preserved_terminal")
        document = RETRO.read_artifact(
            self.repo.artifact(fingerprint), require_final=True
        )
        self.assertEqual(document["routing"]["status"], "posted")

    def test_final_checkpoint_rejects_failed_delivery(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        trusted_finalize(
            self.repo.root,
            prepared["artifact_fingerprint"],
            {
                "provider": "plane",
                "status": "failed",
                "target_issue": ISSUE_ID,
                "error_category": "response_unknown",
                "error_summary": "provider response not confirmed",
            },
        )
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "validate",
                "--repo-root",
                str(self.repo.root),
                "--artifact-fingerprint",
                prepared["artifact_fingerprint"],
                "--final",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["status"], "stalled")

    def test_final_checkpoint_rejects_forged_terminal_status_without_transition(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        artifact = self.repo.artifact(prepared["artifact_fingerprint"])
        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["routing"] = {
            "status": "posted",
            "error_category": None,
            "updated_at_epoch_us": document["routing"]["updated_at_epoch_us"] + 1,
            "proof": {
                "status": "verified",
                "transition_id": "33333333-3333-4333-8333-333333333333",
            },
        }
        artifact.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RETRO.RetroError, "invalid_artifact"):
            RETRO.read_artifact(artifact, require_final=True)

    def test_close_gate_rejects_forged_terminal_status_without_transition(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        artifact = self.repo.artifact(prepared["artifact_fingerprint"])
        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["routing"]["status"] = "posted"
        document["routing"]["updated_at_epoch_us"] += 1
        document["routing"]["proof"] = {
            "status": "verified",
            "transition_id": "33333333-3333-4333-8333-333333333333",
        }
        artifact.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        evidence = (
            self.repo.root
            / "_bmad-output"
            / "implementation-artifacts"
            / "issue-evidence"
            / "PJAN-21.md"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            textwrap.dedent(
                """\
                ## Issue
                PJAN-21
                ## Acceptance Criteria
                Locked.
                ## Repo Changes
                Scoped.
                ## Verification
                Verified.
                ## Ledger Update
                Ledger updated: yes
                ## Known Gaps
                None material.
                ## Close Recommendation
                Close recommendation: ready
                """
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["sh", str(CLOSE_GATE_PATH), "PJAN-21", str(self.repo.root)],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "TMPDIR": str(self.repo.root)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Retro finalization proof failed.", result.stderr)

    def test_close_gate_accepts_provider_finalized_bound_transition(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        trusted_finalize(
            self.repo.root,
            prepared["artifact_fingerprint"],
            {
                "provider": "plane",
                "status": "posted",
                "target_issue": ISSUE_ID,
                "error_category": None,
                "error_summary": None,
            },
        )
        evidence = (
            self.repo.root
            / "_bmad-output"
            / "implementation-artifacts"
            / "issue-evidence"
            / "PJAN-21.md"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            textwrap.dedent(
                """\
                ## Issue
                PJAN-21
                ## Acceptance Criteria
                Locked.
                ## Repo Changes
                Scoped.
                ## Verification
                Verified.
                ## Ledger Update
                Ledger updated: yes
                ## Known Gaps
                None material.
                ## Close Recommendation
                Close recommendation: ready
                """
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["sh", str(CLOSE_GATE_PATH), "PJAN-21", str(self.repo.root)],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "TMPDIR": str(self.repo.root)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLOSE GATE: PASS for PJAN-21", result.stdout)

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
            if operation & RETRO.fcntl.LOCK_EX and not swapped:
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

    def test_relocated_store_is_rejected_before_any_new_artifact_write(self):
        relocated = self.repo.root.parent / f"{self.repo.root.name}-relocated-retros"
        self.addCleanup(shutil.rmtree, relocated, True)
        original_writer = RETRO._write_exclusive_temp
        original_open = RETRO.os.open
        create_attempts = []
        swapped = False

        def swap_then_write(*args, **kwargs):
            nonlocal swapped
            retro = (
                self.repo.root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
            )
            retro.rename(relocated)
            swapped = True
            return original_writer(*args, **kwargs)

        def observe_open(path, flags, *args, **kwargs):
            if swapped and flags & RETRO.os.O_CREAT:
                create_attempts.append(str(path))
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(
                RETRO, "_write_exclusive_temp", side_effect=swap_then_write
            ),
            mock.patch.object(RETRO.os, "open", side_effect=observe_open),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(create_attempts, [])

    def test_relocation_at_temp_create_never_exposes_a_transient_outside_repo(self):
        relocated = self.repo.root.parent / f"{self.repo.root.name}-temp-window"
        self.addCleanup(shutil.rmtree, relocated, True)
        original_open = RETRO.os.open
        observed_components = []
        swapped = False

        def swap_during_temp_create(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and flags & RETRO.os.O_CREAT and ".json.tmp." in str(path):
                retro = (
                    self.repo.root
                    / "_bmad-output"
                    / "implementation-artifacts"
                    / "run-retros"
                )
                retro.rename(relocated)
                swapped = True
                try:
                    descriptor = original_open(path, flags, *args, **kwargs)
                finally:
                    observed_components.append(str(path))
                return descriptor
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(RETRO.os, "open", side_effect=swap_during_temp_create),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertTrue(swapped)
        self.assertTrue(observed_components)
        self.assertTrue(all("/" not in item for item in observed_components))
        self.assertFalse(any(".json.tmp." in item.name for item in relocated.iterdir()))

    def test_relocation_at_durable_replace_never_updates_artifact_outside_repo(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        relocated = self.repo.root.parent / f"{self.repo.root.name}-replace-window"
        self.addCleanup(shutil.rmtree, relocated, True)
        original_replace = RETRO.os.replace
        swapped = False

        def swap_during_replace(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                retro = (
                    self.repo.root
                    / "_bmad-output"
                    / "implementation-artifacts"
                    / "run-retros"
                )
                retro.rename(relocated)
                swapped = True
            return original_replace(*args, **kwargs)

        with (
            mock.patch.object(RETRO.os, "replace", side_effect=swap_during_replace),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            trusted_finalize(
                self.repo.root,
                prepared["artifact_fingerprint"],
                {
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": ISSUE_ID,
                    "error_category": None,
                    "error_summary": None,
                },
            )
        self.assertTrue(swapped)
        self.assertFalse(self.repo.artifact(prepared["artifact_fingerprint"]).exists())

    def test_relocated_store_is_rejected_before_immutable_binding_write(self):
        relocated = self.repo.root.parent / f"{self.repo.root.name}-relocated-binding"
        self.addCleanup(shutil.rmtree, relocated, True)
        original_binding = RETRO._ensure_binding
        original_open = RETRO.os.open
        create_attempts = []
        swapped = False

        def swap_then_bind(*args, **kwargs):
            nonlocal swapped
            retro = (
                self.repo.root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
            )
            retro.rename(relocated)
            swapped = True
            return original_binding(*args, **kwargs)

        def observe_open(path, flags, *args, **kwargs):
            if swapped and flags & RETRO.os.O_CREAT:
                create_attempts.append(str(path))
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(RETRO, "_ensure_binding", side_effect=swap_then_bind),
            mock.patch.object(RETRO.os, "open", side_effect=observe_open),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(create_attempts, [])

    def test_repository_root_replacement_cannot_redirect_binding_creation(self):
        root = self.repo.root
        held_root = root.with_name(f"{root.name}-held-binding-root")
        original_open = RETRO.os.open
        swapped = False

        def replace_root_at_binding_create(path, flags, *args, **kwargs):
            nonlocal swapped
            directory_fd = kwargs.get("dir_fd")
            try:
                directory_path = os.readlink(f"/proc/self/fd/{directory_fd}")
            except (OSError, TypeError):
                directory_path = ""
            if (
                not swapped
                and flags & RETRO.os.O_CREAT
                and (
                    ".sha256" in str(path)
                    or pathlib.Path(directory_path).name == ".bindings"
                )
            ):
                root.rename(held_root)
                root.mkdir()
                shutil.copy2(held_root / ".project.json", root / ".project.json")
                (
                    root
                    / "_bmad-output"
                    / "implementation-artifacts"
                    / "run-retros"
                    / ".bindings"
                ).mkdir(parents=True)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(
                RETRO.os,
                "open",
                side_effect=replace_root_at_binding_create,
            ),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            RETRO.prepare(root, base_intent())

        self.assertTrue(swapped)
        replacement_bindings = (
            root
            / "_bmad-output"
            / "implementation-artifacts"
            / "run-retros"
            / ".bindings"
        )
        self.assertEqual(list(replacement_bindings.iterdir()), [])

    def test_repository_root_replacement_cannot_redirect_durable_replace(self):
        prepared = RETRO.prepare(self.repo.root, base_intent())
        root = self.repo.root
        held_root = root.with_name(f"{root.name}-held-replace-root")
        original_replace = RETRO.os.replace
        swapped = False

        def replace_root_at_durable_replace(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                root.rename(held_root)
                root.mkdir()
                shutil.copy2(held_root / ".project.json", root / ".project.json")
                shutil.copytree(
                    held_root / "_bmad-output",
                    root / "_bmad-output",
                )
                swapped = True
            return original_replace(*args, **kwargs)

        with (
            mock.patch.object(
                RETRO.os,
                "replace",
                side_effect=replace_root_at_durable_replace,
            ),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            trusted_finalize(
                root,
                prepared["artifact_fingerprint"],
                {
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": ISSUE_ID,
                    "error_category": None,
                    "error_summary": None,
                },
            )

        self.assertTrue(swapped)
        replacement = self.repo.artifact(prepared["artifact_fingerprint"])
        self.assertEqual(
            json.loads(replacement.read_text(encoding="utf-8"))["routing"]["status"],
            "prepared",
        )

    def test_relocated_store_is_rejected_before_any_new_lock_write(self):
        relocated = self.repo.root.parent / f"{self.repo.root.name}-relocated-locks"
        self.addCleanup(shutil.rmtree, relocated, True)
        retro = (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )
        retro.mkdir(parents=True)
        original_lock = RETRO._artifact_lock
        original_open = RETRO.os.open
        create_attempts = []
        swapped = False

        @contextlib.contextmanager
        def swap_then_lock(*args, **kwargs):
            nonlocal swapped
            retro.rename(relocated)
            swapped = True
            with original_lock(*args, **kwargs):
                yield

        def observe_open(path, flags, *args, **kwargs):
            if swapped and flags & RETRO.os.O_CREAT:
                create_attempts.append(str(path))
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(RETRO, "_artifact_lock", swap_then_lock),
            mock.patch.object(RETRO.os, "open", side_effect=observe_open),
            self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
        ):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(create_attempts, [])

    def test_symlinked_immutable_binding_is_rejected_without_truncation(self):
        retro = (
            self.repo.root / "_bmad-output" / "implementation-artifacts" / "run-retros"
        )
        bindings = retro / ".bindings"
        bindings.mkdir(parents=True)
        fingerprint = RETRO.artifact_fingerprint("pjangler", base_intent()["run_id"])
        victim = self.repo.root / "binding-victim"
        victim.write_text("DO NOT TRUNCATE\n", encoding="utf-8")
        (bindings / f"{fingerprint}.sha256").symlink_to(victim)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TRUNCATE\n")

    def test_every_persisted_reference_shape_is_closed_and_opaque(self):
        unsafe_fields = {
            "run_id": SYNTHETIC_AWS_ACCESS_KEY,
            "correlation_id": "123-45-6789",
            "local_tracking_reference": SYNTHETIC_STRIPE_SECRET,
            "protected_evidence_refs": [
                "evidence:password:correct-horse-battery-staple"
            ],
        }
        for field, value in unsafe_fields.items():
            intent = base_intent()
            intent[field] = value
            with self.subTest(field=field), self.assertRaises(RETRO.RetroError):
                RETRO.prepare(self.repo.root, intent)

    def test_repository_configuration_is_read_through_the_held_descriptor(self):
        with mock.patch.object(
            RETRO,
            "_read_utf8_json",
            side_effect=AssertionError("pathname configuration reopen"),
        ):
            prepared = RETRO.prepare(self.repo.root, base_intent())
        self.assertEqual(prepared["status"], "prepared")

    def test_cli_input_and_artifact_reads_are_bounded(self):
        input_limit = getattr(RETRO, "MAX_INPUT_BYTES", 64 * 1024)
        artifact_limit = getattr(RETRO, "MAX_ARTIFACT_BYTES", 64 * 1024)
        oversized_input = self.repo.root / "oversized-input.json"
        oversized_input.write_bytes(b"{" + (b" " * input_limit) + b"}")
        prepare = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "prepare",
                "--repo-root",
                str(self.repo.root),
                "--intent",
                str(oversized_input),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepare.returncode, 3)
        self.assertEqual(
            json.loads(prepare.stdout)["error_category"], "input_too_large"
        )
        self.assertEqual(prepare.stderr, "")
        stdin_prepare = subprocess.run(
            [
                sys.executable,
                str(HELPER_PATH),
                "prepare",
                "--repo-root",
                str(self.repo.root),
                "--intent",
                "-",
            ],
            input=b"{" + (b" " * input_limit) + b"}",
            capture_output=True,
            check=False,
        )
        self.assertEqual(stdin_prepare.returncode, 3)
        self.assertEqual(
            json.loads(stdin_prepare.stdout.decode("utf-8"))["error_category"],
            "input_too_large",
        )
        self.assertEqual(stdin_prepare.stderr, b"")

        prepared = RETRO.prepare(
            self.repo.root,
            base_intent("00000000-0000-4000-8000-000000000099"),
        )
        artifact = self.repo.artifact(prepared["artifact_fingerprint"])
        artifact.write_bytes(b"{" + (b" " * artifact_limit) + b"}")
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
            json.loads(validate.stdout)["error_category"], "artifact_too_large"
        )
        self.assertEqual(validate.stderr, "")


class ProviderTrustBoundaryTests(unittest.TestCase):
    class Handler(http.server.BaseHTTPRequestHandler):
        calls = 0
        delayed_calls = 0
        payloads = []

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = self.rfile.read(length)
            if self.path == "/delayed":
                type(self).delayed_calls += 1
            else:
                type(self).calls += 1
                type(self).payloads.append(json.loads(request or b"{}"))
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    def setUp(self):
        self.repo = RepoFixture()
        self.providers = self.repo.root / "providers"
        self.providers.mkdir()
        self.provider = self.providers / "plane.sh"
        self.controller_marker_effect = (
            self.repo.root.parent / f"{self.repo.root.name}-controller-marker"
        )
        self.outside_effect = (
            self.repo.root.parent / f"{self.repo.root.name}-outside-effect"
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.Handler.calls = 0
        self.Handler.delayed_calls = 0
        self.Handler.payloads = []
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()
        project = json.loads((self.repo.root / ".project.json").read_text())
        project["ticket_provider"].update(
            {
                "test_endpoint": (
                    f"http://127.0.0.1:{self.server.server_port}/provider"
                ),
                "controller_marker_effect": str(self.controller_marker_effect),
                "outside_effect": str(self.outside_effect),
            }
        )
        (self.repo.root / ".project.json").write_text(
            json.dumps(project) + "\n",
            encoding="utf-8",
        )
        (self.repo.root / ".project.json").chmod(0o644)
        self.providers.chmod(0o755)
        self.provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                issue="$2"
                python3 - "$issue" <<'PY'
                import json
                import os
                import pathlib
                import sys
                import urllib.request

                config = json.loads(os.environ["HERMES_BOUND_TICKET_PROVIDER_JSON"])
                marker = os.environ.get("PJAN21_CONTROLLER_ONLY_MARKER")
                if marker:
                    pathlib.Path(config["controller_marker_effect"]).write_text(marker)
                pathlib.Path(config["outside_effect"]).write_text("outside")
                request = urllib.request.Request(
                    config["test_endpoint"], data=b"{}", method="POST"
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read()
                print(json.dumps({
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": sys.argv[1],
                    "error_category": None,
                    "error_summary": None,
                }, separators=(",", ":")))
                PY
                """
            ),
            encoding="utf-8",
        )
        self.provider.chmod(0o755)
        prepared = RETRO.prepare(self.repo.root, base_intent())
        self.fingerprint = prepared["artifact_fingerprint"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        for path in (self.controller_marker_effect, self.outside_effect):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        self.repo.close()

    def _assert_rejected_before_launch(self):
        with mock.patch.dict(
            os.environ,
            {"PJAN21_CONTROLLER_ONLY_MARKER": "synthetic-controller-only"},
            clear=False,
        ):
            result = RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        stored = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(stored["routing"]["status"], "posted")
        self.assertFalse(self.controller_marker_effect.exists())
        self.assertFalse(self.outside_effect.exists())
        self.assertEqual(self.Handler.calls, 0)

    def _write_endpoint_only_provider(self):
        self.provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                issue="$2"
                python3 - "$issue" <<'PY'
                import json
                import os
                import sys
                import urllib.request

                config = json.loads(os.environ["HERMES_BOUND_TICKET_PROVIDER_JSON"])
                request = urllib.request.Request(
                    config["test_endpoint"], data=b"{}", method="POST"
                )
                urllib.request.urlopen(request, timeout=2).read()
                print(json.dumps({
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": sys.argv[1],
                    "error_category": None,
                    "error_summary": None,
                }, separators=(",", ":")))
                PY
                """
            ),
            encoding="utf-8",
        )
        self.provider.chmod(0o755)

    def _artifact_and_binding(self):
        artifact = self.repo.artifact(self.fingerprint)
        binding = artifact.parent / ".bindings" / f"{self.fingerprint}.sha256"
        return artifact, binding

    def _forged_target_documents(self):
        artifact, _ = self._artifact_and_binding()
        forged = json.loads(artifact.read_text(encoding="utf-8"))
        forged["source_issue"] = OTHER_ISSUE_ID
        binding = RETRO._prepared_binding(forged)
        return forged, binding

    def _assert_delivery_was_blocked(self, result, error):
        artifact, _ = self._artifact_and_binding()
        self.assertIsNotNone(error)
        self.assertIsNone(result)
        try:
            payload = artifact.read_text(encoding="utf-8")
        except PermissionError:
            readback = subprocess.run(
                ["sudo", "-n", "cat", str(artifact)],
                check=True,
                text=True,
                capture_output=True,
            )
            payload = readback.stdout
        document = json.loads(payload)
        self.assertNotEqual(document["routing"]["status"], "posted")
        self.assertEqual(self.Handler.calls, 0)
        self.assertFalse(self.controller_marker_effect.exists())
        self.assertFalse(self.outside_effect.exists())

    def test_writable_storage_forgery_cannot_redirect_provider_or_finalize(self):
        self._write_endpoint_only_provider()
        artifact, binding = self._artifact_and_binding()
        forged, forged_binding = self._forged_target_documents()
        artifact.write_bytes(RETRO._canonical_json(forged))
        binding.write_bytes(RETRO._canonical_json(forged_binding))
        artifact.parent.chmod(0o775)
        binding.parent.chmod(0o775)
        artifact.chmod(0o664)
        binding.chmod(0o664)
        result = None
        error = None
        try:
            result = RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        except RETRO.RetroError as caught:
            error = caught
        self._assert_delivery_was_blocked(result, error)

    def test_foreign_owned_artifact_and_binding_cannot_reach_provider(self):
        if subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
        ).returncode:
            self.skipTest("non-interactive sudo unavailable for foreign-owner probe")
        self._write_endpoint_only_provider()
        artifact, binding = self._artifact_and_binding()
        subprocess.run(
            [
                "sudo",
                "-n",
                "chown",
                "65534:65534",
                str(artifact),
                str(binding),
            ],
            check=True,
            capture_output=True,
        )
        result = None
        error = None
        try:
            result = RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        except RETRO.RetroError as caught:
            error = caught
        self._assert_delivery_was_blocked(result, error)

    def test_foreign_path_swap_after_read_cannot_redirect_provider(self):
        if subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
        ).returncode:
            self.skipTest("non-interactive sudo unavailable for foreign-swap probe")
        self._write_endpoint_only_provider()
        artifact, binding = self._artifact_and_binding()
        forged, forged_binding = self._forged_target_documents()
        forged_artifact = artifact.with_name(f".foreign-{artifact.name}")
        forged_binding_path = binding.with_name(f".foreign-{binding.name}")
        forged_artifact.write_bytes(RETRO._canonical_json(forged))
        forged_binding_path.write_bytes(RETRO._canonical_json(forged_binding))
        original_provider_context = RETRO._provider_script_fd

        @contextlib.contextmanager
        def swap_after_initial_read(*args, **kwargs):
            with original_provider_context(*args, **kwargs) as descriptor:
                subprocess.run(
                    [
                        "sudo",
                        "-n",
                        sys.executable,
                        "-c",
                        (
                            "import os,sys;"
                            "pairs=((sys.argv[1],sys.argv[2]),"
                            "(sys.argv[3],sys.argv[4]));"
                            "[(os.chown(src,0,0),os.chmod(src,0o600),"
                            "os.replace(src,dst)) for src,dst in pairs]"
                        ),
                        str(forged_artifact),
                        str(artifact),
                        str(forged_binding_path),
                        str(binding),
                    ],
                    check=True,
                    capture_output=True,
                )
                yield descriptor

        result = None
        error = None
        try:
            with mock.patch.object(
                RETRO,
                "_provider_script_fd",
                swap_after_initial_read,
            ):
                result = RETRO.deliver(
                    self.repo.root,
                    self.fingerprint,
                    providers_dir=self.providers,
                )
        except RETRO.RetroError as caught:
            error = caught
        self._assert_delivery_was_blocked(result, error)

    def test_project_configuration_descriptor_remains_bound_through_launch(self):
        with RETRO._repository(self.repo.root) as repository:
            self.assertGreaterEqual(repository.project_fd, 0)
            metadata = os.fstat(repository.project_fd)
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                repository.project_identity,
            )
            replacement = self.repo.root / ".project.json.replacement"
            replacement.write_bytes((self.repo.root / ".project.json").read_bytes())
            replacement.chmod(0o644)
            os.replace(replacement, self.repo.root / ".project.json")
            with self.assertRaisesRegex(
                RETRO.RetroError,
                "invalid_repository_identity|immutable_intent_mismatch",
            ):
                RETRO._assert_repository_inputs(repository)

    def test_world_writable_controller_component_is_rejected(self):
        controller_root = self.repo.root / "controller"
        controller_bin = controller_root / "bin"
        controller_bin.mkdir(parents=True)
        controller = controller_bin / "run-retro.py"
        shutil.copy2(HELPER_PATH, controller)
        controller.chmod(0o755)
        controller_root.chmod(0o777)
        with RETRO._repository(self.repo.root) as repository:
            with (
                mock.patch.object(RETRO, "__file__", str(controller)),
                self.assertRaisesRegex(
                    RETRO.RetroError,
                    "provider_containment_unavailable",
                ),
            ):
                descriptor = RETRO._controller_source_fd(repository)
                os.close(descriptor)

    def test_mode_0666_non_executable_provider_is_rejected_before_launch(self):
        self.provider.chmod(0o666)
        self._assert_rejected_before_launch()

    def test_world_writable_provider_component_is_rejected_before_launch(self):
        self.providers.chmod(0o777)
        self._assert_rejected_before_launch()

    def test_world_writable_repository_anchor_is_rejected_before_launch(self):
        self.repo.root.chmod(0o777)
        with self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"):
            RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        stored = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        self.assertNotEqual(stored["routing"]["status"], "posted")
        self.assertFalse(self.controller_marker_effect.exists())
        self.assertFalse(self.outside_effect.exists())
        self.assertEqual(self.Handler.calls, 0)

    def test_group_writable_project_configuration_is_rejected_before_launch(self):
        (self.repo.root / ".project.json").chmod(0o664)
        with mock.patch.dict(
            os.environ,
            {"PJAN21_CONTROLLER_ONLY_MARKER": "synthetic-controller-only"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                RETRO.RetroError,
                "invalid_repository_identity",
            ):
                RETRO.deliver(
                    self.repo.root,
                    self.fingerprint,
                    providers_dir=self.providers,
                )
        stored = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        self.assertNotEqual(stored["routing"]["status"], "posted")
        self.assertFalse(self.controller_marker_effect.exists())
        self.assertFalse(self.outside_effect.exists())
        self.assertEqual(self.Handler.calls, 0)

    def test_controller_source_permissions_are_revalidated_before_launch(self):
        controller = pathlib.Path(RETRO.__file__)
        original_mode = stat.S_IMODE(controller.stat().st_mode)
        try:
            controller.chmod(0o666)
            result = RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        finally:
            controller.chmod(original_mode)
        stored = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(stored["routing"]["status"], "posted")
        self.assertEqual(self.Handler.calls, 0)

    def test_provider_environment_is_provider_specific_and_controller_marker_free(self):
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/synthetic/controller/path",
                "TMPDIR": str(self.repo.root),
                "PLANE_API_KEY": "synthetic-plane-value",
                "LINEAR_API_KEY": "synthetic-linear-value",
                "TRELLO_KEY": "synthetic-trello-key",
                "TRELLO_TOKEN": "synthetic-trello-value",
                "PJAN21_CONTROLLER_ONLY_MARKER": "synthetic-controller-only",
            },
            clear=True,
        ):
            environments = {
                provider: RETRO._provider_environment(
                    provider,
                    {"type": provider},
                )
                for provider in ("linear", "plane", "trello")
            }
        expected = {
            "linear": {"LINEAR_API_KEY"},
            "plane": {"PLANE_API_KEY"},
            "trello": {"TRELLO_KEY", "TRELLO_TOKEN"},
        }
        all_secrets = set().union(*expected.values())
        for provider, environment in environments.items():
            with self.subTest(provider=provider):
                self.assertTrue(expected[provider].issubset(environment))
                self.assertFalse(
                    (all_secrets - expected[provider]) & environment.keys()
                )
                self.assertNotIn("PJAN21_CONTROLLER_ONLY_MARKER", environment)
                self.assertNotIn("TMPDIR", environment)
                self.assertEqual(environment["TICKET_PROVIDER"], provider)
                self.assertEqual(environment["PATH"], os.defpath)

    def test_read_only_root_uses_ephemeral_temp_and_cleans_provider_subtree(self):
        self.provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                issue="$2"
                (
                  sleep 0.6
                  python3 - <<'PY'
                import json
                import os
                import urllib.request
                config = json.loads(os.environ["HERMES_BOUND_TICKET_PROVIDER_JSON"])
                request = urllib.request.Request(
                    config["test_endpoint"].replace("/provider", "/delayed"),
                    data=b"{}",
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=2).read()
                PY
                ) &
                python3 - "$issue" <<'PY'
                import json
                import os
                import pathlib
                import sys
                import urllib.request

                config = json.loads(os.environ["HERMES_BOUND_TICKET_PROVIDER_JSON"])
                temp_path = pathlib.Path(os.environ["TMPDIR"])
                temp_probe = temp_path / "provider-temp-probe"
                temp_probe.write_text("temporary")
                outside_blocked = False
                try:
                    pathlib.Path(config["outside_effect"]).write_text("outside")
                except OSError:
                    outside_blocked = True
                observation = {
                    "controller_marker_absent": (
                        "PJAN21_CONTROLLER_ONLY_MARKER" not in os.environ
                    ),
                    "outside_blocked": outside_blocked,
                    "temp_path": str(temp_path),
                    "temp_write": temp_probe.read_text() == "temporary",
                }
                request = urllib.request.Request(
                    config["test_endpoint"],
                    data=json.dumps(observation).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read()
                print(json.dumps({
                    "provider": "plane",
                    "status": "posted",
                    "target_issue": sys.argv[1],
                    "error_category": None,
                    "error_summary": None,
                }, separators=(",", ":")))
                PY
                """
            ),
            encoding="utf-8",
        )
        self.provider.chmod(0o755)
        with mock.patch.dict(
            os.environ,
            {"PJAN21_CONTROLLER_ONLY_MARKER": "synthetic-controller-only"},
            clear=False,
        ):
            result = RETRO.deliver(
                self.repo.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
        self.assertEqual(result["status"], "posted")
        self.assertEqual(self.Handler.calls, 1)
        observation = self.Handler.payloads[0]
        self.assertTrue(observation["controller_marker_absent"])
        self.assertTrue(observation["outside_blocked"])
        self.assertTrue(observation["temp_write"])
        self.assertFalse(pathlib.Path(observation["temp_path"]).exists())
        time.sleep(0.8)
        self.assertEqual(self.Handler.delayed_calls, 0)
        self.assertFalse(self.outside_effect.exists())


class AdapterEnsureCommentTests(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture()
        self.root = self.repo.root
        self.providers = self.root / "providers"
        self.providers.mkdir()
        self.providers.chmod(0o755)
        self.store = self.root / "comments"
        self.store.touch()
        self.calls = self.root / "calls"
        self.calls.touch()
        self._configure_provider(
            FAKE_COMMENT_STORE=str(self.store),
            FAKE_CALL_STORE=str(self.calls),
        )
        fake = self.providers / "plane.sh"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                op="$1"; shift
                [ "$op" = ensure_comment ] || exit 2
                issue="$1"; marker="$2"; body="$3"
                printf '%s|%s|%s\\n' "${TICKET_PROVIDER:-unset}" "$issue" "$marker" >> "$HERMES_PROVIDER_CONFIG_FAKE_CALL_STORE"
                if grep -Fq "$marker" "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"; then
                  printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                sleep 0.1
                printf '%s\\n' "$body" >> "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"
                if [ "${HERMES_PROVIDER_CONFIG_FAKE_LOST_RESPONSE_ONCE:-0}" = 1 ] && [ ! -e "$HERMES_PROVIDER_CONFIG_FAKE_LOST_MARK" ]; then
                  : > "$HERMES_PROVIDER_CONFIG_FAKE_LOST_MARK"
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
                printf 'WRONG_PROVIDER_CALLED\\n' >> "$HERMES_PROVIDER_CONFIG_FAKE_CALL_STORE"
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

    def _configure_provider(self, **values):
        project_path = self.root / ".project.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["ticket_provider"].update(
            {name.casefold(): value for name, value in values.items()}
        )
        project_path.write_text(
            json.dumps(project) + "\n",
            encoding="utf-8",
        )
        project_path.chmod(0o644)

    def call_delivery(self, extra_env=None):
        self._configure_provider(**(extra_env or {}))
        with mock.patch.dict(
            os.environ,
            {
                "TICKET_PROVIDER": "trello",
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
                if grep -Fq "$marker" "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"; then
                  printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                if mkdir "$HERMES_PROVIDER_CONFIG_FAKE_FIRST_CLAIM" 2>/dev/null; then
                  : > "$HERMES_PROVIDER_CONFIG_FAKE_PROVIDER_STARTED"
                  sleep 1.5
                  printf '%s\\n' "$body" >> "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"
                  printf 'post\\n' >> "$HERMES_PROVIDER_CONFIG_FAKE_POST_STORE"
                  printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                printf 'duplicate-post-attempt\\n' >> "$HERMES_PROVIDER_CONFIG_FAKE_POST_STORE"
                printf '%s\\n' "$body" >> "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        self._configure_provider(
            FAKE_COMMENT_STORE=str(self.store),
            FAKE_PROVIDER_STARTED=str(started),
            FAKE_FIRST_CLAIM=str(first_claim),
            FAKE_POST_STORE=str(posts),
        )
        command = [
            sys.executable,
            str(self.repo.controller),
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

    def test_artifact_lock_acquisition_is_bounded(self):
        lock_path = (
            self.root
            / "_bmad-output"
            / "implementation-artifacts"
            / "run-retros"
            / ".locks"
            / "artifacts"
            / f"{self.fingerprint}.lock"
        )
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,os,sys,time;"
                    "fd=os.open(sys.argv[1],os.O_RDWR);"
                    "fcntl.flock(fd,fcntl.LOCK_EX);"
                    "print('locked',flush=True);"
                    "time.sleep(10)"
                ),
                str(lock_path),
            ],
            text=True,
            stdout=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            holder.stdout.close()
            intent = self.root / "bounded-lock-intent.json"
            intent.write_text(json.dumps(base_intent()) + "\n", encoding="utf-8")
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    str(HELPER_PATH),
                    "prepare",
                    "--repo-root",
                    str(self.root),
                    "--intent",
                    str(intent),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
                env={
                    **os.environ,
                    "HERMES_LOCK_TIMEOUT_SECONDS": "0.2",
                    "TMPDIR": str(self.root),
                },
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)["error_category"], "lock_timeout"
            )
            self.assertLess(elapsed, 1.0)
        finally:
            holder.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                holder.wait(timeout=1)
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=1)

    def test_independent_comment_keys_do_not_serialize(self):
        changed = base_intent("00000000-0000-4000-8000-000000000002")
        changed["decisions"]["what_should_change"]["summary"] = (
            "signal=environment_drift; action=stabilize_environment"
        )
        second = RETRO.prepare(self.root, changed)
        self.assertNotEqual(
            self.prepared["comment_fingerprint_marker"],
            second["comment_fingerprint_marker"],
        )
        provider = self.providers / "plane.sh"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                sleep 0.8
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$2"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)

        def deliver(fingerprint):
            return RETRO.deliver(
                self.root,
                fingerprint,
                providers_dir=self.providers,
            )

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    deliver,
                    [self.fingerprint, second["artifact_fingerprint"]],
                )
            )
        elapsed = time.monotonic() - started
        self.assertEqual([result["status"] for result in results], ["posted", "posted"])
        self.assertLess(elapsed, 1.4)

    def test_success_contains_double_fork_after_all_inherited_descriptors_close(self):
        provider = self.providers / "plane.sh"
        descendant_pid = self.root / "closed-fd-descendant.pid"
        ready = self.root / "closed-fd-ready"
        delayed_side_effect = self.root / "closed-fd-delayed-side-effect"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                python3 - "$HERMES_PROVIDER_CONFIG_DESCENDANT_PID" "$HERMES_PROVIDER_CONFIG_DESCENDANT_READY" "$HERMES_PROVIDER_CONFIG_DELAYED_SIDE_EFFECT" <<'PY' >/dev/null 2>&1 &
                import os, pathlib, resource, signal, sys, time
                child = os.fork()
                if child:
                    os._exit(0)
                os.setsid()
                child = os.fork()
                if child:
                    os._exit(0)
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                pid_path, ready_path, effect_path = sys.argv[1:]
                soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                for descriptor in range(0, min(4096, soft_limit)):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                pathlib.Path(pid_path).write_text(str(os.getpid()))
                pathlib.Path(ready_path).write_text("ready")
                time.sleep(0.8)
                pathlib.Path(effect_path).write_text("escaped")
                time.sleep(10)
                PY
                deadline=200
                while [ ! -e "$HERMES_PROVIDER_CONFIG_DESCENDANT_READY" ] && [ "$deadline" -gt 0 ]; do
                  sleep 0.01
                  deadline=$((deadline - 1))
                done
                [ -e "$HERMES_PROVIDER_CONFIG_DESCENDANT_READY" ] || exit 91
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$2"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        result = self.call_delivery(
            {
                "DESCENDANT_PID": str(descendant_pid),
                "DESCENDANT_READY": str(ready),
                "DELAYED_SIDE_EFFECT": str(delayed_side_effect),
            }
        )
        self.assertEqual(result["status"], "posted")
        self.assertTrue(descendant_pid.exists())
        self.assertGreater(int(descendant_pid.read_text(encoding="utf-8")), 1)
        time.sleep(1.0)
        self.assertFalse(delayed_side_effect.exists())

    def test_containment_info_failure_is_bounded_and_fail_closed(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                RETRO.RetroError,
                "provider_containment_unavailable",
            ):
                RETRO._read_containment_info(read_fd)
        finally:
            os.close(read_fd)
        self.assertLess(time.monotonic() - started, 0.5)

    def _run_actual_containment_failure_probe(self, failing_symbol):
        if sys.platform != "linux":
            self.skipTest("Bubblewrap PID containment is a Linux guarantee")
        try:
            RETRO._containment_executable()
        except RETRO.RetroError:
            self.skipTest("trusted Bubblewrap is unavailable")
        effect = self.root / f"{failing_symbol}-delayed-effect"
        provider = self.providers / "plane.sh"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                ( sleep 0.7; printf 'escaped\\n' > "$HERMES_PROVIDER_CONFIG_CONTAINMENT_EFFECT" ) &
                sleep 10
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        with RETRO._repository(self.root) as repository:
            with RETRO._retro_store(repository, create=False) as store:
                with RETRO._trusted_artifact_snapshot(
                    store,
                    self.fingerprint,
                ) as snapshot:
                    stored = snapshot.document
                    payload = RETRO._provider_supervisor_payload(
                        stored,
                        {"type": "plane", "containment_effect": str(effect)},
                        repository,
                        store,
                        snapshot,
                    )
        payload["provider_timeout_seconds"] = 0.2
        script_fd = os.open(provider, os.O_RDONLY)
        started = time.monotonic()
        try:
            with (
                mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}, clear=False),
                mock.patch.object(
                    RETRO,
                    failing_symbol,
                    side_effect=RETRO.RetroError("provider_containment_unavailable"),
                ),
            ):
                result = RETRO._run_contained_provider(payload, script_fd)
        finally:
            os.close(script_fd)
        self.assertEqual(result["status"], "failed")
        self.assertLess(time.monotonic() - started, 2.5)
        time.sleep(0.9)
        self.assertFalse(effect.exists())

    def test_actual_containment_info_failure_never_releases_provider(self):
        self._run_actual_containment_failure_probe("_read_containment_info")

    def test_actual_pidfd_open_failure_never_releases_provider(self):
        self._run_actual_containment_failure_probe("_open_pidfd")

    def test_actual_pidfd_signal_failure_reaps_before_delayed_effect(self):
        self._run_actual_containment_failure_probe("_signal_pidfd")

    def test_pidfd_signal_failure_falls_through_to_bounded_group_reap(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 424242
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 0.01), 0]
        with (
            mock.patch.object(
                RETRO,
                "_signal_pidfd",
                side_effect=RETRO.RetroError("provider_containment_unavailable"),
            ),
            mock.patch.object(RETRO.os, "killpg") as killpg,
        ):
            RETRO._terminate_contained_provider(process, 99)
        killpg.assert_called_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)

    def test_controller_budget_includes_info_and_both_shutdown_windows(self):
        with mock.patch.object(RETRO, "_lock_timeout_seconds", return_value=0.5):
            budget = RETRO._controller_timeout_seconds()
        self.assertGreaterEqual(
            budget,
            0.5
            + RETRO.PROVIDER_TIMEOUT_SECONDS
            + (4 * RETRO.SUPERVISOR_SHUTDOWN_SECONDS),
        )

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
        body = RETRO.comment_body(before)
        marker = RETRO.comment_marker(before)
        self.call_delivery(
            {
                "FAKE_LOST_RESPONSE_ONCE": "1",
                "FAKE_LOST_MARK": str(self.root / "lost"),
            }
        )
        failed = RETRO.read_artifact(path)
        self.assertEqual(RETRO.comment_body(failed), body)
        self.assertEqual(RETRO.comment_marker(failed), marker)
        self.assertEqual(failed["operator_action_required"], False)
        self.call_delivery()
        retried = RETRO.read_artifact(path)
        self.assertEqual(RETRO.comment_body(retried), body)
        self.assertEqual(RETRO.comment_marker(retried), marker)

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

    def test_tampered_prepared_source_is_rejected_before_provider_call(self):
        path = self.repo.artifact(self.fingerprint)
        document = RETRO.read_artifact(path)
        document["source_issue"] = OTHER_ISSUE_ID
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(RETRO.RetroError, "immutable_intent_mismatch"):
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

    def test_comment_lock_has_no_filesystem_target_for_symlink_redirect(self):
        victim = self.root / "comment-lock-victim"
        victim.write_text("DO NOT TRUNCATE\n", encoding="utf-8")
        hostile = self.root / "hermes-run-retro-comment-lock"
        hostile.symlink_to(victim)
        result = self.call_delivery()
        self.assertEqual(result["status"], "posted")
        self.assertEqual(victim.read_text(encoding="utf-8"), "DO NOT TRUNCATE\n")

    def test_host_global_lock_is_cross_user_and_has_no_predictable_namespace(self):
        self.assertEqual(
            RETRO.GLOBAL_COMMENT_LOCK_NAMESPACE,
            b"\0hermes.run-retro.comment-lock.v1.",
        )
        self.assertFalse(hasattr(RETRO, "GLOBAL_COMMENT_LOCK_ROOT"))
        marker = self.prepared["comment_fingerprint_marker"]
        fingerprint = marker.removeprefix("[run-retro-comment:").removesuffix("]")

        with (
            mock.patch.object(RETRO.os, "getuid", return_value=1000),
            RETRO._global_comment_lock(marker),
        ):
            contender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import errno,socket,sys;"
                        "lock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
                        "\ntry:"
                        " lock.bind('\\0hermes.run-retro.comment-lock.v1.'+sys.argv[1])"
                        "\nexcept OSError as error:"
                        "\n sys.exit(23 if error.errno==errno.EADDRINUSE else 24)"
                        "\nsys.exit(0)"
                    ),
                    fingerprint,
                ],
                check=False,
            )
        self.assertEqual(contender.returncode, 23)

        released = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import socket,sys;"
                    "lock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
                    "lock.bind('\\0hermes.run-retro.comment-lock.v1.'+sys.argv[1])"
                ),
                fingerprint,
            ],
            check=False,
        )
        self.assertEqual(released.returncode, 0)

    def test_comment_lock_acquisition_is_bounded(self):
        marker = self.prepared["comment_fingerprint_marker"]
        with (
            RETRO._global_comment_lock(marker),
            mock.patch.dict(
                os.environ,
                {"HERMES_LOCK_TIMEOUT_SECONDS": "0.2"},
                clear=False,
            ),
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RETRO.RetroError, "lock_timeout"):
                with RETRO._global_comment_lock(marker):
                    self.fail("duplicate lock unexpectedly acquired")
            self.assertLess(time.monotonic() - started, 1.0)

    def test_foreign_legacy_namespace_precreation_cannot_dos_comment_lock(self):
        hostile_root = self.root / "hermes-run-retro-comment-locks-1000"
        hostile_root.mkdir(mode=0o700)
        victim = self.root / "foreign-victim"
        victim.write_text("foreign\n", encoding="utf-8")
        (hostile_root / "redirect.lock").symlink_to(victim)
        before = sorted(path.name for path in hostile_root.iterdir())

        with (
            mock.patch.object(RETRO.os, "getuid", return_value=1001),
            RETRO._global_comment_lock(self.prepared["comment_fingerprint_marker"]),
        ):
            pass

        self.assertEqual(
            sorted(path.name for path in hostile_root.iterdir()),
            before,
        )
        self.assertEqual(victim.read_text(encoding="utf-8"), "foreign\n")

    def test_delivery_finalizes_through_the_held_store_without_path_reopen(self):
        with mock.patch.object(
            RETRO,
            "finalize",
            side_effect=AssertionError("deliver reopened the repository"),
        ):
            result = self.call_delivery()
        self.assertEqual(result["status"], "posted")
        stored = RETRO.read_artifact(self.repo.artifact(self.fingerprint))
        self.assertEqual(stored["routing"]["status"], "posted")

    def test_repository_path_replacement_cannot_split_provider_and_finalization(self):
        held_root = self.root.with_name(f"{self.root.name}-held")
        original_invoke = RETRO._invoke_provider
        swapped = False

        def replace_root_then_invoke(*args, **kwargs):
            nonlocal swapped
            self.root.rename(held_root)
            shutil.copytree(held_root, self.root, symlinks=True)
            swapped = True
            return original_invoke(*args, **kwargs)

        try:
            with (
                mock.patch.object(
                    RETRO,
                    "_invoke_provider",
                    side_effect=replace_root_then_invoke,
                ),
                self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
            ):
                self.call_delivery()
            held_artifact = (
                held_root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
                / f"{self.fingerprint}.json"
            )
            replacement_artifact = self.repo.artifact(self.fingerprint)
            self.assertEqual(
                RETRO.read_artifact(held_artifact)["routing"]["status"],
                "prepared",
            )
            self.assertEqual(
                RETRO.read_artifact(replacement_artifact)["routing"]["status"],
                "prepared",
            )
            self.assertEqual(self.calls.read_text(encoding="utf-8"), "")
        finally:
            if swapped:
                shutil.rmtree(self.root)
                held_root.rename(self.root)

    def test_provider_and_configuration_are_bound_before_root_replacement(self):
        held_root = self.root.with_name(f"{self.root.name}-provider-held")
        replacement_mark = self.root / "replacement-provider-ran"
        original_provider_context = RETRO._provider_script_fd
        swapped = False

        @contextlib.contextmanager
        def swap_before_provider_open(*args, **kwargs):
            nonlocal swapped
            self.root.rename(held_root)
            shutil.copytree(held_root, self.root, symlinks=True)
            replacement = self.root / "providers" / "plane.sh"
            replacement.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env sh
                    printf 'replacement\\n' > "$HERMES_PROVIDER_CONFIG_REPLACEMENT_MARK"
                    printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$2"
                    """
                ),
                encoding="utf-8",
            )
            replacement.chmod(0o755)
            swapped = True
            with original_provider_context(*args, **kwargs) as descriptor:
                yield descriptor

        try:
            with (
                mock.patch.object(
                    RETRO, "_provider_script_fd", swap_before_provider_open
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "REPLACEMENT_MARK": str(replacement_mark),
                        "FAKE_COMMENT_STORE": str(self.store),
                        "FAKE_CALL_STORE": str(self.calls),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(RETRO.RetroError, "unsafe_artifact_path"),
            ):
                RETRO.deliver(
                    self.root,
                    self.fingerprint,
                    providers_dir=self.providers,
                )
            self.assertFalse(replacement_mark.exists())
            self.assertEqual(self.calls.read_text(encoding="utf-8"), "")
        finally:
            if swapped:
                shutil.rmtree(self.root)
                held_root.rename(self.root)

    def test_root_copy_cannot_fork_the_comment_lock_domain(self):
        second = RETRO.prepare(
            self.root,
            base_intent("00000000-0000-4000-8000-000000000002"),
        )
        started = self.root / "provider-started"
        provider = self.providers / "plane.sh"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                issue="$2"; marker="$3"; body="$4"
                printf 'started\\n' >> "$HERMES_PROVIDER_CONFIG_PROVIDER_STARTED"
                if grep -Fq "$marker" "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"; then
                  printf '{"provider":"plane","status":"already_present","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                  exit 0
                fi
                sleep 0.6
                printf '%s\\n' "$body" >> "$HERMES_PROVIDER_CONFIG_FAKE_COMMENT_STORE"
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        self._configure_provider(PROVIDER_STARTED=str(started))
        held_root = self.root.with_name(f"{self.root.name}-copy-held")
        self.addCleanup(shutil.rmtree, held_root, True)

        def deliver(fingerprint):
            try:
                return RETRO.deliver(
                    self.root, fingerprint, providers_dir=self.providers
                )["status"]
            except RETRO.RetroError as error:
                return error.category

        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(deliver, self.fingerprint)
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists())
            self.root.rename(held_root)
            shutil.copytree(held_root, self.root, symlinks=True)
            second_result = executor.submit(
                deliver, second["artifact_fingerprint"]
            ).result(timeout=5)
            first_result = first.result(timeout=5)

        marker = self.prepared["comment_fingerprint_marker"]
        self.assertEqual(self.store.read_text(encoding="utf-8").count(marker), 1)
        self.assertIn("unsafe_artifact_path", [first_result, second_result])
        self.assertTrue({"posted", "already_present"} & {first_result, second_result})

    def test_provider_execution_uses_bound_config_through_containment_supervisor(self):
        provider = self.providers / "plane.sh"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                [ "${HERMES_BOUND_PROVIDER_CONFIG:-0}" = 1 ] || exit 81
                printf '%s' "$HERMES_BOUND_TICKET_PROVIDER_JSON" |
                  python3 -c 'import json,sys; assert json.load(sys.stdin)["type"].casefold()=="plane"'
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$2"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        result = self.call_delivery()
        self.assertEqual(result["status"], "posted")
        self.assertNotIn("/proc/self/fd", HELPER_PATH.read_text(encoding="utf-8"))
        self.assertIn("--as-pid-1", HELPER_PATH.read_text(encoding="utf-8"))

    def test_missing_containment_primitive_fails_before_provider_start(self):
        with (
            mock.patch.object(
                RETRO,
                "_containment_executable",
                side_effect=RETRO.RetroError("provider_containment_unavailable"),
            ),
            mock.patch.object(
                RETRO.subprocess, "Popen", wraps=RETRO.subprocess.Popen
            ) as popen,
        ):
            result = self.call_delivery()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["transition"], "updated")
        popen.assert_not_called()
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_non_linux_platform_explicitly_fails_before_provider_start(self):
        with (
            mock.patch.object(RETRO.sys, "platform", "darwin"),
            mock.patch.object(
                RETRO.subprocess,
                "Popen",
                wraps=RETRO.subprocess.Popen,
            ) as popen,
        ):
            result = self.call_delivery()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["transition"], "updated")
        popen.assert_not_called()
        self.assertEqual(self.calls.read_text(encoding="utf-8"), "")

    def test_timeout_terminates_and_reaps_the_entire_provider_process_group(self):
        provider = self.providers / "plane.sh"
        descendant_pid = self.root / "descendant.pid"
        delayed_side_effect = self.root / "delayed-side-effect"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                (
                  trap 'exit 0' TERM INT
                  sleep 0.8
                  printf 'escaped\\n' >> "$HERMES_PROVIDER_CONFIG_DELAYED_SIDE_EFFECT"
                ) >/dev/null 2>&1 &
                printf '%s\\n' "$!" > "$HERMES_PROVIDER_CONFIG_DESCENDANT_PID"
                sleep 10
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        self._configure_provider(
            DESCENDANT_PID=str(descendant_pid),
            DELAYED_SIDE_EFFECT=str(delayed_side_effect),
        )
        original_run = RETRO.subprocess.run

        def short_run(*args, **kwargs):
            kwargs["timeout"] = 0.2
            return original_run(*args, **kwargs)

        with (
            mock.patch.object(RETRO, "PROVIDER_TIMEOUT_SECONDS", 0.2, create=True),
            mock.patch.object(RETRO.subprocess, "run", side_effect=short_run),
        ):
            started = time.monotonic()
            result = RETRO.deliver(
                self.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["transition"], "updated")
        self.assertLess(elapsed, 1.0)
        time.sleep(1.0)
        self.assertFalse(delayed_side_effect.exists())
        self.assertGreater(int(descendant_pid.read_text(encoding="utf-8")), 1)

    def test_timeout_terminates_setsid_descendants_before_lock_release(self):
        provider = self.providers / "plane.sh"
        descendant_pid = self.root / "setsid-descendant.pid"
        delayed_side_effect = self.root / "setsid-delayed-side-effect"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                python3 - "$HERMES_PROVIDER_CONFIG_DESCENDANT_PID" "$HERMES_PROVIDER_CONFIG_DELAYED_SIDE_EFFECT" <<'PY' &
                import os, pathlib, signal, sys, time
                os.setsid()
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
                time.sleep(0.8)
                pathlib.Path(sys.argv[2]).write_text("escaped")
                PY
                sleep 10
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        with mock.patch.object(RETRO, "PROVIDER_TIMEOUT_SECONDS", 0.2):
            result = self.call_delivery(
                {
                    "DESCENDANT_PID": str(descendant_pid),
                    "DELAYED_SIDE_EFFECT": str(delayed_side_effect),
                }
            )
        self.assertEqual(result["status"], "failed")
        time.sleep(1.0)
        self.assertFalse(delayed_side_effect.exists())
        self.assertGreater(int(descendant_pid.read_text(encoding="utf-8")), 1)

    def test_success_terminates_setsid_descendants_before_lock_release(self):
        provider = self.providers / "plane.sh"
        descendant_pid = self.root / "success-setsid-descendant.pid"
        delayed_side_effect = self.root / "success-setsid-delayed-side-effect"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                python3 - "$HERMES_PROVIDER_CONFIG_DESCENDANT_PID" "$HERMES_PROVIDER_CONFIG_DELAYED_SIDE_EFFECT" <<'PY' >/dev/null 2>&1 &
                import os, pathlib, sys, time
                os.setsid()
                pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
                time.sleep(0.8)
                pathlib.Path(sys.argv[2]).write_text("escaped")
                time.sleep(10)
                PY
                printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$2"
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        result = self.call_delivery(
            {
                "DESCENDANT_PID": str(descendant_pid),
                "DELAYED_SIDE_EFFECT": str(delayed_side_effect),
            }
        )
        self.assertEqual(result["status"], "posted")
        time.sleep(1.0)
        self.assertFalse(delayed_side_effect.exists())
        if descendant_pid.exists():
            self.assertGreater(int(descendant_pid.read_text(encoding="utf-8")), 1)

    def test_timeout_terminates_reparented_double_fork_before_lock_release(self):
        provider = self.providers / "plane.sh"
        descendant_pid = self.root / "double-fork-descendant.pid"
        delayed_side_effect = self.root / "double-fork-delayed-side-effect"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                setsid python3 - "$HERMES_PROVIDER_CONFIG_DESCENDANT_PID" "$HERMES_PROVIDER_CONFIG_DELAYED_SIDE_EFFECT" <<'PY' >/dev/null 2>&1 &
                import os, pathlib, signal, sys, time
                child = os.fork()
                if child:
                    os._exit(0)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
                pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
                time.sleep(0.8)
                pathlib.Path(sys.argv[2]).write_text("escaped")
                time.sleep(10)
                PY
                sleep 10
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        with mock.patch.object(RETRO, "PROVIDER_TIMEOUT_SECONDS", 0.2):
            result = self.call_delivery(
                {
                    "DESCENDANT_PID": str(descendant_pid),
                    "DELAYED_SIDE_EFFECT": str(delayed_side_effect),
                }
            )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(descendant_pid.exists())
        self.assertGreater(int(descendant_pid.read_text(encoding="utf-8")), 1)
        time.sleep(1.0)
        self.assertFalse(delayed_side_effect.exists())

    def test_provider_output_is_bounded_and_terminates_the_process_group(self):
        provider = self.providers / "plane.sh"
        provider.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                set -eu
                while :; do
                  printf '0123456789abcdef0123456789abcdef'
                  printf '0123456789abcdef0123456789abcdef' >&2
                done
                """
            ),
            encoding="utf-8",
        )
        provider.chmod(0o755)
        original_run = RETRO.subprocess.run

        def short_run(*args, **kwargs):
            kwargs["timeout"] = 0.5
            return original_run(*args, **kwargs)

        with (
            mock.patch.object(RETRO, "PROVIDER_TIMEOUT_SECONDS", 2.0, create=True),
            mock.patch.object(RETRO.subprocess, "run", side_effect=short_run),
        ):
            started = time.monotonic()
            result = RETRO.deliver(
                self.root,
                self.fingerprint,
                providers_dir=self.providers,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertLess(elapsed, 0.5)

    def test_provider_script_is_bound_by_descriptor_before_execution(self):
        with mock.patch.object(
            RETRO.subprocess,
            "Popen",
            wraps=RETRO.subprocess.Popen,
        ) as popen:
            result = self.call_delivery()
        self.assertEqual(result["status"], "posted")
        provider_calls = [
            call
            for call in popen.call_args_list
            if call.args and "_supervise-provider" in call.args[0]
        ]
        self.assertEqual(len(provider_calls), 1)
        command = provider_calls[0].args[0]
        self.assertEqual(command[:3], [sys.executable, "-", "_supervise-provider"])
        self.assertIsInstance(provider_calls[0].kwargs["stdin"], int)
        self.assertEqual(len(provider_calls[0].kwargs["pass_fds"]), 8)
        for descriptor_flag in (
            "--repo-fd",
            "--project-fd",
            "--retro-fd",
            "--bindings-fd",
            "--artifact-fd",
            "--binding-fd",
        ):
            self.assertIn(descriptor_flag, command)


class PlanePaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
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

    def run_plane_resolve(self, reference=ISSUE_ID, *, extra_env=None, timeout=5):
        return subprocess.run(
            [
                "sh",
                str(self.role / ".scripts" / "providers" / "plane.sh"),
                "resolve_issue_id",
                reference,
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
                "TMPDIR": str(self.root),
                **(extra_env or {}),
            },
            timeout=timeout,
        )

    def test_plane_resolve_preserves_nul_bytes_before_strict_validation(self):
        self.write_curl(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                printf '%s' '{{"id":"{ISSUE_ID}"}}'
                printf '\\000'
                """
            )
        )
        result = self.run_plane_resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_plane_resolve_malformed_direct_success_never_falls_back(self):
        self.write_curl(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"work-items/{ISSUE_ID}/"*)
                    printf '%s' '{{"id":"{ISSUE_ID}"}}'
                    printf '\\000'
                    ;;
                  *)
                    printf '{{"results":[{{"id":"{ISSUE_ID}","sequence_id":21,"identifier":"PJAN-21","project_identifier":"PJAN"}}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}}\\n'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane_resolve()
        self.assertNotEqual(result.returncode, 0)
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("work-items/"), 1)
        self.assertNotIn("per_page=100", log)

    def test_plane_terminal_work_item_page_ignores_live_nonempty_cursor(self):
        self.write_curl(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"work-items/PJAN-999/"*) exit 22 ;;
                esac
                printf '{{"results":[{{"id":"{ISSUE_ID}","sequence_id":21,"identifier":"PJAN-21","project_identifier":"PJAN"}}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":"100:1:0"}}\\n'
                """
            )
        )
        result = self.run_plane_resolve("PJAN-999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "issue not found or provider returned a non-canonical UUID",
            result.stderr,
        )
        self.assertNotIn("issue lookup failed", result.stderr)

    def test_plane_terminal_comment_page_ignores_live_nonempty_cursor(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"-X POST"*)
                    printf '{"id":"33333333-3333-4333-8333-333333333333"}\\n'
                    ;;
                  *)
                    printf '{"results":[],"count":0,"total_results":0,"next_page_results":false,"next_cursor":"100:1:0"}\\n'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "posted")
        self.assertIn("-X POST", self.log.read_text(encoding="utf-8"))

    def test_plane_stream_overflow_reaps_group_after_curl_leader_exit(self):
        delayed = self.root / "plane-curl-delayed-effect"
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import pathlib
                import sys
                import time

                child = os.fork()
                if child:
                    os._exit(0)
                try:
                    for _ in range(40):
                        os.write(1, b"x" * 4096)
                except BrokenPipeError:
                    pass
                time.sleep(0.4)
                pathlib.Path(os.environ["DELAYED_EFFECT"]).write_text("escaped")
                os._exit(0)
                """
            )
        )
        result = self.run_plane_resolve(
            extra_env={"DELAYED_EFFECT": str(delayed)},
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        time.sleep(0.7)
        self.assertFalse(delayed.exists())

    def test_plane_issue_resolution_rejects_a_b_a_cursor_cycles(self):
        counter = self.root / "cursor-count"
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"work-items/PJAN-21/"*) exit 22 ;;
                esac
                count=0
                [ ! -f "$CURSOR_COUNT" ] || count="$(cat "$CURSOR_COUNT")"
                count=$((count + 1))
                printf '%s\\n' "$count" > "$CURSOR_COUNT"
                case "$count" in
                  1) next=A ;;
                  2) next=B ;;
                  *) next=A ;;
                esac
                printf '{"results":[],"count":0,"total_results":1,"next_page_results":true,"next_cursor":"%s"}\\n' "$next"
                """
            )
        )
        started = time.monotonic()
        result = self.run_plane_resolve(
            "PJAN-21",
            extra_env={"CURSOR_COUNT": str(counter)},
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertLessEqual(int(counter.read_text(encoding="utf-8")), 3)

    def test_plane_issue_resolution_has_one_operation_wide_deadline(self):
        counter = self.root / "deadline-count"
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"work-items/PJAN-21/"*) exit 22 ;;
                esac
                count=0
                [ ! -f "$CURSOR_COUNT" ] || count="$(cat "$CURSOR_COUNT")"
                count=$((count + 1))
                printf '%s\\n' "$count" > "$CURSOR_COUNT"
                sleep 0.18
                printf '{"results":[],"count":0,"total_results":100,"next_page_results":true,"next_cursor":"cursor-%s"}\\n' "$count"
                """
            )
        )
        started = time.monotonic()
        result = self.run_plane_resolve(
            "PJAN-21",
            extra_env={
                "CURSOR_COUNT": str(counter),
                "HERMES_RESOLVE_TIMEOUT_SECONDS": "0.25",
            },
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLessEqual(int(counter.read_text(encoding="utf-8")), 2)

    def test_plane_comment_lookup_exhausts_current_cursor_pages(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *cursor=next-1*)
                    printf '{"results":[{"id":"33333333-3333-4333-8333-333333333333","comment_html":"safe %s"}],"count":1,"total_results":2,"next_page_results":false,"next_cursor":null}\\n' "$RETRO_MARKER"
                    ;;
                  *)
                    printf '{"results":[{"id":"22222222-2222-4222-8222-222222222222","comment_html":"safe"}],"count":1,"total_results":2,"next_page_results":true,"next_cursor":"next-1"}\\n'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "already_present")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("per_page=100", log)
        self.assertIn("cursor=next-1", log)
        self.assertNotIn("offset=", log)
        self.assertIn(f"/work-items/{ISSUE_ID}/comments/", log)
        self.assertNotIn("/issues/", log)
        self.assertNotIn("-X POST", log)

    def test_plane_collection_snapshot_drift_fails_closed_without_post(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *cursor=next-1*)
                    printf '{"results":[{"id":"33333333-3333-4333-8333-333333333333","comment_html":"safe"}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}\\n'
                    ;;
                  *)
                    printf '{"results":[{"id":"22222222-2222-4222-8222-222222222222","comment_html":"safe"}],"count":1,"total_results":2,"next_page_results":true,"next_cursor":"next-1"}\\n'
                    ;;
                esac
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "lookup_failed")
        self.assertNotIn("-X POST", self.log.read_text(encoding="utf-8"))

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
            '{"results":"not-a-list","count":0,"total_results":0,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":"0","next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":true,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":-1,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":true,"total_results":0,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":1,"total_results":0,"next_page_results":false,"next_cursor":null}',
            '{"results":[null],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}',
            '{"results":[{}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}',
            '{"results":[{"id":42,"comment_html":"safe"}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}',
            '{"results":[{"id":"22222222-2222-4222-8222-222222222222","comment_html":null}],"count":1,"total_results":1,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":1,"next_page_results":false,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":1,"next_page_results":true,"next_cursor":null}',
            '{"results":[],"count":0,"total_results":1,"next_page_results":true,"next_cursor":"same"}',
            '{"results":[],"count":0,"total_results":2001,"next_page_results":false,"next_cursor":null}',
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

    def test_plane_preserves_nul_bytes_and_fails_lookup_closed_before_post(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"-X POST"*)
                    printf '{"id":"33333333-3333-4333-8333-333333333333"}\\n'
                    ;;
                  *)
                    printf '{"results":[],"count":0,"total_results":0,"next_page_results":false,"next_cursor":null}'
                    printf '\\000'
                    ;;
                esac
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
                  *) printf '{"results":[],"count":0,"total_results":0,"next_page_results":false,"next_cursor":null}\\n' ;;
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

    def test_plane_post_requires_a_canonical_string_uuid(self):
        malformed_responses = [
            '{"id":null}',
            '{"id":42}',
            '{"id":"not-a-uuid"}',
            '{"id":"33333333-3333-4333-0333-333333333333"}',
            "null",
        ]
        for index, response in enumerate(malformed_responses):
            with self.subTest(index=index, response=response):
                self.log.write_text("", encoding="utf-8")
                self.write_curl(
                    textwrap.dedent(
                        f"""\
                        #!/usr/bin/env sh
                        printf '%s\\n' "$*" >> "$CURL_LOG"
                        case "$*" in
                          *"-X POST"*) printf '%s\\n' '{response}' ;;
                          *) printf '{{"results":[],"count":0,"total_results":0,"next_page_results":false,"next_cursor":null}}\\n' ;;
                        esac
                        """
                    )
                )
                result = self.run_plane()
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(payload["error_category"], "response_unknown")

    def test_plane_http_reads_are_bounded(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                printf '{"results":[],"count":0,"total_results":0,"next_page_results":false,"next_cursor":null}\\n'
                """
            )
        )
        result = self.run_plane()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--max-filesize", log)
        self.assertIn("--max-time", log)

    def test_plane_response_bound_does_not_depend_on_curl_max_filesize(self):
        self.write_curl(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                import time

                with open(os.environ["CURL_LOG"], "a", encoding="utf-8") as log:
                    log.write(" ".join(sys.argv[1:]) + "\\n")
                sys.stdout.buffer.write(b"x" * 262144)
                sys.stdout.buffer.flush()
                time.sleep(10)
                """
            )
        )
        started = time.monotonic()
        result = self.run_plane_resolve(timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.5)


class TrelloDeliveryContractTests(unittest.TestCase):
    CARD_ID = "a" * 24

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = pathlib.Path(self.temp.name)
        self.role = self.root / "role"
        provider_dir = self.role / ".scripts" / "providers"
        provider_dir.mkdir(parents=True)
        shutil.copy2(TRELLO_PATH, provider_dir / "trello.sh")
        (self.role / "role.yaml").write_text(
            "ticket_provider:\n  name: trello\n  board: bbbbbbbbbbbbbbbbbbbbbbbb\n",
            encoding="utf-8",
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "curl.log"
        self.marker = "[run-retro-comment:" + ("c" * 64) + "]"

    def tearDown(self):
        self.temp.cleanup()

    def run_trello(self):
        return subprocess.run(
            [
                "sh",
                str(self.role / ".scripts" / "providers" / "trello.sh"),
                "ensure_comment",
                self.CARD_ID,
                self.marker,
                f"safe summary {self.marker}",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "TRELLO_KEY": "test-key",
                "TRELLO_TOKEN": "test-token",
                "CURL_LOG": str(self.log),
            },
        )

    def run_trello_resolve(self, reference=None, *, extra_env=None, timeout=5):
        return subprocess.run(
            [
                "sh",
                str(self.role / ".scripts" / "providers" / "trello.sh"),
                "resolve_issue_id",
                reference or self.CARD_ID,
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "TRELLO_KEY": "test-key",
                "TRELLO_TOKEN": "test-token",
                "CURL_LOG": str(self.log),
                "TMPDIR": str(self.root),
                **(extra_env or {}),
            },
            timeout=timeout,
        )

    def test_trello_resolve_preserves_nul_bytes_before_strict_validation(self):
        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                printf '%s' '{{"id":"{self.CARD_ID}"}}'
                printf '\\000'
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        result = self.run_trello_resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_trello_stream_overflow_reaps_group_after_curl_leader_exit(self):
        delayed = self.root / "trello-curl-delayed-effect"
        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import pathlib
                import time

                child = os.fork()
                if child:
                    os._exit(0)
                try:
                    for _ in range(40):
                        os.write(1, b"x" * 4096)
                except BrokenPipeError:
                    pass
                time.sleep(0.4)
                pathlib.Path(os.environ["DELAYED_EFFECT"]).write_text("escaped")
                os._exit(0)
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        result = self.run_trello_resolve(
            extra_env={"DELAYED_EFFECT": str(delayed)},
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        time.sleep(0.7)
        self.assertFalse(delayed.exists())

    def test_trello_rejects_numeric_post_id_and_bounds_every_http_read(self):
        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                case "$*" in
                  *"-X POST"*) printf '{"id":42}\\n' ;;
                  *) printf '[]\\n' ;;
                esac
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        result = self.run_trello()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "response_unknown")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("--max-filesize", log)
        self.assertIn("--max-time", log)

    def test_trello_rejects_malformed_typed_lookup_rows_without_post(self):
        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env sh
                printf '%s\\n' "$*" >> "$CURL_LOG"
                printf '[{"id":42,"data":{"text":"safe"}}]\\n'
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        result = self.run_trello()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "lookup_failed")
        self.assertNotIn("-X POST", self.log.read_text(encoding="utf-8"))

    def test_trello_response_bound_does_not_depend_on_curl_max_filesize(self):
        curl = self.bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                import time

                with open(os.environ["CURL_LOG"], "a", encoding="utf-8") as log:
                    log.write(" ".join(sys.argv[1:]) + "\\n")
                sys.stdout.buffer.write(b"x" * 262144)
                sys.stdout.buffer.flush()
                time.sleep(10)
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        started = time.monotonic()
        result = self.run_trello_resolve(timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.5)


class LinearDeliveryContractTests(unittest.TestCase):
    class Handler(http.server.BaseHTTPRequestHandler):
        response_mode = "numeric"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = self.rfile.read(length)
            if self.response_mode == "oversized":
                payload = b'{"data":' + (b" " * (131072 + 1)) + b"}"
            elif self.response_mode == "malformed_lookup":
                payload = json.dumps(
                    {
                        "data": {
                            "issue": {
                                "comments": {
                                    "nodes": [{"id": 42, "body": "safe"}],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                ).encode()
            elif b"commentCreate" in request:
                payload = json.dumps(
                    {
                        "data": {
                            "commentCreate": {
                                "comment": {"id": 42},
                                "success": True,
                            }
                        }
                    }
                ).encode()
            else:
                payload = json.dumps(
                    {
                        "data": {
                            "issue": {
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = pathlib.Path(self.temp.name)
        self.role = self.root / "role"
        provider_dir = self.role / ".scripts" / "providers"
        provider_dir.mkdir(parents=True)
        shutil.copy2(LINEAR_PATH, provider_dir / "linear.sh")
        (self.role / "role.yaml").write_text(
            "ticket_provider:\n  name: linear\n  team: DEMO\n",
            encoding="utf-8",
        )
        self.marker = "[run-retro-comment:" + ("d" * 64) + "]"

    def tearDown(self):
        self.temp.cleanup()

    def run_linear(self, response_mode):
        self.Handler.response_mode = response_mode
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return subprocess.run(
                [
                    "sh",
                    str(self.role / ".scripts" / "providers" / "linear.sh"),
                    "ensure_comment",
                    ISSUE_ID,
                    self.marker,
                    f"safe summary {self.marker}",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
                env={
                    **os.environ,
                    "LINEAR_API_KEY": "test-only",
                    "LINEAR_API_URL": (
                        f"http://127.0.0.1:{server.server_port}/graphql"
                    ),
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_linear_rejects_numeric_post_id(self):
        result = self.run_linear("numeric")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "response_unknown")

    def test_linear_http_reads_are_bounded_and_timed(self):
        result = self.run_linear("oversized")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "lookup_failed")

    def test_linear_rejects_malformed_typed_lookup_rows_without_post(self):
        result = self.run_linear("malformed_lookup")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_category"], "lookup_failed")


class CopierBootstrapTrustTests(unittest.TestCase):
    def render(self, root, output=None):
        copier = shutil.which("copier")
        if copier is None:
            self.skipTest("Copier is unavailable")
        output = output or root / "output"
        output.parent.mkdir(parents=True, exist_ok=True)

        def umask_002():
            os.umask(0o002)

        result = subprocess.run(
            [
                copier,
                "copy",
                str(ROOT),
                str(output),
                "--trust",
                "--skip-tasks",
                "--defaults",
                "--data",
                "target_repo=pjangler",
                "--data",
                "role=pm",
                "--data",
                "ticket_provider=plane",
            ],
            text=True,
            capture_output=True,
            check=False,
            preexec_fn=umask_002,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def run_first_task(self, output, marker, trace_path=None):
        task = rendered_copier_task()
        self.assertIsInstance(task, str)
        environment = dict(os.environ)
        environment["HERMES_BOOTSTRAP_TEST_MARKER"] = str(marker)
        command = ["/bin/sh", "-c", task]
        if trace_path is not None:
            strace = shutil.which("strace")
            if strace is None:
                self.skipTest("strace is unavailable")
            command = [
                strace,
                "-f",
                "-qq",
                "-yy",
                "-e",
                "trace=openat,close,fchmod,newfstatat",
                "-o",
                str(trace_path),
                *command,
            ]
        return subprocess.run(
            command,
            cwd=output,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def malicious_bootstrap():
        return textwrap.dedent(
            """\
            #!/usr/bin/env sh
            set -eu
            printf 'invoked\\n' > "$HERMES_BOOTSTRAP_TEST_MARKER"
            """
        )

    def managed_ancestor(self, root, project_name, manifest_bytes=None):
        repository = subprocess.run(
            ["git", "init", "--quiet", str(root)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(repository.returncode, 0, repository.stderr)
        project = root / ".project.json"
        if manifest_bytes is None:
            manifest_bytes = (
                json.dumps({"project_name": project_name}) + "\n"
            ).encode()
        project.write_bytes(manifest_bytes)
        implementation = root / "_bmad-output" / "implementation-artifacts"
        retros = implementation / "run-retros"
        retros.mkdir(parents=True)
        private = retros / "existing-private.json"
        private.write_text("{}\n", encoding="utf-8")
        expected = {
            root: 0o770,
            project: 0o660,
            root / "_bmad-output": 0o770,
            implementation: 0o770,
            retros: 0o770,
            private: 0o660,
        }
        for path, mode in expected.items():
            path.chmod(mode)
        return expected

    def assert_modes(self, expected):
        for path, mode in expected.items():
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_unrelated_managed_ancestor_identity_is_not_normalized(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root, root / "agents" / "hermes" / "pm")
            expected = self.managed_ancestor(root, "unrelated-project")

            result = self.run_first_task(output, root / "unexpected-marker")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_modes(expected)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)

    def test_project_identity_without_canonical_role_path_is_not_normalized(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root, root / "standalone" / "output")
            expected = self.managed_ancestor(root, "pjangler")

            result = self.run_first_task(output, root / "unexpected-marker")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_modes(expected)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)

    def test_matching_managed_ancestor_at_canonical_role_path_is_normalized(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root, root / "agents" / "hermes" / "pm")
            original = self.managed_ancestor(root, "pjangler")

            result = self.run_first_task(output, root / "unexpected-marker")

            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                root: 0o755,
                root / ".project.json": 0o644,
                root / "_bmad-output": 0o755,
                root / "_bmad-output" / "implementation-artifacts": 0o755,
                root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros": 0o700,
                root
                / "_bmad-output"
                / "implementation-artifacts"
                / "run-retros"
                / "existing-private.json": 0o600,
            }
            self.assertEqual(set(original), set(expected))
            self.assert_modes(expected)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)

    def assert_nonstandard_json_constant_declines_ancestor(self, token):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root, root / "agents" / "hermes" / "pm")
            document = (
                f'{{"project_name":"pjangler","non_standard":{token}}}\n'
            ).encode()
            expected = self.managed_ancestor(
                root,
                "pjangler",
                manifest_bytes=document,
            )

            result = self.run_first_task(output, root / "unexpected-marker")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_modes(expected)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)

    def test_nan_manifest_declines_ancestor_normalization(self):
        self.assert_nonstandard_json_constant_declines_ancestor("NaN")

    def test_infinity_manifest_declines_ancestor_normalization(self):
        self.assert_nonstandard_json_constant_declines_ancestor("Infinity")

    def test_negative_infinity_manifest_declines_ancestor_normalization(self):
        self.assert_nonstandard_json_constant_declines_ancestor("-Infinity")

    def test_noncanonical_manifest_matrix_declines_ancestor_normalization(self):
        cases = {
            "ordinary_malformed": b'{"project_name":"pjangler",}\n',
            "oversized": (
                b'{"project_name":"pjangler","padding":"' + (b"x" * 65536) + b'"}\n'
            ),
            "non_utf8": b'{"project_name":"pjangler","value":"\xff"}\n',
            "non_object": b'["pjangler"]\n',
            "non_string_project_name": b'{"project_name":1}\n',
            "nan": b'{"project_name":"pjangler","value":NaN}\n',
            "positive_infinity": (b'{"project_name":"pjangler","value":Infinity}\n'),
            "negative_infinity": (b'{"project_name":"pjangler","value":-Infinity}\n'),
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
                    root = pathlib.Path(tmp)
                    root.chmod(0o755)
                    output = self.render(
                        root,
                        root / "agents" / "hermes" / "pm",
                    )
                    expected = self.managed_ancestor(
                        root,
                        "pjangler",
                        manifest_bytes=document,
                    )

                    result = self.run_first_task(
                        output,
                        root / "unexpected-marker",
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_modes(expected)
                    self.assertEqual(
                        stat.S_IMODE(output.stat().st_mode),
                        0o755,
                    )

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "descriptor syscall ordering receipt is Linux-specific",
    )
    def test_manifest_descriptor_remains_bound_through_repository_chmod(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root, root / "agents" / "hermes" / "pm")
            self.managed_ancestor(root, "pjangler")
            trace_path = root / "bootstrap.strace"

            result = self.run_first_task(
                output,
                root / "unexpected-marker",
                trace_path=trace_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = trace_path.read_text(encoding="utf-8").splitlines()
            project_path = str(root / ".project.json")
            repository_path = str(root)
            manifest_open = next(
                index
                for index, line in enumerate(lines)
                if "openat(" in line
                and '".project.json"' in line
                and f"<{project_path}>" in line
            )
            manifest_close = next(
                index
                for index, line in enumerate(
                    lines[manifest_open + 1 :],
                    manifest_open + 1,
                )
                if "close(" in line and f"<{project_path}>" in line
            )
            repository_chmod = next(
                index
                for index, line in enumerate(
                    lines[manifest_open + 1 :],
                    manifest_open + 1,
                )
                if "fchmod(" in line and f"<{repository_path}>" in line
            )
            manifest_chmod = next(
                index
                for index, line in enumerate(
                    lines[manifest_open + 1 :],
                    manifest_open + 1,
                )
                if "fchmod(" in line and f"<{project_path}>" in line
            )
            self.assertLess(manifest_open, repository_chmod)
            self.assertLess(repository_chmod, manifest_chmod)
            self.assertLess(manifest_chmod, manifest_close)

    def test_bootstrap_file_symlink_is_rejected_without_chmod_or_execution(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root)
            marker = root / "bootstrap-invoked"
            outside = root / "outside-bootstrap.sh"
            outside.write_text(self.malicious_bootstrap(), encoding="utf-8")
            outside.chmod(0o644)
            bootstrap = output / ".scripts" / "02-security-modes.sh"
            bootstrap.unlink()
            bootstrap.symlink_to(outside)

            result = self.run_first_task(output, marker)

            self.assertEqual(
                {
                    "accepted": result.returncode == 0,
                    "outside_mode": stat.S_IMODE(outside.stat().st_mode),
                    "executed": marker.exists(),
                },
                {
                    "accepted": False,
                    "outside_mode": 0o644,
                    "executed": False,
                },
            )

    def test_scripts_parent_symlink_is_rejected_without_external_effect(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root)
            marker = root / "parent-bootstrap-invoked"
            scripts = output / ".scripts"
            scripts.rename(output / ".scripts.original")
            outside = root / "outside-scripts"
            outside.mkdir()
            outside.chmod(0o755)
            bootstrap = outside / "02-security-modes.sh"
            bootstrap.write_text(self.malicious_bootstrap(), encoding="utf-8")
            bootstrap.chmod(0o644)
            scripts.symlink_to(outside, target_is_directory=True)

            result = self.run_first_task(output, marker)

            self.assertEqual(
                {
                    "accepted": result.returncode == 0,
                    "outside_mode": stat.S_IMODE(bootstrap.stat().st_mode),
                    "executed": marker.exists(),
                },
                {
                    "accepted": False,
                    "outside_mode": 0o644,
                    "executed": False,
                },
            )

    def test_bootstrap_content_is_normalized_but_never_executed(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = self.render(root)
            marker = root / "regular-bootstrap-invoked"
            bootstrap = output / ".scripts" / "02-security-modes.sh"
            bootstrap.write_text(self.malicious_bootstrap(), encoding="utf-8")
            bootstrap.chmod(0o664)
            repository = subprocess.run(
                ["git", "init", "--quiet", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(repository.returncode, 0, repository.stderr)
            root.chmod(0o770)

            result = self.run_first_task(output, marker)
            task = rendered_copier_task()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(bootstrap.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o770)
            self.assertFalse(marker.exists())
            self.assertNotIn(
                "chmod 0755 .scripts/02-security-modes.sh",
                task,
            )
            self.assertNotIn("./.scripts/02-security-modes.sh", task)
            self.assertIn('"O_NOFOLLOW"', task)
            self.assertIn("os.fchmod", task)


class CopierSecurityMetadataTests(unittest.TestCase):
    def test_umask_002_render_normalizes_modes_and_delivers_with_rendered_controller(
        self,
    ):
        copier = shutil.which("copier")
        if copier is None:
            self.skipTest("Copier is unavailable")
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = pathlib.Path(tmp)
            root.chmod(0o755)
            output = root / "output"

            def umask_002():
                os.umask(0o002)

            render = subprocess.run(
                [
                    copier,
                    "copy",
                    str(ROOT),
                    str(output),
                    "--trust",
                    "--skip-tasks",
                    "--defaults",
                    "--data",
                    "target_repo=pjangler",
                    "--data",
                    "role=pm",
                    "--data",
                    "ticket_provider=plane",
                ],
                text=True,
                capture_output=True,
                check=False,
                preexec_fn=umask_002,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            project = output / ".project.json"
            project.write_text(
                json.dumps(
                    {
                        "project_name": "pjangler",
                        "ticket_provider": {"type": "plane"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            project.chmod(0o664)
            provider = output / ".scripts" / "providers" / "plane.sh"
            provider.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env sh
                    set -eu
                    issue="$2"
                    printf 'temporary\\n' > "$TMPDIR/provider-temp-probe"
                    printf '{"provider":"plane","status":"posted","target_issue":"%s","error_category":null,"error_summary":null}\\n' "$issue"
                    """
                ),
                encoding="utf-8",
            )
            provider.chmod(0o775)
            security_modes = output / ".scripts" / "02-security-modes.sh"
            self.assertTrue(
                security_modes.is_file(),
                "rendered security-mode provisioning task is required",
            )
            normalized = subprocess.run(
                rendered_copier_task(),
                cwd=output,
                shell=True,
                executable="/bin/sh",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(normalized.returncode, 0, normalized.stderr)
            expected_modes = {
                output: 0o755,
                output / ".scripts": 0o755,
                output / ".scripts" / "sentinel": 0o755,
                output / ".scripts" / "sentinel" / "bin": 0o755,
                output / ".scripts" / "providers": 0o755,
                output / ".scripts" / "sentinel" / "bin" / "run-retro.py": 0o755,
                provider: 0o755,
                project: 0o644,
                security_modes: 0o644,
            }
            for path, expected in expected_modes.items():
                with self.subTest(path=path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)
            rendered_helper = output / ".scripts" / "sentinel" / "bin" / "run-retro.py"
            spec = importlib.util.spec_from_file_location(
                "rendered_run_retro",
                rendered_helper,
            )
            rendered_retro = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = rendered_retro
            spec.loader.exec_module(rendered_retro)
            prepared = rendered_retro.prepare(output, base_intent())
            result = rendered_retro.deliver(
                output,
                prepared["artifact_fingerprint"],
                providers_dir=output / ".scripts" / "providers",
            )
            self.assertEqual(result["status"], "posted")
            document = rendered_retro.read_artifact(
                output / prepared["artifact_path"],
                require_final=True,
            )
            self.assertEqual(document["routing"]["status"], "posted")


class ProtocolParityTests(unittest.TestCase):
    def test_portable_temp_contract_has_no_hard_coded_var_tmp(self):
        for path in (HELPER_PATH, PLANE_PATH, TRELLO_PATH):
            with self.subTest(path=path):
                self.assertNotIn("/var/tmp", path.read_text(encoding="utf-8"))

    def test_prompt_docs_schema_and_helper_share_exact_contract(self):
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        docs = DOC_PATH.read_text(encoding="utf-8")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        required = [
            "hermes.run-retro.artifact",
            "hermes.run-retro.comment",
            "run-retro.v8.schema.json",
            "tp ensure_comment",
            "resolve_issue_id",
            "Unicode NFKC",
            "closed safe-summary vocabulary",
            "no-replace",
            "parent-directory fsync",
            "descriptor-relative",
            "abstract Unix-socket",
            "`--unshare-pid --as-pid-1`",
            "`--info-fd` response",
            "pidfd",
            "different keys",
            "finite deadline",
            "`TMPDIR`",
            "lookup_failed",
            "response_unknown",
            "TICKET_PROVIDER",
            "per_page=100",
            "`cursor`",
            "fixed byte limits",
            ".bindings/",
            "symlinks",
            "monotonic",
            "mathematically integral JSON",
            "$(?![\\s\\S])",
            "provider configuration",
            "root replacement",
            "host-global cross-run lock",
            "shell stdin",
            "`setsid`",
            "Linear and Trello",
            "`--final`; only",
            "`routing.proof.transition_id`",
            "`final_document_sha256`",
            "operation-wide deadline",
            "A-B-A",
            "curl predates",
            "process-group",
            "controller deadline",
            "repository descriptor",
            "issue close gate",
            "untrusted_finalization",
            "HMAC",
            "`bindings_fd`",
            "`retro_fd`",
            "bare filename",
            "same-OS-UID peer processes",
            "stale identities",
            "untrusted repository content",
            "final syscall window",
            "immutable/mount helpers",
            "trusted mutation daemons",
            "explicit environment allowlist",
            "`HERMES_PROVIDER_CONFIG_` namespace",
            "group/world",
            "02-security-modes.sh",
            "trusted inline logic from `copier.yml`",
            "never executes the rendered bootstrap entry",
            "canonical path is exactly",
            "`project_name` byte-equal",
            "normalizes only the output root",
            "strict RFC JSON",
            "same manifest descriptor",
            "remains open through both mutations",
            "mode `& 0022 == 0`",
            "artifact and binding descriptors",
            "retained configuration",
            "retained artifact/binding identities",
            "mounts `/` read-only",
            "prepared repository is writable",
            "valid existing final binding",
            "zero-byte final-name poison",
            "`next_page_results=false`",
            "`100:1:0`",
            "original process group",
            "caller's current directory",
        ]
        for token in required:
            with self.subTest(token=token):
                self.assertIn(token, prompt)
                self.assertIn(token, docs)
        self.assertEqual(schema["properties"]["schema_version"]["const"], 8)
        self.assertEqual(RETRO.SCHEMA_VERSION, 8)
        self.assertEqual(RETRO.COMMENT_FINGERPRINT_VERSION, 6)
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
