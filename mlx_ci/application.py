from __future__ import annotations

import os
from collections.abc import Mapping

from mlx_ci.control_plane import ControlPlane
from mlx_ci.runner_api import RunnerAPI
from mlx_ci.scheduler import Scheduler
from mlx_ci.service import ControlService, RunnerAuthenticator
from mlx_ci.signing import OpenSSLEd25519Signer
from mlx_ci.store import StateStore


def create_application(
    environ: Mapping[str, str] | None = None,
    *,
    allow_insecure: bool = False,
) -> RunnerAPI:
    environ = os.environ if environ is None else environ
    state_path = _required(environ, "MLX_CI_STATE_PATH")
    credentials_path = _required(environ, "MLX_CI_RUNNER_CREDENTIALS")
    signing_key = _required(environ, "MLX_CI_SIGNING_KEY")
    signing_key_id = _required(environ, "MLX_CI_SIGNING_KEY_ID")
    queue_token_digest = _required(environ, "MLX_CI_QUEUE_TOKEN_DIGEST")
    store = StateStore(state_path)
    store.initialize()
    scheduler = Scheduler(store)
    signer = OpenSSLEd25519Signer(
        key_id=signing_key_id,
        private_key=signing_key,
    )
    authenticator = RunnerAuthenticator.from_file(credentials_path)
    return RunnerAPI(
        ControlService(ControlPlane(store), scheduler, signer, authenticator),
        queue_token_digest=queue_token_digest,
        allow_insecure=allow_insecure,
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value
