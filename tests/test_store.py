import tempfile
import unittest
from pathlib import Path

from mlx_ci.contracts import seal_manifest
from mlx_ci.store import StateConflict, StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.store.initialize()

    def test_active_attempts_coalesce_by_repository_pr_and_head(self):
        first, coalesced = self.store.create_attempt(
            self.request("request:1"),
            attempt_id="attempt:1",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        second, coalesced_again = self.store.create_attempt(
            self.request("request:2"),
            attempt_id="attempt:2",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )

        self.assertFalse(coalesced)
        self.assertTrue(coalesced_again)
        self.assertEqual(second["attempt_id"], first["attempt_id"])

    def test_completed_revision_can_start_a_new_attempt(self):
        self.create_attempt()
        self.store.set_attempt_state("attempt:1", "running", now="2026-09-04T12:01:00Z")
        self.store.set_attempt_state(
            "attempt:1", "completed", now="2026-09-04T12:02:00Z"
        )

        attempt, coalesced = self.store.create_attempt(
            self.request("request:2"),
            attempt_id="attempt:2",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )

        self.assertFalse(coalesced)
        self.assertEqual(attempt["attempt_id"], "attempt:2")

    def test_request_delivery_is_idempotent(self):
        first = self.create_attempt()
        replay, coalesced = self.store.create_attempt(
            self.request("request:1"),
            attempt_id="ignored-attempt",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )

        self.assertTrue(coalesced)
        self.assertEqual(replay, first)

    def test_coalesced_delivery_replay_stays_bound_after_completion(self):
        first = self.create_attempt()
        coalesced, _ = self.store.create_attempt(
            self.request("request:2"),
            attempt_id="attempt:2",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        self.store.set_attempt_state(
            "attempt:1", "cancelled", now="2026-09-04T12:01:00Z"
        )

        replay, was_coalesced = self.store.create_attempt(
            self.request("request:2"),
            attempt_id="attempt:3",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )

        self.assertEqual(coalesced, first)
        self.assertTrue(was_coalesced)
        self.assertEqual(replay["attempt_id"], "attempt:1")

    def test_request_id_reuse_with_different_content_is_rejected(self):
        self.create_attempt()
        changed = self.request("request:1")
        changed["pull_request"] = 8

        with self.assertRaisesRegex(StateConflict, "request_id"):
            self.store.create_attempt(
                changed,
                attempt_id="attempt:2",
                base_sha="a" * 40,
                head_sha="b" * 40,
                contract_sha="c" * 40,
            )

    def test_request_id_reuse_with_different_resolution_is_rejected(self):
        self.create_attempt()

        with self.assertRaisesRegex(StateConflict, "different head_sha"):
            self.store.create_attempt(
                self.request("request:1"),
                attempt_id="attempt:2",
                base_sha="a" * 40,
                head_sha="d" * 40,
                contract_sha="c" * 40,
            )

    def test_jobs_are_validated_against_attempt_identity(self):
        self.create_attempt()
        manifest = self.job()
        manifest["repository"] = "Marvis-Labs/mlx-audio"

        with self.assertRaisesRegex(StateConflict, "repository"):
            self.store.enqueue_jobs(
                "attempt:1",
                [seal_manifest(manifest)],
                now="2026-09-04T12:01:00Z",
            )

    def test_enqueue_is_idempotent_but_rejects_job_id_reuse(self):
        self.create_attempt()
        manifest = seal_manifest(self.job())
        self.store.enqueue_jobs("attempt:1", [manifest], now="2026-09-04T12:01:00Z")
        jobs = self.store.enqueue_jobs(
            "attempt:1", [manifest], now="2026-09-04T12:02:00Z"
        )
        changed = self.job()
        changed["required_memory_gib"] = 32

        self.assertEqual(len(jobs), 1)
        with self.assertRaisesRegex(StateConflict, "job_id"):
            self.store.enqueue_jobs(
                "attempt:1",
                [seal_manifest(changed)],
                now="2026-09-04T12:03:00Z",
            )

    def test_stale_runner_heartbeat_cannot_overwrite_newer_capacity(self):
        newest = self.runner("2026-09-04T12:02:00Z", memory_gib=64)
        stale = self.runner("2026-09-04T12:01:00Z", memory_gib=16)

        self.store.record_runner(newest)
        persisted = self.store.record_runner(stale)

        self.assertEqual(persisted, newest)

    def test_same_runner_heartbeat_timestamp_cannot_change_capacity(self):
        first = self.runner("2026-09-04T12:02:00Z", memory_gib=64)
        conflicting = self.runner("2026-09-04T12:02:00Z", memory_gib=16)
        self.store.record_runner(first)

        with self.assertRaisesRegex(StateConflict, "heartbeat timestamp"):
            self.store.record_runner(conflicting)

    def test_terminal_attempt_cannot_accept_jobs_or_transition(self):
        self.create_attempt()
        self.store.set_attempt_state(
            "attempt:1", "cancelled", now="2026-09-04T12:01:00Z"
        )

        with self.assertRaises(StateConflict):
            self.store.enqueue_jobs(
                "attempt:1",
                [seal_manifest(self.job())],
                now="2026-09-04T12:02:00Z",
            )
        with self.assertRaises(StateConflict):
            self.store.set_attempt_state(
                "attempt:1", "running", now="2026-09-04T12:02:00Z"
            )

    def create_attempt(self):
        attempt, _ = self.store.create_attempt(
            self.request("request:1"),
            attempt_id="attempt:1",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        return attempt

    @staticmethod
    def request(request_id):
        return {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": request_id,
            "repository": "Marvis-Labs/mlx-vlm",
            "pull_request": 7,
            "comment_id": 99,
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }

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
    def runner(heartbeat_at, *, memory_gib):
        return {
            "schema_version": 1,
            "kind": "runner_capability",
            "runner_id": "mini-1",
            "labels": ["apple-silicon", "mlx-ci-sandbox-v1"],
            "memory_gib": memory_gib,
            "available_disk_gib": 128,
            "status": "online",
            "heartbeat_at": heartbeat_at,
        }


if __name__ == "__main__":
    unittest.main()
