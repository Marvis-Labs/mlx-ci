from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

from mlx_ci.contracts import DIGEST_PATTERN, IDENTIFIER_PATTERN, ContractError
from mlx_ci.service import AuthenticationError, ControlService, authenticate_token
from mlx_ci.signing import SigningError
from mlx_ci.store import StateConflict, StateError

LEASE_PATH = re.compile(
    rf"/v1/leases/(?P<lease_id>{IDENTIFIER_PATTERN.pattern})/(?P<operation>renew|respond|complete)"
)
RUNNER_PATH = re.compile(
    rf"/v1/runners/(?P<runner_id>{IDENTIFIER_PATTERN.pattern})/poll"
)
MAX_BODY_BYTES = 2_100_000
MAX_QUEUE_BYTES = 16_100_000


class RunnerAPI:
    def __init__(
        self,
        control_plane: ControlService,
        *,
        queue_token_digest: str | None = None,
        clock: Callable[[], datetime] | None = None,
        allow_insecure: bool = False,
    ):
        if (
            queue_token_digest is not None
            and DIGEST_PATTERN.fullmatch(queue_token_digest) is None
        ):
            raise ValueError("queue token digest is invalid")
        self.control_plane = control_plane
        self.queue_token_digest = queue_token_digest
        self.clock = clock or (lambda: datetime.now(UTC))
        self.allow_insecure = allow_insecure

    def __call__(
        self,
        environ: Mapping[str, Any],
        start_response: Callable[[str, list[tuple[str, str]]], None],
    ) -> Iterable[bytes]:
        try:
            status, payload = self._handle(environ)
        except AuthenticationError:
            status, payload = HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
        except StateConflict:
            status, payload = HTTPStatus.CONFLICT, {"error": "state_conflict"}
        except (ContractError, StateError, ValueError):
            status, payload = HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}
        except SigningError:
            status, payload = (
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "signing_unavailable"},
            )
        except Exception:
            status, payload = (
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"},
            )
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = [
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
        ]
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]

    def _handle(
        self, environ: Mapping[str, Any]
    ) -> tuple[HTTPStatus, dict[str, Any] | None]:
        if environ.get("REQUEST_METHOD") != "POST":
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"}
        if not self.allow_insecure and environ.get("wsgi.url_scheme") != "https":
            return HTTPStatus.BAD_REQUEST, {"error": "https_required"}
        path = environ.get("PATH_INFO")
        if not isinstance(path, str):
            raise ContractError("request path is invalid")
        token = _bearer_token(environ.get("HTTP_AUTHORIZATION"))
        body = _json_body(
            environ,
            maximum=MAX_QUEUE_BYTES if path == "/v1/queues" else MAX_BODY_BYTES,
        )
        if path == "/v1/queues":
            if self.queue_token_digest is None or not authenticate_token(
                self.queue_token_digest, token
            ):
                raise AuthenticationError("queue authentication failed")
            _exact_fields(body, {"request", "plan", "attempt_id"})
            request = body.get("request")
            plan = body.get("plan")
            attempt_id = body.get("attempt_id")
            if (
                not isinstance(request, Mapping)
                or not isinstance(plan, Mapping)
                or not isinstance(attempt_id, str)
            ):
                raise ContractError("queue submission is invalid")
            submitted = self.control_plane.submit(
                dict(request), dict(plan), attempt_id=attempt_id
            )
            return HTTPStatus.OK, submitted.as_dict()
        runner_match = RUNNER_PATH.fullmatch(path)
        if runner_match is not None:
            runner_id = runner_match.group("runner_id")
            if body.get("runner_id") != runner_id:
                raise ContractError("runner path does not match request")
            assignment = self.control_plane.poll(body, token=token, at=self.clock())
            if assignment is None:
                return HTTPStatus.NO_CONTENT, None
            return HTTPStatus.OK, assignment
        lease_match = LEASE_PATH.fullmatch(path)
        if lease_match is None:
            return HTTPStatus.NOT_FOUND, {"error": "not_found"}
        lease_id = lease_match.group("lease_id")
        operation = lease_match.group("operation")
        runner_id = _string(body, "runner_id")
        generation = _string(body, "generation")
        if operation == "renew":
            _exact_fields(body, {"runner_id", "generation"})
            lease = self.control_plane.renew(
                lease_id,
                runner_id=runner_id,
                generation=generation,
                token=token,
                at=self.clock(),
            )
            return HTTPStatus.OK, lease
        if operation == "respond":
            _exact_fields(body, {"runner_id", "generation", "response"})
            response = body.get("response")
            if (
                not isinstance(response, Mapping)
                or response.get("runner_id") != runner_id
                or response.get("lease_id") != lease_id
                or response.get("generation") != generation
            ):
                raise ContractError("runner response does not match request")
            recorded = self.control_plane.respond(
                response, token=token, at=self.clock()
            )
            return HTTPStatus.OK, recorded
        _exact_fields(body, {"runner_id", "generation", "result"})
        result = body.get("result")
        if not isinstance(result, Mapping) or result.get("runner_id") != runner_id:
            raise ContractError("result runner does not match request")
        completed = self.control_plane.complete(
            result,
            generation=generation,
            token=token,
            at=self.clock(),
        )
        return HTTPStatus.OK, completed


def _bearer_token(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("Bearer "):
        raise AuthenticationError("runner authentication failed")
    token = value.removeprefix("Bearer ")
    if not token or any(character.isspace() for character in token):
        raise AuthenticationError("runner authentication failed")
    return token


def _json_body(environ: Mapping[str, Any], *, maximum: int) -> dict[str, Any]:
    if environ.get("CONTENT_TYPE") != "application/json":
        raise ContractError("request content type is invalid")
    try:
        length = int(environ.get("CONTENT_LENGTH", ""))
    except (TypeError, ValueError) as error:
        raise ContractError("request content length is invalid") from error
    if not 0 < length <= maximum:
        raise ContractError("request body size is invalid")
    stream = environ.get("wsgi.input")
    if stream is None:
        raise ContractError("request body is unavailable")
    raw = stream.read(length + 1)
    if len(raw) != length:
        raise ContractError("request body length does not match")
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise ContractError("request body is invalid JSON") from error
    if not isinstance(value, dict):
        raise ContractError("request body must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ContractError("request fields are invalid")


def _string(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise ContractError(f"{field} must be a string")
    return item
