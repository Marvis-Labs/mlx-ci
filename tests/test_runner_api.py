import base64
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mlx_ci.contracts import canonical_digest, seal_manifest, seal_work_plan
from mlx_ci.control_plane import ControlPlane
from mlx_ci.runner_api import RunnerAPI
from mlx_ci.scheduler import Scheduler
from mlx_ci.service import ControlService, RunnerAuthenticator
from mlx_ci.store import StateStore

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TOKEN = "runner_" + "a" * 40
QUEUE_TOKEN = "queue_" + "b" * 40


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


class RunnerAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.store.initialize()
        scheduler = Scheduler(self.store)
        authenticator = RunnerAuthenticator(
            {"runner-1": RunnerAuthenticator.digest(TOKEN)}
        )
        control_plane = ControlService(
            ControlPlane(self.store), scheduler, RecordingSigner(), authenticator
        )
        self.api = RunnerAPI(
            control_plane,
            queue_token_digest=RunnerAuthenticator.digest(QUEUE_TOKEN),
            clock=lambda: NOW,
        )

    def test_poll_returns_signed_assignment(self):
        self.queue_job()

        status, headers, body = self.request(
            "/v1/runners/runner-1/poll", self.capability()
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["kind"], "runner_assignment")
        self.assertEqual(body["lease"]["runner_id"], "runner-1")

    def test_idle_poll_returns_no_content(self):
        status, _, body = self.request("/v1/runners/runner-1/poll", self.capability())

        self.assertEqual(status, "204 No Content")
        self.assertIsNone(body)

    def test_runner_response_is_bound_to_path_and_lease(self):
        self.queue_job()
        _, _, assignment = self.request("/v1/runners/runner-1/poll", self.capability())
        lease = assignment["lease"]
        response = {
            "schema_version": 1,
            "kind": "runner_response",
            "lease_id": lease["lease_id"],
            "attempt_id": lease["attempt_id"],
            "job_id": lease["job_id"],
            "runner_id": "runner-1",
            "generation": lease["generation"],
            "decision": "accepted",
            "reason": None,
            "observed": {},
        }

        status, _, body = self.request(
            f"/v1/leases/{lease['lease_id']}/respond",
            {
                "runner_id": "runner-1",
                "generation": lease["generation"],
                "response": response,
            },
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(body, response)

        forged = dict(response)
        forged["lease_id"] = "lease:forged"
        status, _, body = self.request(
            f"/v1/leases/{lease['lease_id']}/respond",
            {
                "runner_id": "runner-1",
                "generation": lease["generation"],
                "response": forged,
            },
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body, {"error": "invalid_request"})

    def test_runner_path_mismatch_is_rejected(self):
        capability = self.capability()
        capability["runner_id"] = "other-runner"

        status, _, body = self.request("/v1/runners/runner-1/poll", capability)

        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body, {"error": "invalid_request"})

    def test_bad_token_is_rejected_without_details(self):
        status, _, body = self.request(
            "/v1/runners/runner-1/poll",
            self.capability(),
            token="wrong_" + "b" * 40,
        )

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(body, {"error": "unauthorized"})
        self.assertIsNone(self.store.get_runner("runner-1"))

    def test_plain_http_is_rejected(self):
        status, _, body = self.request(
            "/v1/runners/runner-1/poll", self.capability(), scheme="http"
        )

        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body, {"error": "https_required"})

    def test_body_size_is_bounded(self):
        status = None

        def start_response(value, _):
            nonlocal status
            status = value

        body = list(
            self.api(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": "/v1/runners/runner-1/poll",
                    "CONTENT_TYPE": "application/json",
                    "CONTENT_LENGTH": "2100001",
                    "HTTP_AUTHORIZATION": f"Bearer {TOKEN}",
                    "wsgi.input": io.BytesIO(b"{}"),
                    "wsgi.url_scheme": "https",
                },
                start_response,
            )
        )

        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(json.loads(body[0]), {"error": "invalid_request"})

    def test_queue_submission_persists_an_immutable_attempt(self):
        status, _, body = self.request(
            "/v1/queues",
            {
                "request": self.run_request(),
                "plan": self.plan(),
                "attempt_id": "attempt:1",
            },
            token=QUEUE_TOKEN,
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(body["job_ids"], ["models:family"])
        self.assertEqual(body["disposition"], "created")
        self.assertEqual(self.store.get_attempt("attempt:1")["state"], "queued")

    def test_queue_submission_requires_separate_credential(self):
        status, _, body = self.request(
            "/v1/queues",
            {
                "request": self.run_request(),
                "plan": self.plan(),
                "attempt_id": "attempt:1",
            },
            token=TOKEN,
        )

        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(body, {"error": "unauthorized"})

    def request(self, path, payload, *, token=TOKEN, scheme="https"):
        raw = json.dumps(payload).encode()
        status = None
        response_headers = None

        def start_response(value, headers):
            nonlocal status, response_headers
            status = value
            response_headers = dict(headers)

        chunks = self.api(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": path,
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(raw)),
                "HTTP_AUTHORIZATION": f"Bearer {token}",
                "wsgi.input": io.BytesIO(raw),
                "wsgi.url_scheme": scheme,
            },
            start_response,
        )
        encoded = b"".join(chunks)
        return (
            status,
            response_headers,
            json.loads(encoded) if encoded else None,
        )

    def queue_job(self):
        request = self.run_request()
        queue = self.queue()
        self.store.create_attempt(
            request,
            attempt_id="attempt:1",
            base_sha="a" * 40,
            head_sha="b" * 40,
            contract_sha="c" * 40,
        )
        self.store.enqueue_jobs("attempt:1", queue["jobs"], now="2026-09-04T12:00:00Z")

    @staticmethod
    def run_request():
        return {
            "schema_version": 1,
            "kind": "run_request",
            "request_id": "request:1",
            "repository": "Marvis-Labs/example-models",
            "pull_request": 7,
            "comment_id": 99,
            "requester": "maintainer",
            "requested_at": "2026-09-04T12:00:00Z",
        }

    @staticmethod
    def queue():
        manifest = seal_manifest(
            {
                "schema_version": 1,
                "kind": "work_manifest",
                "job_id": "models:family",
                "attempt_id": "attempt:1",
                "repository": "Marvis-Labs/example-models",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "contract_sha": "c" * 40,
                "component": "models",
                "subject": "family",
                "phases": ["synthetic", "checkpoint"],
                "required_memory_gib": 16,
                "required_disk_gib": 8,
                "payload": {"repository_payload": {"scenario": "small"}},
            }
        )
        return {
            "schema_version": 1,
            "kind": "repository_queue",
            "attempt_id": "attempt:1",
            "repository": "Marvis-Labs/example-models",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "terminal_state": "planned",
            "jobs": [manifest],
        }

    @staticmethod
    def plan():
        runner_manifest = {
            "id": "models:family",
            "repository": "Marvis-Labs/example-models",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "contract_sha": "c" * 40,
            "component": "models",
            "subject": "family",
            "phases": ["synthetic", "checkpoint"],
            "required_memory_gib": 16,
            "required_disk_gib": 8,
            "operation": "generate",
        }
        runner_manifest["manifest_digest"] = canonical_digest(runner_manifest)
        return seal_work_plan(
            {
                "schema_version": 1,
                "kind": "repository_work_plan",
                "plan_id": "plan:1",
                "repository": "Marvis-Labs/example-models",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "contract_sha": "c" * 40,
                "jobs": [runner_manifest],
                "metadata": {"planning_outcome": "ready"},
            }
        )

    @staticmethod
    def capability():
        return {
            "schema_version": 1,
            "kind": "runner_capability",
            "runner_id": "runner-1",
            "labels": ["apple-silicon", "mlx-ci-sandbox-v1"],
            "memory_gib": 16,
            "available_disk_gib": 128,
            "status": "online",
        }


if __name__ == "__main__":
    unittest.main()
