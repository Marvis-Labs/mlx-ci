import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from runners.actions import (
    admission_command,
    choose_runner,
    collect_results,
    matrix,
    memory_label,
    normalize_result,
)
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
from runners.resources import (
    GIB,
    ResourceError,
    huggingface_cache_state,
    runner_decision,
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

    def runner(self, memory_gib=128, free_disk_gib=8, cache_state="complete"):
        job = self.job()
        return {
            "schema_version": 1,
            "id": f"runner-{memory_gib}",
            "physical_memory_bytes": memory_gib * GIB,
            "available_memory_bytes": memory_gib * GIB,
            "free_disk_bytes": free_disk_gib * GIB,
            "busy": False,
            "artifacts": [
                {
                    "kind": job["artifact"]["kind"],
                    "repository": job["artifact"]["repository"],
                    "revision": job["artifact"]["revision"],
                    "state": cache_state,
                }
            ],
        }

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
            {"schema_version": 1, "jobs": [job], "blocked": []}, REPOSITORIES
        )
        entry = value["include"][0]
        self.assertEqual(entry["job_id"], job["id"])
        self.assertEqual(entry["memory_label"], "memory-128gb")

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

    def test_collection_represents_missing_runner_without_inventing_device(self):
        job = self.job()
        attempt = {
            "schema_version": 2,
            "engine": "vlm",
            "repository": REPOSITORIES["vlm"],
            "pull_request": 42,
            "comment_id": 123,
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
            )
        result = bundle["results"][0]
        self.assertEqual(result["status"], "infrastructure_failure")
        self.assertIsNone(result["device"])

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
                admission_command(job_path, result_path, REPOSITORIES, self.runner()),
                [
                    "/usr/local/libexec/marvis-ci/RUN_JOB.sh",
                    str(job_path),
                    str(result_path),
                ],
            )

    def test_runner_gate_uses_capacity_availability_and_cache(self):
        job = self.job()
        self.assertEqual(
            runner_decision(job, self.runner(64))["reason"],
            "insufficient_memory",
        )
        unavailable = self.runner()
        unavailable["available_memory_bytes"] = 64 * GIB
        self.assertEqual(
            runner_decision(job, unavailable)["reason"],
            "insufficient_available_memory",
        )
        uncached = self.runner(free_disk_gib=8, cache_state="absent")
        self.assertEqual(runner_decision(job, uncached)["reason"], "insufficient_disk")
        self.assertTrue(runner_decision(job, self.runner())["eligible"])

    def test_smallest_fit_runner_wins_before_cache_locality(self):
        large = self.runner(256)
        small = self.runner(128, free_disk_gib=256, cache_state="absent")
        self.assertEqual(
            choose_runner(self.job(), [large, small], REPOSITORIES)["id"],
            "runner-128",
        )
        with self.assertRaises(ResourceError):
            choose_runner(self.job(), [self.runner(64)], REPOSITORIES)

    def test_runner_snapshot_is_strictly_validated(self):
        runner = self.runner()
        runner["command"] = "sh"
        with self.assertRaisesRegex(ResourceError, "fields"):
            runner_decision(self.job(), runner)
        runner = self.runner()
        runner["available_memory_bytes"] = runner["physical_memory_bytes"] + 1
        with self.assertRaisesRegex(ResourceError, "available memory"):
            runner_decision(self.job(), runner)

    def test_huggingface_cache_requires_complete_weight_bytes(self):
        artifact = self.job()["artifact"]
        artifact = {**artifact, "tensor_bytes": 8}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = (
                root
                / "models--mlx-community--Qwen3.8-Flash-Next-4bit"
                / "snapshots"
                / artifact["revision"]
            )
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}")
            (snapshot / "model.safetensors").write_bytes(b"1234")
            self.assertEqual(huggingface_cache_state(artifact, root), "partial")
            (snapshot / "model.safetensors").write_bytes(b"12345678")
            self.assertEqual(huggingface_cache_state(artifact, root), "complete")
            (snapshot / "model.safetensors.index.json").write_text(
                json.dumps(
                    {
                        "weight_map": {
                            "first": "model-00001.safetensors",
                            "second": "model-00002.safetensors",
                        }
                    }
                )
            )
            self.assertEqual(huggingface_cache_state(artifact, root), "partial")
            (snapshot / "model-00001.safetensors").write_bytes(b"1234")
            (snapshot / "model-00002.safetensors").write_bytes(b"5678")
            self.assertEqual(huggingface_cache_state(artifact, root), "complete")
