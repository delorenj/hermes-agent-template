import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONSUMER_PATH = ROOT / "runtime-scaffold" / "bloodbank-consumer.py"
GENERATED_CONSUMER_PATH = ROOT / "template" / ".runtime-scaffold" / "bloodbank-consumer.py"


def load_consumer():
    sys.modules.setdefault("nats", types.ModuleType("nats"))
    spec = importlib.util.spec_from_file_location("bloodbank_consumer_contract", CONSUMER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.AGENT_ID = "demo-pm"
    module.REPO = "demo"
    module.PRODUCER = "hermes-agent:demo-pm"
    module.SOURCE = "hermes://agent/demo-pm"
    return module


class BloodbankConsumerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_home = tempfile.TemporaryDirectory()
        cls.previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = cls.temp_home.name
        cls.consumer = load_consumer()

    @classmethod
    def tearDownClass(cls):
        if cls.previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = cls.previous_home
        cls.temp_home.cleanup()

    def test_scaffold_copies_stay_identical(self):
        self.assertEqual(CONSUMER_PATH.read_bytes(), GENERATED_CONSUMER_PATH.read_bytes())

    def test_subscriptions_use_fixed_canonical_routes(self):
        self.assertEqual(
            self.consumer.SUBJECTS,
            ["bloodbank.evt.v1.repo.>", "bloodbank.cmd.v1.agent.>"],
        )
        self.assertNotIn("demo", ".".join(self.consumer.SUBJECTS))
        self.assertNotIn("demo-pm", ".".join(self.consumer.SUBJECTS))

    def test_envelope_keeps_identity_out_of_type_and_subject(self):
        envelope = self.consumer.build_envelope(
            "bloodbank.v1.repo.issue.updated",
            {"repo": "demo", "issue": "PJAN-1"},
        )
        self.assertEqual(envelope["type"], "bloodbank.v1.repo.issue.updated")
        self.assertEqual(envelope["subject"], "bloodbank.evt.v1.repo.issue.updated")
        self.assertEqual(envelope["data"]["repo"], "demo")
        self.assertEqual(envelope["actor"]["agent_id"], "demo-pm")
        self.assertEqual(envelope["source"], "hermes://agent/demo-pm")
        self.assertNotIn("demo", envelope["type"])
        self.assertNotIn("demo", envelope["subject"])
        with self.assertRaisesRegex(ValueError, r"bloodbank\.v1"):
            self.consumer.build_envelope(
                "bloodbank.v2.repo.issue.updated",
                {"repo": "demo"},
            )

    def test_repo_events_route_by_data_repo(self):
        subject = "bloodbank.evt.v1.repo.issue.updated"
        envelope = self.consumer.build_envelope(
            "bloodbank.v1.repo.issue.updated",
            {"repo": "demo"},
        )
        self.assertTrue(self.consumer._is_for_consumer(subject, envelope))
        envelope["data"]["repo"] = "another-repo"
        self.assertFalse(self.consumer._is_for_consumer(subject, envelope))

    def test_agent_commands_route_by_target_agent_id(self):
        subject = "bloodbank.cmd.v1.agent.task.assign"
        envelope = self.consumer.build_envelope(
            "bloodbank.v1.agent.task.assign",
            {"target_agent_id": "demo-pm"},
            kind="command",
        )
        self.assertTrue(self.consumer._is_for_consumer(subject, envelope))
        envelope["data"]["target_agent_id"] = "other-agent"
        self.assertFalse(self.consumer._is_for_consumer(subject, envelope))

    def test_rejects_identifier_bearing_or_mismatched_routes(self):
        envelope = self.consumer.build_envelope(
            "bloodbank.v1.repo.issue.updated",
            {"repo": "demo"},
        )
        self.assertFalse(
            self.consumer._is_for_consumer(
                "bloodbank.evt.v1.repo.demo.issue.updated",
                envelope,
            )
        )
        self.assertFalse(
            self.consumer._is_for_consumer(
                "bloodbank.evt.v1.repo.issue.created",
                envelope,
            )
        )
        envelope["kind"] = "command"
        self.assertFalse(
            self.consumer._is_for_consumer(
                "bloodbank.evt.v1.repo.issue.updated",
                envelope,
            )
        )

    def test_generated_contract_docs_do_not_put_identifiers_in_routes(self):
        coupled_paths = [
            ROOT / "docs" / "architecture.md",
            ROOT / "docs" / "operations.md",
            ROOT / "template" / "SOUL.md.jinja",
            ROOT / "template" / "role.yaml.jinja",
            ROOT / "runtime-scaffold" / "memories" / "MEMORY.md",
            ROOT / "template" / ".runtime-scaffold" / "memories" / "MEMORY.md",
        ]
        forbidden = re.compile(
            r"bloodbank\.(?:v1\.repo|evt\.v1\.repo|cmd\.v1\.agent)\."
            r"(?:\{\{|<repo>|<agent_id>)"
        )
        for path in coupled_paths:
            with self.subTest(path=path):
                self.assertIsNone(forbidden.search(path.read_text()))


if __name__ == "__main__":
    unittest.main()
