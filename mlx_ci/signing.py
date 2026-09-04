from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mlx_ci.contracts import (
    IDENTIFIER_PATTERN,
    ContractError,
    canonical_json,
    validate_envelope,
    validate_job,
)


class SigningError(RuntimeError):
    pass


class OpenSSLEd25519Signer:
    def __init__(
        self,
        *,
        key_id: str,
        private_key: str | Path,
        openssl: str = "openssl",
    ):
        if IDENTIFIER_PATTERN.fullmatch(key_id) is None:
            raise ValueError("signing key id is invalid")
        self.key_id = key_id
        self.private_key = _key_path(private_key, name="private")
        self.openssl = openssl

    def sign(self, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest = validate_job(manifest)
        try:
            with (
                _open_key(self.private_key, private=True) as key_fd,
                tempfile.NamedTemporaryFile() as message_file,
            ):
                message_file.write(canonical_json(manifest))
                message_file.flush()
                completed = subprocess.run(
                    [
                        self.openssl,
                        "pkeyutl",
                        "-sign",
                        "-rawin",
                        "-inkey",
                        f"/dev/fd/{key_fd}",
                        "-in",
                        message_file.name,
                    ],
                    capture_output=True,
                    check=False,
                    pass_fds=(key_fd,),
                    timeout=10,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SigningError("manifest signing failed") from error
        if completed.returncode != 0 or len(completed.stdout) != 64:
            raise SigningError("manifest signing failed")
        envelope = {
            "schema_version": 1,
            "kind": "signed_work_manifest",
            "algorithm": "ed25519",
            "key_id": self.key_id,
            "manifest": manifest,
            "signature": base64.b64encode(completed.stdout).decode(),
        }
        try:
            return validate_envelope(envelope)
        except ContractError as error:
            raise SigningError("signed manifest is invalid") from error


class OpenSSLEd25519Verifier:
    def __init__(
        self,
        public_keys: dict[str, str | Path],
        *,
        openssl: str = "openssl",
    ):
        if not public_keys:
            raise ValueError("at least one public key is required")
        if any(IDENTIFIER_PATTERN.fullmatch(key_id) is None for key_id in public_keys):
            raise ValueError("public key id is invalid")
        self.public_keys = {
            key_id: _key_path(path, name="public")
            for key_id, path in public_keys.items()
        }
        self.openssl = openssl

    def verify(self, envelope: dict[str, Any]) -> dict[str, Any]:
        envelope = validate_envelope(envelope)
        public_key = self.public_keys.get(envelope["key_id"])
        if public_key is None:
            raise SigningError("manifest signing key is not trusted")
        signature = base64.b64decode(envelope["signature"], validate=True)
        try:
            with (
                _open_key(public_key, private=False) as key_fd,
                tempfile.NamedTemporaryFile() as message_file,
                tempfile.NamedTemporaryFile() as signature_file,
            ):
                message_file.write(canonical_json(envelope["manifest"]))
                message_file.flush()
                signature_file.write(signature)
                signature_file.flush()
                completed = subprocess.run(
                    [
                        self.openssl,
                        "pkeyutl",
                        "-verify",
                        "-pubin",
                        "-inkey",
                        f"/dev/fd/{key_fd}",
                        "-sigfile",
                        signature_file.name,
                        "-rawin",
                        "-in",
                        message_file.name,
                    ],
                    capture_output=True,
                    check=False,
                    pass_fds=(key_fd,),
                    timeout=10,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SigningError("manifest signature verification failed") from error
        if completed.returncode != 0:
            raise SigningError("manifest signature verification failed")
        return envelope["manifest"]


def _key_path(value: str | Path, *, name: str) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SigningError(f"{name} key is unavailable") from error
    if path.is_symlink() or not resolved.is_file():
        raise SigningError(f"{name} key must be a regular non-symlink file")
    return resolved


@contextmanager
def _open_key(path: Path, *, private: bool) -> Iterator[int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SigningError("signing key is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SigningError("signing key must be a regular file")
        if private and metadata.st_mode & 0o077:
            raise SigningError("private key permissions must be owner-only")
        if private and hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise SigningError("private key must be owned by the service user")
        if not private and metadata.st_mode & 0o022:
            raise SigningError("public key must not be group or world writable")
        yield descriptor
    finally:
        os.close(descriptor)
