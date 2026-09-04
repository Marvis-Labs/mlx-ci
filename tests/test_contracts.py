import base64
import unittest

from mlx_ci.contracts import (
    ContractError,
    canonical_digest,
    seal_manifest,
    seal_result,
    validate_envelope,
    validate_job,
    validate_lease,
    validate_request,
    validate_result,
    validate_runner,
)


class ContractTests(unittest.TestCase):
    def test_request_contract(self):
        request = {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": "request:1",
            "repository": "Marvis-Labs/mlx-vlm",
            "pull_request": 7,
            "comment_id": 99,
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }

        self.assertEqual(validate_request(request), request)

    def test_job_digest_detects_mutation(self):
        sealed = seal_manifest(self.job())
        validate_job(sealed)
        sealed["required_memory_gib"] = 1

        with self.assertRaisesRegex(ContractError, "manifest_digest"):
            validate_job(sealed)

    def test_job_rejects_unknown_fields(self):
        job = self.job()
        job["command"] = "curl attacker"

        with self.assertRaisesRegex(ContractError, "unexpected"):
            seal_manifest(job)

    def test_job_requires_immutable_revisions(self):
        job = self.job()
        job["head_sha"] = "main"

        with self.assertRaisesRegex(ContractError, "full lowercase commit"):
            seal_manifest(job)

    def test_signed_envelope_shape(self):
        envelope = {
            "schema_version": 1,
            "kind": "signed_work_manifest",
            "algorithm": "ed25519",
            "key_id": "control-plane-2026-09",
            "manifest": seal_manifest(self.job()),
            "signature": base64.b64encode(b"s" * 64).decode(),
        }

        self.assertEqual(validate_envelope(envelope), envelope)

    def test_envelope_rejects_wrong_signature_length(self):
        envelope = {
            "schema_version": 1,
            "kind": "signed_work_manifest",
            "algorithm": "ed25519",
            "key_id": "control-plane-2026-09",
            "manifest": seal_manifest(self.job()),
            "signature": base64.b64encode(b"short").decode(),
        }

        with self.assertRaisesRegex(ContractError, "64 bytes"):
            validate_envelope(envelope)

    def test_runner_contract(self):
        runner = {
            "schema_version": 1,
            "kind": "runner_capability",
            "runner_id": "mini-1",
            "labels": ["apple-silicon", "mlx-ci-sandbox-v1"],
            "memory_gib": 16,
            "available_disk_gib": 128,
            "status": "online",
            "heartbeat_at": "2026-09-04T12:00:00Z",
        }

        self.assertEqual(validate_runner(runner), runner)

    def test_lease_rejects_expiry_before_heartbeat(self):
        lease = self.lease()
        lease["expires_at"] = lease["heartbeat_at"]

        with self.assertRaisesRegex(ContractError, "not ordered"):
            validate_lease(lease)

    def test_result_digest_detects_mutation(self):
        result = seal_result(self.result())
        validate_result(result)
        result["outcome"] = "regressed"

        with self.assertRaisesRegex(ContractError, "result_digest"):
            validate_result(result)

    def test_canonical_digest_is_order_independent(self):
        self.assertEqual(
            canonical_digest({"a": 1, "b": 2}),
            canonical_digest({"b": 2, "a": 1}),
        )

    @staticmethod
    def job():
        return {
            "schema_version": 1,
            "kind": "work_manifest",
            "job_id": "model_path:qwen2_vl",
            "attempt_id": "attempt:1",
            "repository": "Marvis-Labs/mlx-vlm",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "component": "model_path",
            "subject": "qwen2_vl",
            "phases": ["synthetic", "hf_checkpoint"],
            "required_memory_gib": 16,
            "required_disk_gib": 8,
            "payload": {"checkpoint": "mlx-community/example"},
        }

    @staticmethod
    def lease():
        return {
            "schema_version": 1,
            "kind": "runner_lease",
            "lease_id": "lease:1",
            "attempt_id": "attempt:1",
            "job_id": "model_path:qwen2_vl",
            "runner_id": "mini-1",
            "generation": "generation:1",
            "acquired_at": "2026-09-04T12:00:00Z",
            "heartbeat_at": "2026-09-04T12:01:00Z",
            "expires_at": "2026-09-04T12:06:00Z",
        }

    @staticmethod
    def result():
        return {
            "schema_version": 1,
            "kind": "work_result",
            "job_id": "model_path:qwen2_vl",
            "attempt_id": "attempt:1",
            "repository": "Marvis-Labs/mlx-vlm",
            "runner_id": "mini-1",
            "lease_id": "lease:1",
            "outcome": "passed",
            "failure_code": None,
            "evidence": {"synthetic": {"outcome": "passed"}},
            "started_at": "2026-09-04T12:00:00Z",
            "finished_at": "2026-09-04T12:05:00Z",
        }


if __name__ == "__main__":
    unittest.main()
