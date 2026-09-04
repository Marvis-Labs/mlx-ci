import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mlx_ci.contracts import ContractError, seal_manifest, seal_result
from mlx_ci.control_plane import ControlPlane
from mlx_ci.scheduler import Scheduler
from mlx_ci.service import AuthenticationError, ControlService, RunnerAuthenticator
from mlx_ci.store import StateConflict, StateStore

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
MINI_TOKEN = "mini_" + "a" * 40
STUDIO_TOKEN = "studio_" + "b" * 40


class RecordingSigner:
    def sign(self, manifest):
        return {
            "schema_version": 1,
            "kind": "signed_work_manifest",
            "algorithm": "ed25519",
            "key_id": "test-key",
            "manifest": manifest,
            "signature": base64.b64encode(b"s" * 64).decode(),
        }


class ControlServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.store.initialize()
        scheduler = Scheduler(self.store)
        authenticator = RunnerAuthenticator(
            {
                "mini": RunnerAuthenticator.digest(MINI_TOKEN),
                "studio": RunnerAuthenticator.digest(STUDIO_TOKEN),
            }
        )
        self.service = ControlService(
            ControlPlane(self.store), scheduler, RecordingSigner(), authenticator
        )

    def test_poll_assigns_jobs_to_the_smallest_available_runners(self):
        self.queue_job(
            repository="Marvis-Labs/example-a",
            attempt_id="attempt:a",
            request_id="request:a",
            job_id="models:small",
            memory_gib=8,
        )
        self.queue_job(
            repository="Marvis-Labs/example-b",
            attempt_id="attempt:b",
            request_id="request:b",
            job_id="models:large",
            memory_gib=32,
        )
        self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )

        studio = self.service.poll(
            self.capability("studio", memory_gib=64), token=STUDIO_TOKEN, at=NOW
        )
        mini = self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )

        self.assertEqual(studio["envelope"]["manifest"]["job_id"], "models:large")
        self.assertEqual(mini["envelope"]["manifest"]["job_id"], "models:small")

    def test_poll_reuses_an_active_signed_assignment(self):
        self.queue_job(memory_gib=8)
        first = self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )
        replay = self.service.poll(
            self.capability("mini", memory_gib=16),
            token=MINI_TOKEN,
            at=NOW + timedelta(seconds=30),
        )

        self.assertEqual(replay, first)

    def test_server_owns_runner_heartbeat_and_authenticates_first(self):
        with self.assertRaises(AuthenticationError):
            self.service.poll(
                self.capability("mini", memory_gib=16), token=STUDIO_TOKEN, at=NOW
            )
        self.assertIsNone(self.store.get_runner("mini"))

        self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )
        self.assertEqual(
            self.store.get_runner("mini")["heartbeat_at"],
            "2026-09-04T12:00:00Z",
        )

    def test_decline_and_completion_are_bound_to_runner_and_generation(self):
        self.queue_job(memory_gib=8)
        assignment = self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )
        lease = assignment["lease"]
        response = {
            "schema_version": 1,
            "kind": "runner_response",
            "lease_id": lease["lease_id"],
            "attempt_id": lease["attempt_id"],
            "job_id": lease["job_id"],
            "runner_id": "mini",
            "generation": lease["generation"],
            "decision": "declined",
            "reason": "busy",
            "observed": {},
        }
        self.service.respond(response, token=MINI_TOKEN, at=NOW + timedelta(seconds=5))
        escalated = self.service.poll(
            self.capability("studio", memory_gib=64),
            token=STUDIO_TOKEN,
            at=NOW + timedelta(seconds=6),
        )
        result = self.result(escalated)

        with self.assertRaises(AuthenticationError):
            self.service.complete(
                result,
                generation=escalated["lease"]["generation"],
                token=MINI_TOKEN,
                at=NOW + timedelta(minutes=1),
            )
        self.service.complete(
            result,
            generation=escalated["lease"]["generation"],
            token=STUDIO_TOKEN,
            at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(self.store.get_attempt("attempt:1")["state"], "completed")

    def test_runner_credentials_require_restricted_file(self):
        path = Path(self.temporary_directory.name) / "runners.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "runner_credentials",
                    "token_digests": {"mini": RunnerAuthenticator.digest(MINI_TOKEN)},
                }
            )
        )
        path.chmod(0o600)
        RunnerAuthenticator.from_file(path).authenticate("mini", MINI_TOKEN)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            RunnerAuthenticator.from_file(path)

    def test_offline_runner_cannot_poll(self):
        capability = self.capability("mini", memory_gib=16)
        capability["status"] = "offline"
        with self.assertRaisesRegex(ContractError, "must report online"):
            self.service.poll(capability, token=MINI_TOKEN, at=NOW)

    def test_renew_rejects_the_wrong_generation(self):
        self.queue_job(memory_gib=8)
        assignment = self.service.poll(
            self.capability("mini", memory_gib=16), token=MINI_TOKEN, at=NOW
        )
        with self.assertRaisesRegex(StateConflict, "owner"):
            self.service.renew(
                assignment["lease"]["lease_id"],
                runner_id="mini",
                generation="generation:wrong",
                token=MINI_TOKEN,
                at=NOW + timedelta(minutes=1),
            )

    def queue_job(
        self,
        *,
        repository="Marvis-Labs/example-models",
        attempt_id="attempt:1",
        request_id="request:1",
        job_id="models:family",
        memory_gib,
    ):
        request = {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": request_id,
            "repository": repository,
            "pull_request": 7,
            "comment_id": 99,
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }
        self.store.create_attempt(
            request,
            attempt_id=attempt_id,
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        self.store.enqueue_jobs(
            attempt_id,
            [
                seal_manifest(
                    {
                        "schema_version": 1,
                        "kind": "work_manifest",
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "repository": repository,
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "contract_sha": "c" * 40,
                        "component": "models",
                        "subject": job_id.rsplit(":", 1)[-1],
                        "phases": ["synthetic", "checkpoint"],
                        "required_memory_gib": memory_gib,
                        "required_disk_gib": 8,
                        "payload": {"repository_payload": {"scenario": "small"}},
                    }
                )
            ],
            now="2026-09-04T12:00:00Z",
        )

    @staticmethod
    def capability(runner_id, *, memory_gib):
        return {
            "schema_version": 1,
            "kind": "runner_capability",
            "runner_id": runner_id,
            "labels": ["apple-silicon", "mlx-ci-sandbox-v1"],
            "memory_gib": memory_gib,
            "available_disk_gib": 128,
            "status": "online",
        }

    @staticmethod
    def result(assignment):
        manifest = assignment["envelope"]["manifest"]
        lease = assignment["lease"]
        return seal_result(
            {
                "schema_version": 1,
                "kind": "work_result",
                "job_id": manifest["job_id"],
                "attempt_id": manifest["attempt_id"],
                "repository": manifest["repository"],
                "runner_id": lease["runner_id"],
                "lease_id": lease["lease_id"],
                "outcome": "passed",
                "failure_code": None,
                "evidence": {"synthetic": {"outcome": "passed"}},
                "started_at": "2026-09-04T12:00:06Z",
                "finished_at": "2026-09-04T12:01:00Z",
            }
        )


if __name__ == "__main__":
    unittest.main()
