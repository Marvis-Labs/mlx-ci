from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mlx_ci.contracts import (
    COMMIT_PATTERN,
    IDENTIFIER_PATTERN,
    canonical_json,
    validate_job,
    validate_request,
    validate_runner,
)

ACTIVE_ATTEMPT_STATES = {"queued", "running"}
TERMINAL_ATTEMPT_STATES = {"completed", "failed", "cancelled"}
ATTEMPT_STATES = ACTIVE_ATTEMPT_STATES | TERMINAL_ATTEMPT_STATES


class StateError(RuntimeError):
    pass


class StateConflict(StateError):
    pass


class StateStore:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    repository TEXT NOT NULL,
                    pull_request INTEGER NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    contract_sha TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'queued', 'running', 'completed', 'failed', 'cancelled'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt_per_revision
                    ON attempts(repository, pull_request, head_sha)
                    WHERE state IN ('queued', 'running');

                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    attempt_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    required_memory_gib INTEGER NOT NULL,
                    required_disk_gib INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'queued', 'leased', 'completed', 'failed', 'cancelled'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, job_id),
                    FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
                );

                CREATE INDEX IF NOT EXISTS queued_jobs
                    ON jobs(state, created_at, attempt_id, job_id);

                CREATE TABLE IF NOT EXISTS runners (
                    runner_id TEXT PRIMARY KEY,
                    capability_json TEXT NOT NULL,
                    memory_gib INTEGER NOT NULL,
                    available_disk_gib INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('online', 'draining', 'offline')
                    ),
                    heartbeat_at TEXT NOT NULL
                );
                """
            )

    def create_attempt(
        self,
        request: dict[str, Any],
        *,
        attempt_id: str,
        base_sha: str,
        head_sha: str,
        contract_sha: str,
    ) -> tuple[dict[str, Any], bool]:
        request = validate_request(request)
        _identifier(attempt_id, "attempt_id")
        for name, value in (
            ("base_sha", base_sha),
            ("head_sha", head_sha),
            ("contract_sha", contract_sha),
        ):
            _commit(value, name)

        record = {
            "attempt_id": attempt_id,
            "request_id": request["request_id"],
            "repository": request["repository"],
            "pull_request": request["pull_request"],
            "base_sha": base_sha,
            "head_sha": head_sha,
            "contract_sha": contract_sha,
            "state": "queued",
            "created_at": request["requested_at"],
            "completed_at": None,
        }
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT requests.request_json, attempts.*
                FROM requests
                JOIN attempts USING (attempt_id)
                WHERE requests.request_id = ?
                """,
                (request["request_id"],),
            ).fetchone()
            if existing is not None:
                persisted_request = json.loads(existing["request_json"])
                if persisted_request != request:
                    raise StateConflict("request_id was reused with different content")
                for field, value in (
                    ("base_sha", base_sha),
                    ("head_sha", head_sha),
                    ("contract_sha", contract_sha),
                ):
                    if existing[field] != value:
                        raise StateConflict(
                            f"request_id was reused with a different {field}"
                        )
                return _attempt_record(existing), True

            active = connection.execute(
                """
                SELECT * FROM attempts
                WHERE repository = ? AND pull_request = ? AND head_sha = ?
                  AND state IN ('queued', 'running')
                """,
                (request["repository"], request["pull_request"], head_sha),
            ).fetchone()
            if active is not None:
                connection.execute(
                    "INSERT INTO requests VALUES (?, ?, ?)",
                    (
                        request["request_id"],
                        active["attempt_id"],
                        canonical_json(request).decode(),
                    ),
                )
                return dict(active), True

            connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, request_id, repository, pull_request, base_sha,
                    head_sha, contract_sha, state, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record.values()),
            )
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?)",
                (
                    request["request_id"],
                    attempt_id,
                    canonical_json(request).decode(),
                ),
            )
        return record, False

    def enqueue_jobs(
        self, attempt_id: str, manifests: Sequence[dict[str, Any]], *, now: str
    ) -> list[dict[str, Any]]:
        _identifier(attempt_id, "attempt_id")
        if not manifests:
            raise StateError("at least one work manifest is required")
        validated = [validate_job(manifest) for manifest in manifests]
        _parse_timestamp(now, "now")

        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                raise StateError("attempt does not exist")
            if attempt["state"] not in ACTIVE_ATTEMPT_STATES:
                raise StateConflict("cannot enqueue work for a terminal attempt")

            for manifest in validated:
                self._validate_manifest_identity(dict(attempt), manifest)
                existing = connection.execute(
                    """
                    SELECT manifest_digest FROM jobs
                    WHERE attempt_id = ? AND job_id = ?
                    """,
                    (attempt_id, manifest["job_id"]),
                ).fetchone()
                if existing is not None:
                    if existing["manifest_digest"] != manifest["manifest_digest"]:
                        raise StateConflict("job_id was reused with different content")
                    continue
                connection.execute(
                    """
                    INSERT INTO jobs (
                        attempt_id, job_id, repository, manifest_json,
                        manifest_digest, required_memory_gib, required_disk_gib,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        attempt_id,
                        manifest["job_id"],
                        manifest["repository"],
                        canonical_json(manifest).decode(),
                        manifest["manifest_digest"],
                        manifest["required_memory_gib"],
                        manifest["required_disk_gib"],
                        now,
                        now,
                    ),
                )
        return self.list_jobs(attempt_id=attempt_id)

    def record_runner(self, capability: dict[str, Any]) -> dict[str, Any]:
        capability = validate_runner(capability)
        serialized = canonical_json(capability).decode()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT capability_json, heartbeat_at FROM runners WHERE runner_id = ?",
                (capability["runner_id"],),
            ).fetchone()
            if existing is not None:
                incoming_at = _parse_timestamp(
                    capability["heartbeat_at"], "heartbeat_at"
                )
                existing_at = _parse_timestamp(existing["heartbeat_at"], "heartbeat_at")
                if incoming_at < existing_at:
                    return json.loads(existing["capability_json"])
                if (
                    incoming_at == existing_at
                    and serialized != existing["capability_json"]
                ):
                    raise StateConflict("runner heartbeat timestamp was reused")
            connection.execute(
                """
                INSERT INTO runners (
                    runner_id, capability_json, memory_gib, available_disk_gib,
                    status, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    capability_json = excluded.capability_json,
                    memory_gib = excluded.memory_gib,
                    available_disk_gib = excluded.available_disk_gib,
                    status = excluded.status,
                    heartbeat_at = excluded.heartbeat_at
                """,
                (
                    capability["runner_id"],
                    serialized,
                    capability["memory_gib"],
                    capability["available_disk_gib"],
                    capability["status"],
                    capability["heartbeat_at"],
                ),
            )
        runner = self.get_runner(capability["runner_id"])
        if runner is None:
            raise StateError("runner heartbeat was not persisted")
        return runner

    def set_attempt_state(
        self, attempt_id: str, state: str, *, now: str
    ) -> dict[str, Any]:
        _identifier(attempt_id, "attempt_id")
        if state not in ATTEMPT_STATES:
            raise StateError("attempt state is invalid")
        _parse_timestamp(now, "now")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if current is None:
                raise StateError("attempt does not exist")
            if state == current["state"]:
                attempt = connection.execute(
                    "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                return dict(attempt)
            if current["state"] in TERMINAL_ATTEMPT_STATES:
                raise StateConflict("terminal attempts cannot transition")
            if current["state"] == "queued" and state == "completed":
                raise StateConflict("queued attempts cannot complete directly")
            completed_at = now if state in TERMINAL_ATTEMPT_STATES else None
            connection.execute(
                "UPDATE attempts SET state = ?, completed_at = ? WHERE attempt_id = ?",
                (state, completed_at, attempt_id),
            )
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise StateError("attempt disappeared after update")
        return attempt

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_runner(self, runner_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT capability_json FROM runners WHERE runner_id = ?",
                (runner_id,),
            ).fetchone()
        return json.loads(row["capability_json"]) if row is not None else None

    def list_jobs(self, *, attempt_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        parameters: tuple[str, ...] = ()
        if attempt_id is not None:
            query += " WHERE attempt_id = ?"
            parameters = (attempt_id,)
        query += " ORDER BY created_at, attempt_id, job_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._job_record(row) for row in rows]

    @staticmethod
    def _job_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["manifest"] = json.loads(record.pop("manifest_json"))
        return record

    @staticmethod
    def _validate_manifest_identity(
        attempt: dict[str, Any], manifest: dict[str, Any]
    ) -> None:
        for field in (
            "attempt_id",
            "repository",
            "base_sha",
            "head_sha",
            "contract_sha",
        ):
            if manifest[field] != attempt[field]:
                raise StateConflict(f"manifest {field} does not match its attempt")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise StateError(f"{field} is invalid")


def _commit(value: str, field: str) -> None:
    if not isinstance(value, str) or COMMIT_PATTERN.fullmatch(value) is None:
        raise StateError(f"{field} must be a full lowercase commit SHA")


def _parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StateError(f"{field} must be a UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise StateError(f"{field} must be a UTC RFC3339 timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise StateError(f"{field} must be a UTC RFC3339 timestamp")
    return parsed


def _attempt_record(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key != "request_json"}
