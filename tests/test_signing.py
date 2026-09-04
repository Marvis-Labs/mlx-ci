import base64
import subprocess
import tempfile
import unittest
from pathlib import Path

from mlx_ci.contracts import seal_manifest
from mlx_ci.signing import (
    OpenSSLEd25519Signer,
    OpenSSLEd25519Verifier,
    SigningError,
)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
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
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(self.private_key),
                "-pubout",
                "-out",
                str(self.public_key),
            ],
            check=True,
            capture_output=True,
        )
        self.signer = OpenSSLEd25519Signer(
            key_id="control-plane-2026-09", private_key=self.private_key
        )
        self.verifier = OpenSSLEd25519Verifier(
            {"control-plane-2026-09": self.public_key}
        )

    def test_signs_and_verifies_canonical_manifest(self):
        manifest = seal_manifest(self.manifest())

        envelope = self.signer.sign(manifest)

        self.assertEqual(self.verifier.verify(envelope), manifest)

    def test_verification_rejects_manifest_mutation(self):
        envelope = self.signer.sign(seal_manifest(self.manifest()))
        envelope["manifest"]["required_memory_gib"] = 32

        with self.assertRaisesRegex(ValueError, "manifest_digest"):
            self.verifier.verify(envelope)

    def test_verification_rejects_signature_mutation(self):
        envelope = self.signer.sign(seal_manifest(self.manifest()))
        signature = bytearray(base64.b64decode(envelope["signature"]))
        signature[0] ^= 1
        envelope["signature"] = base64.b64encode(signature).decode()

        with self.assertRaisesRegex(SigningError, "verification failed"):
            self.verifier.verify(envelope)

    def test_unknown_key_is_rejected(self):
        envelope = self.signer.sign(seal_manifest(self.manifest()))
        envelope["key_id"] = "unknown-key"

        with self.assertRaisesRegex(SigningError, "not trusted"):
            self.verifier.verify(envelope)

    def test_private_key_permissions_are_restricted(self):
        self.private_key.chmod(0o644)
        signer = OpenSSLEd25519Signer(key_id="key", private_key=self.private_key)

        with self.assertRaisesRegex(SigningError, "owner-only"):
            signer.sign(seal_manifest(self.manifest()))

    def test_private_key_replacement_is_revalidated(self):
        signer = OpenSSLEd25519Signer(key_id="key", private_key=self.private_key)
        self.private_key.chmod(0o644)

        with self.assertRaisesRegex(SigningError, "owner-only"):
            signer.sign(seal_manifest(self.manifest()))

    @staticmethod
    def manifest():
        return {
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


if __name__ == "__main__":
    unittest.main()
