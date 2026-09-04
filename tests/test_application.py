import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mlx_ci.application import create_application
from mlx_ci.runner_api import RunnerAPI
from mlx_ci.service import RunnerAuthenticator

TOKEN = "runner_" + "a" * 40


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.private_key = self.root / "private.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "ED25519",
                "-out",
                str(self.private_key),
            ],
            check=True,
            capture_output=True,
        )
        self.private_key.chmod(0o600)
        self.credentials = self.root / "runners.json"
        self.credentials.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "runner_credentials",
                    "token_digests": {"runner-1": RunnerAuthenticator.digest(TOKEN)},
                }
            )
        )
        self.credentials.chmod(0o600)

    def test_builds_wsgi_application_from_explicit_paths(self):
        application = create_application(
            {
                "MLX_CI_STATE_PATH": str(self.root / "state.sqlite3"),
                "MLX_CI_RUNNER_CREDENTIALS": str(self.credentials),
                "MLX_CI_SIGNING_KEY": str(self.private_key),
                "MLX_CI_SIGNING_KEY_ID": "control-plane-2026-09",
                "MLX_CI_QUEUE_TOKEN_DIGEST": RunnerAuthenticator.digest(TOKEN),
            }
        )

        self.assertIsInstance(application, RunnerAPI)
        self.assertTrue((self.root / "state.sqlite3").is_file())

    def test_missing_configuration_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "MLX_CI_STATE_PATH"):
            create_application({})


if __name__ == "__main__":
    unittest.main()
