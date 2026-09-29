import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from runners.actions import bundle_passed, collect_results, matrix, normalize_result
from runners.contract import (
    ContractError,
    read_json,
    seal_job,
    seal_plan,
    validate_attempt,
    validate_job,
    validate_request,
    validate_result,
)
from runners.resources import GIB
from runners.resources import runner_tier_gib

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
                "schema_version": 2,
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
                "phases": ["synthetic", "checkpoint"],
                "work": {
                    "synthetic": {"selectors": ["test_models.py::qwen2_vl"]},
                    "checkpoint": {"prompt_tokens": 512, "max_tokens": 16},
                },
                "resources": {
                    "resident_bytes": 111_519_423_247,
                    "fixed_bytes": 0,
                    "bytes_per_unit": 100_000,
                    "units": 30_839,
                    "batch_size": 1,
                    "workspace_bytes": 4 * GIB,
                },
                "artifact": {
                    "kind": "huggingface",
                    "repository": "mlx-community/Qwen3.8-Flash-Next-4bit",
                    "revision": "c" * 40,
                    "tensor_bytes": 111_519_423_247,
                },
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

    def test_attempt_and_engine_plan_seal_into_jobs(self):
        attempt = {
            "schema_version": 2,
            "engine": "vlm",
            "repository": REPOSITORIES["vlm"],
            "pull_request": 42,
            "comment_id": 123,
            "requested_at": "2026-09-28T18:24:42Z",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "head_repository": "contributor/mlx-vlm",
            "contract_sha": "a" * 40,
            "run_id": 17,
            "run_attempt": 1,
            "changed_files": ["mlx_vlm/models/qwen2_vl/vision.py"],
        }
        validate_attempt(attempt, REPOSITORIES)
        source = self.job()
        template = {
            key: source[key]
            for key in (
                "id",
                "component",
                "subject",
                "phases",
                "work",
                "resources",
                "artifact",
            )
        }
        sealed = seal_plan(
            {**attempt, "head_repository": source["head_repository"]},
            {"schema_version": 1, "jobs": [template], "blocked": []},
            REPOSITORIES,
        )
        self.assertEqual(sealed["jobs"][0]["head_sha"], "b" * 40)
        self.assertEqual(sealed["jobs"][0]["phases"], source["phases"])

    def test_job_is_sealed_to_immutable_revisions(self):
        job = self.job()
        self.assertEqual(validate_job(job, REPOSITORIES), job)
        with self.assertRaises(ContractError):
            validate_job({**job, "head_sha": "c" * 40}, REPOSITORIES)
        with self.assertRaises(ContractError):
            validate_job({**job, "command": "sh"}, REPOSITORIES)
        with self.assertRaises(ContractError):
            seal_job({**job, "contract_sha": "c" * 40}, REPOSITORIES)

    def test_job_resource_requirements_are_centrally_derived(self):
        job = self.job()
        self.assertEqual(job["required_memory_gib"], 128)
        self.assertGreater(job["required_disk_gib"], 100)
        bad = {**job, "required_memory_gib": 64}
        payload = {key: value for key, value in bad.items() if key != "manifest_digest"}
        bad["manifest_digest"] = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ContractError, "resource requirements"):
            validate_job(bad, REPOSITORIES)

    def test_calibrated_models_map_to_expected_tiers(self):
        plan = {
            key: value
            for key, value in self.job().items()
            if key
            not in {
                "estimated_peak_bytes",
                "required_memory_gib",
                "required_disk_gib",
                "manifest_digest",
            }
        }
        cases = (
            ("qwen3-vl-30b-a3b", 18_252_103_673, 90_000, 30_836, 32),
            ("qwen3-8-27b-bf16", 54_700_000_000, 150_000, 30_839, 128),
            ("qwen3-8-flash-next", 111_519_423_247, 100_000, 30_839, 128),
            ("glm5-next", 181_709_451_790, 950_000, 30_533, 256),
            ("mistral-large-3", 386_726_010_714, 750_000, 30_512, 512),
        )
        for subject, resident, bytes_per_unit, units, expected in cases:
            with self.subTest(subject=subject):
                resources = {
                    "resident_bytes": resident,
                    "fixed_bytes": 0,
                    "bytes_per_unit": bytes_per_unit,
                    "units": units,
                    "batch_size": 1,
                    "workspace_bytes": 4 * GIB,
                }
                job = seal_job(
                    {
                        **plan,
                        "subject": subject,
                        "resources": resources,
                        "artifact": {
                            **plan["artifact"],
                            "tensor_bytes": resident,
                        },
                    },
                    REPOSITORIES,
                )
                self.assertEqual(job["required_memory_gib"], expected)

    def test_result_is_bound_to_job(self):
        job = self.job()
        result = {
            "schema_version": 2,
            "job_id": job["id"],
            "manifest_digest": job["manifest_digest"],
            "status": "passed",
            "device": {"chip": "Apple M4", "memory_gib": 16},
            "cache": "hit",
            "duration_ms": 1250,
            "checks": [
                {
                    "name": "Synthetic",
                    "category": "correctness",
                    "status": "passed",
                    "detail": "Outputs match",
                }
            ],
            "metrics": [
                {
                    "name": "TTFT",
                    "unit": "ms",
                    "base": 100,
                    "head": 104,
                    "change_pct": 4.0,
                    "verdict": "regressed",
                }
            ],
        }
        self.assertEqual(validate_result(result, job), result)
        with self.assertRaises(ContractError):
            validate_result({**result, "manifest_digest": "0" * 64}, job)
        with self.assertRaises(ContractError):
            validate_result({**result, "command": "sh"}, job)
        with self.assertRaises(ContractError):
            validate_result(
                {
                    **result,
                    "checks": [
                        {
                            "name": "Synthetic",
                            "category": "correctness",
                            "status": "failed",
                            "detail": "Outputs differ",
                        }
                    ],
                },
                job,
            )

    def test_public_result_rejects_runner_identity(self):
        job = self.job()
        result = {
            "schema_version": 2,
            "job_id": job["id"],
            "manifest_digest": job["manifest_digest"],
            "status": "passed",
            "device": {"chip": "Apple M4 Max", "memory_gib": 128},
            "cache": "downloaded",
            "duration_ms": 2500,
            "checks": [
                {
                    "name": "Checkpoint",
                    "category": "correctness",
                    "status": "passed",
                    "detail": "Outputs match",
                }
            ],
            "metrics": [],
        }
        self.assertEqual(validate_result(result, job), result)
        for field in ("runner", "runner_id", "runner_name", "hostname"):
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_result({**result, field: "private"}, job)

    def test_dispatch_matrix_uses_smallest_memory_label(self):
        job = self.job()
        value = matrix(
            {"schema_version": 1, "jobs": [job], "blocked": []},
            REPOSITORIES,
            (16, 128),
        )
        entry = value["include"][0]
        self.assertEqual(entry["job_id"], job["id"])
        self.assertEqual(entry["memory_label"], "memory-128gb")
        self.assertEqual(runner_tier_gib(16, (16, 128)), 16)
        self.assertEqual(runner_tier_gib(32, (16, 128)), 128)
        self.assertEqual(runner_tier_gib(64, (16, 128)), 128)
        self.assertIsNone(runner_tier_gib(256, (16, 128)))

    def test_runner_result_is_sanitized_before_collection(self):
        job = self.job()
        raw = {
            "job_id": job["id"],
            "outcome": "passed",
            "reason": None,
            "device": "private-hostname",
            "cache": {"before": "complete", "after": "complete", "reused": True},
            "findings": {"metrics": []},
        }
        result = normalize_result(job, raw, "Apple M4 Max", 128, 2500)
        self.assertEqual(result["device"], {"chip": "Apple M4 Max", "memory_gib": 128})
        self.assertNotIn("private-hostname", json.dumps(result))
        self.assertEqual(result["cache"], "hit")

    def test_runner_findings_are_preserved_without_exposing_internal_errors(self):
        job = self.job()
        raw = {
            "job_id": job["id"],
            "outcome": "test_failure",
            "reason": None,
            "cache": {"before": "not_applicable", "after": "not_applicable"},
            "findings": {
                "error": "CalledProcessError: /Users/private/checkout",
                "metrics": [],
                "verdict": "test_failure",
            },
        }
        result = normalize_result(job, raw, "Apple M4", 16, 25)
        self.assertEqual(
            result["checks"][0]["detail"], "Checkout or test command failed"
        )
        self.assertNotIn("/Users/private", json.dumps(result))

        raw["outcome"] = "passed"
        raw["findings"] = {
            "checks": [
                {
                    "name": "Synthetic structure",
                    "category": "correctness",
                    "status": "passed",
                    "detail": "Main and PR passed",
                }
            ],
            "metrics": [],
            "verdict": "passed",
        }
        result = normalize_result(job, raw, "Apple M4", 16, 25)
        self.assertEqual(result["checks"], raw["findings"]["checks"])

    def test_collection_represents_missing_runner_without_inventing_device(self):
        job = self.job()
        attempt = {
            "schema_version": 2,
            "engine": "vlm",
            "repository": REPOSITORIES["vlm"],
            "pull_request": 42,
            "comment_id": 123,
            "requested_at": "2026-09-28T18:24:42Z",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "head_repository": "contributor/mlx-vlm",
            "contract_sha": "a" * 40,
            "run_id": 17,
            "run_attempt": 1,
            "changed_files": ["mlx_vlm/models/qwen2_vl/vision.py"],
        }
        with tempfile.TemporaryDirectory() as directory:
            bundle = collect_results(
                attempt,
                {"schema_version": 1, "jobs": [job], "blocked": []},
                Path(directory),
                "https://github.com/Marvis-Labs/mlx-ci/actions/runs/17",
                REPOSITORIES,
                {
                    "include": [{"job_id": job["id"]}],
                    "unavailable": [],
                },
            )
        result = bundle["results"][0]
        self.assertEqual(result["status"], "infrastructure_failure")
        self.assertIsNone(result["device"])

    def test_capability_skip_is_distinct_from_runner_failure(self):
        checkpoint = self.job()
        source = {
            key: value
            for key, value in checkpoint.items()
            if key
            not in {
                "estimated_peak_bytes",
                "required_memory_gib",
                "required_disk_gib",
                "manifest_digest",
            }
        }
        synthetic = seal_job(
            {
                **source,
                "id": "model-qwen2_vl-synthetic",
                "phases": ["synthetic"],
                "work": {"synthetic": {"selectors": ["contract"]}},
                "resources": {
                    "resident_bytes": 256 << 20,
                    "fixed_bytes": GIB,
                    "bytes_per_unit": 256 << 10,
                    "units": 512,
                    "batch_size": 1,
                    "workspace_bytes": 4 * GIB,
                },
                "artifact": None,
            },
            REPOSITORIES,
        )
        checkpoint = seal_job(
            {
                **source,
                "id": "model-qwen2_vl-checkpoint",
                "phases": ["checkpoint"],
                "work": {"checkpoint": source["work"]["checkpoint"]},
            },
            REPOSITORIES,
        )
        synthetic_result = {
            "schema_version": 2,
            "job_id": synthetic["id"],
            "manifest_digest": synthetic["manifest_digest"],
            "status": "passed",
            "device": {"chip": "Apple M4", "memory_gib": 16},
            "cache": "not_applicable",
            "duration_ms": 25,
            "checks": [
                {
                    "name": "Synthetic",
                    "category": "correctness",
                    "status": "passed",
                    "detail": "Contracts passed",
                }
            ],
            "metrics": [],
        }
        attempt = {
            "schema_version": 2,
            "engine": "vlm",
            "repository": REPOSITORIES["vlm"],
            "pull_request": 42,
            "comment_id": 123,
            "requested_at": "2026-09-28T18:24:42Z",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "head_repository": "contributor/mlx-vlm",
            "contract_sha": "a" * 40,
            "run_id": 17,
            "run_attempt": 1,
            "changed_files": ["mlx_vlm/models/qwen2_vl/vision.py"],
        }
        with tempfile.TemporaryDirectory() as directory:
            result_directory = Path(directory)
            (result_directory / f"{synthetic['id']}.json").write_text(
                json.dumps(synthetic_result)
            )
            bundle = collect_results(
                attempt,
                {"schema_version": 1, "jobs": [synthetic, checkpoint], "blocked": []},
                result_directory,
                "https://github.com/Marvis-Labs/mlx-ci/actions/runs/17",
                REPOSITORIES,
                {
                    "include": [{"job_id": synthetic["id"]}],
                    "unavailable": [checkpoint["id"]],
                },
            )
        skipped = bundle["results"][1]
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("Needs a capable runner", skipped["checks"][0]["detail"])
        self.assertTrue(bundle_passed(bundle))

    def test_correctness_failure_allows_only_advisory_metrics(self):
        job = self.job()
        result = {
            "schema_version": 2,
            "job_id": job["id"],
            "manifest_digest": job["manifest_digest"],
            "status": "failed",
            "device": {"chip": "Apple M4", "memory_gib": 16},
            "cache": "hit",
            "duration_ms": 1250,
            "checks": [
                {
                    "name": "Output",
                    "category": "correctness",
                    "status": "failed",
                    "detail": "Outputs differ",
                }
            ],
            "metrics": [
                {
                    "name": "TTFT",
                    "unit": "ms",
                    "base": 100,
                    "head": 90,
                    "change_pct": -10,
                    "verdict": "advisory",
                }
            ],
        }
        self.assertEqual(validate_result(result, job), result)
        result["metrics"][0]["verdict"] = "improved"
        with self.assertRaisesRegex(ContractError, "advisory"):
            validate_result(result, job)

    def test_bundle_gate_accepts_only_clean_results(self):
        bundle = {
            "jobs": {"blocked": []},
            "results": [{"status": "passed", "metrics": []}],
        }
        self.assertTrue(bundle_passed(bundle))

        bundle["results"][0]["metrics"] = [{"verdict": "regressed"}]
        self.assertFalse(bundle_passed(bundle))

        bundle["results"][0] = {"status": "failed", "metrics": []}
        self.assertFalse(bundle_passed(bundle))

        bundle["results"][0] = {
            "status": "infrastructure_failure",
            "metrics": [],
        }
        self.assertFalse(bundle_passed(bundle))

    def test_bundle_gate_rejects_blocked_work(self):
        bundle = {
            "jobs": {"blocked": [{"reason": "model_case_missing"}]},
            "results": [],
        }
        self.assertFalse(bundle_passed(bundle))

    def test_reader_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text('{"engine":"vlm","engine":"audio"}')
            with self.assertRaises(ContractError):
                read_json(path)
