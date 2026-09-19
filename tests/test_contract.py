import json
import tempfile
import unittest
from pathlib import Path

from runners.actions import admission_command, memory_label
from runners.contract import (
    ContractError,
    read_json,
    seal_job,
    validate_job,
    validate_request,
    validate_result,
)


REPOSITORIES = {"vlm": "Marvis-Labs/mlx-vlm", "audio": "Marvis-Labs/mlx-audio"}


class ContractTests(unittest.TestCase):
    def request(self):
        return {
            "schema_version": 1,
            "engine": "vlm",
            "repository": REPOSITORIES["vlm"],
            "pull_request": 42,
            "comment_id": 123,
        }

    def job(self):
        return seal_job(
            {
                "schema_version": 1,
                "engine": "vlm",
                "repository": REPOSITORIES["vlm"],
                "pull_request": 42,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "head_repository": "contributor/mlx-vlm",
                "contract_sha": "a" * 40,
                "id": "model-qwen2_vl",
                "component": "model",
                "subject": "qwen2_vl",
                "required_memory_gib": 16,
                "required_disk_gib": 8,
            },
            REPOSITORIES,
        )

    def test_request_is_repository_bound(self):
        self.assertEqual(
            validate_request(self.request(), REPOSITORIES)["engine"], "vlm"
        )
        bad = {**self.request(), "repository": REPOSITORIES["audio"]}
        with self.assertRaises(ContractError):
            validate_request(bad, REPOSITORIES)

    def test_request_rejects_extra_fields_and_boolean_identifiers(self):
        for bad in (
            {**self.request(), "command": "sh"},
            {**self.request(), "comment_id": True},
        ):
            with self.assertRaises(ContractError):
                validate_request(bad, REPOSITORIES)

    def test_job_is_sealed_to_immutable_revisions(self):
        job = self.job()
        self.assertEqual(validate_job(job, REPOSITORIES), job)
        with self.assertRaises(ContractError):
            validate_job({**job, "head_sha": "c" * 40}, REPOSITORIES)
        with self.assertRaises(ContractError):
            validate_job({**job, "command": "sh"}, REPOSITORIES)
        with self.assertRaises(ContractError):
            seal_job(
                {**job, "contract_sha": "c" * 40, "manifest_digest": "0" * 64},
                REPOSITORIES,
            )

    def test_result_is_bound_to_job(self):
        job = self.job()
        result = {
            "schema_version": 1,
            "job_id": job["id"],
            "manifest_digest": job["manifest_digest"],
            "status": "passed",
            "checks": [{"name": "synthetic", "status": "passed"}],
        }
        self.assertEqual(validate_result(result, job), result)
        with self.assertRaises(ContractError):
            validate_result({**result, "manifest_digest": "0" * 64}, job)
        with self.assertRaises(ContractError):
            validate_result({**result, "command": "sh"}, job)
        with self.assertRaises(ContractError):
            validate_result(
                {**result, "checks": [{"name": "synthetic", "status": "failed"}]}, job
            )

    def test_reader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text('{"engine":"vlm","engine":"audio"}')
            with self.assertRaises(ContractError):
                read_json(path)

    def test_smallest_runner_tier_and_fixed_broker(self):
        self.assertEqual(memory_label(17), "memory-32gb")
        with tempfile.TemporaryDirectory() as directory:
            job_path = Path(directory) / "job.json"
            result_path = Path(directory) / "result.json"
            job_path.write_text(json.dumps(self.job()))
            self.assertEqual(
                admission_command(job_path, result_path, REPOSITORIES),
                [
                    "/usr/local/libexec/marvis-ci/RUN_JOB.sh",
                    str(job_path),
                    str(result_path),
                ],
            )
