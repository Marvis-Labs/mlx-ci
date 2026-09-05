import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mlx_ci.contracts import ContractError, unwrap_runner_manifest
from mlx_ci.repository_plan import prepare_repository_plan, validate_repository_queue


class RepositoryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.jobs = Path(self.temporary_directory.name)

    def test_prepares_generic_queue_and_smallest_memory_label(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        control = self.control(manifest)

        queue, matrix = prepare_repository_plan(control, jobs=self.jobs)

        self.assertEqual(queue["kind"], "repository_queue")
        self.assertEqual(queue["attempt_id"], "attempt:1")
        self.assertEqual(unwrap_runner_manifest(queue["jobs"][0]), manifest)
        self.assertEqual(
            matrix,
            [
                {
                    "id": "models:family",
                    "file": "models-family.json",
                    "memory_label": "memory-16gb",
                }
            ],
        )

    def test_accepts_matching_legacy_memory_label(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        control = self.control(manifest)
        control["device_jobs"][0]["memory_label"] = "memory-16gb"

        _, matrix = prepare_repository_plan(control, jobs=self.jobs)

        self.assertEqual(matrix[0]["memory_label"], "memory-16gb")

    def test_rejects_tampered_persisted_manifest(self):
        manifest = self.manifest()
        persisted = dict(manifest)
        persisted["subject"] = "other"
        self.write("models-family.json", persisted)

        with self.assertRaisesRegex(ContractError, "embedded manifest"):
            prepare_repository_plan(self.control(manifest), jobs=self.jobs)

    def test_rejects_repository_identity_mismatch(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        control = self.control(manifest)
        control["repository"] = "Example/project-two"

        with self.assertRaisesRegex(ContractError, "repository does not match"):
            prepare_repository_plan(control, jobs=self.jobs)

    def test_rejects_path_traversal(self):
        manifest = self.manifest()
        control = self.control(manifest)
        control["device_jobs"][0]["file"] = "../job.json"

        with self.assertRaisesRegex(ContractError, "file is invalid"):
            prepare_repository_plan(control, jobs=self.jobs)

    def test_rejects_device_work_from_blocked_repository_plan(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        control = self.control(manifest)
        control["terminal_state"] = "blocked"

        with self.assertRaisesRegex(ContractError, "blocked repository plan"):
            prepare_repository_plan(control, jobs=self.jobs)

    def test_queue_rejects_duplicate_jobs(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        queue, _ = prepare_repository_plan(self.control(manifest), jobs=self.jobs)
        queue["jobs"].append(queue["jobs"][0])

        with self.assertRaisesRegex(ContractError, "must be unique"):
            validate_repository_queue(queue)

    def test_queue_rejects_job_identity_mismatch(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        queue, _ = prepare_repository_plan(self.control(manifest), jobs=self.jobs)
        queue["head_sha"] = "d" * 40

        with self.assertRaisesRegex(ContractError, "head_sha does not match"):
            validate_repository_queue(queue)

    def test_queue_rejects_work_when_terminal_state_is_blocked(self):
        manifest = self.manifest()
        self.write("models-family.json", manifest)
        queue, _ = prepare_repository_plan(self.control(manifest), jobs=self.jobs)
        queue["terminal_state"] = "blocked"

        with self.assertRaisesRegex(ContractError, "blocked repository queue"):
            validate_repository_queue(queue)

    def write(self, name, value):
        (self.jobs / name).write_text(json.dumps(value))

    @staticmethod
    def control(manifest):
        return {
            "schema_version": 1,
            "attempt_id": "attempt:1",
            "repository": "Example/project-one",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "terminal_state": "planned",
            "device_jobs": [
                {
                    "id": "models:family",
                    "file": "models-family.json",
                    "manifest": manifest,
                }
            ],
        }

    @staticmethod
    def manifest():
        value = {
            "id": "models:family",
            "repository": "Example/project-one",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "component": "models",
            "subject": "family",
            "work_type": "FamilyPath",
            "phases": ["synthetic", "checkpoint"],
            "required_memory_gib": 8,
            "required_disk_gib": 4,
            "repository_payload": {"scenario": "small"},
        }
        value["manifest_digest"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
        )
        return value


if __name__ == "__main__":
    unittest.main()
