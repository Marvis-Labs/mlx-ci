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
    TIMESTAMP_PATTERN,
    canonical_json,
    validate_job,
    validate_lease,
    validate_request,
    validate_result,
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

                CREATE TABLE IF NOT EXISTS leases (
                    lease_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT,
                    FOREIGN KEY (attempt_id, job_id)
                        REFERENCES jobs(attempt_id, job_id),
                    FOREIGN KEY (runner_id) REFERENCES runners(runner_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_job
                    ON leases(attempt_id, job_id)
                    WHERE released_at IS NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_runner
                    ON leases(runner_id)
                    WHERE released_at IS NULL;

                CREATE TABLE IF NOT EXISTS rejections (
                    attempt_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, job_id, runner_id),
                    FOREIGN KEY (attempt_id, job_id)
                        REFERENCES jobs(attempt_id, job_id),
                    FOREIGN KEY (runner_id) REFERENCES runners(runner_id)
                );

                CREATE TABLE IF NOT EXISTS results (
                    attempt_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, job_id),
                    FOREIGN KEY (attempt_id, job_id)
                        REFERENCES jobs(attempt_id, job_id)
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
            if state == "completed":
                unfinished = connection.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE attempt_id = ? AND state IN ('queued', 'leased')
                    """,
                    (attempt_id,),
                ).fetchone()[0]
                if unfinished:
                    raise StateConflict("attempt has unfinished jobs")
            if state in {"failed", "cancelled"}:
                leases = connection.execute(
                    """
                    SELECT * FROM leases
                    WHERE attempt_id = ? AND released_at IS NULL
                    """,
                    (attempt_id,),
                ).fetchall()
                for lease in leases:
                    connection.execute(
                        """
                        UPDATE leases SET released_at = ?, release_reason = ?
                        WHERE lease_id = ?
                        """,
                        (now, f"attempt_{state}", lease["lease_id"]),
                    )
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, updated_at = ?
                    WHERE attempt_id = ? AND state IN ('queued', 'leased')
                    """,
                    (state, now, attempt_id),
                )
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

    def claim_next(
        self,
        *,
        lease_id: str,
        generation: str,
        now: str,
        expires_at: str,
        stale_before: str,
    ) -> dict[str, Any] | None:
        _identifier(lease_id, "lease_id")
        _identifier(generation, "generation")
        current_time = _parse_timestamp(now, "now")
        if _parse_timestamp(expires_at, "expires_at") <= current_time:
            raise StateError("lease expiry must be after acquisition")
        _parse_timestamp(stale_before, "stale_before")

        with self._transaction() as connection:
            self._reap_expired(connection, now)
            row = connection.execute(
                """
                SELECT
                    jobs.attempt_id,
                    jobs.job_id,
                    jobs.manifest_json,
                    runners.runner_id
                FROM jobs
                JOIN attempts USING (attempt_id)
                JOIN runners
                    ON runners.status = 'online'
                   AND runners.memory_gib >= jobs.required_memory_gib
                   AND runners.available_disk_gib >= jobs.required_disk_gib
                   AND julianday(runners.heartbeat_at) >= julianday(?)
                WHERE jobs.state = 'queued'
                  AND attempts.state IN ('queued', 'running')
                  AND NOT EXISTS (
                      SELECT 1 FROM leases
                      WHERE leases.runner_id = runners.runner_id
                        AND leases.released_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM rejections
                      WHERE rejections.attempt_id = jobs.attempt_id
                        AND rejections.job_id = jobs.job_id
                        AND rejections.runner_id = runners.runner_id
                  )
                ORDER BY
                    jobs.created_at,
                    jobs.attempt_id,
                    jobs.job_id,
                    runners.memory_gib,
                    runners.available_disk_gib,
                    runners.runner_id
                LIMIT 1
                """,
                (stale_before,),
            ).fetchone()
            if row is None:
                return None

            lease = {
                "schema_version": 1,
                "kind": "runner_lease",
                "lease_id": lease_id,
                "attempt_id": row["attempt_id"],
                "job_id": row["job_id"],
                "runner_id": row["runner_id"],
                "generation": generation,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": expires_at,
            }
            validate_lease(lease)
            connection.execute(
                """
                INSERT INTO leases (
                    lease_id, attempt_id, job_id, runner_id, generation,
                    acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    row["attempt_id"],
                    row["job_id"],
                    row["runner_id"],
                    generation,
                    now,
                    now,
                    expires_at,
                ),
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'leased', updated_at = ?
                WHERE attempt_id = ? AND job_id = ?
                """,
                (now, row["attempt_id"], row["job_id"]),
            )
            connection.execute(
                """
                UPDATE attempts SET state = 'running'
                WHERE attempt_id = ? AND state = 'queued'
                """,
                (row["attempt_id"],),
            )
            return {
                "lease": lease,
                "manifest": json.loads(row["manifest_json"]),
            }

    def renew_lease(
        self,
        lease_id: str,
        *,
        runner_id: str,
        generation: str,
        now: str,
        expires_at: str,
    ) -> dict[str, Any]:
        current_time = _parse_timestamp(now, "now")
        if _parse_timestamp(expires_at, "expires_at") <= current_time:
            raise StateError("lease expiry must follow its heartbeat")
        with self._transaction() as connection:
            lease = self._owned_lease(
                connection, lease_id, runner_id=runner_id, generation=generation
            )
            heartbeat_time = _parse_timestamp(lease["heartbeat_at"], "heartbeat_at")
            expiry_time = _parse_timestamp(lease["expires_at"], "expires_at")
            if expiry_time <= current_time:
                self._reap_expired(connection, now)
                raise StateConflict("lease has expired")
            if current_time < heartbeat_time:
                raise StateConflict("lease heartbeat cannot move backwards")
            if _parse_timestamp(expires_at, "expires_at") <= expiry_time:
                raise StateConflict("lease renewal must extend its expiry")
            connection.execute(
                """
                UPDATE leases SET heartbeat_at = ?, expires_at = ?
                WHERE lease_id = ?
                """,
                (now, expires_at, lease_id),
            )
            lease = connection.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            return self._lease_record(lease)

    def reject_lease(
        self,
        lease_id: str,
        *,
        runner_id: str,
        generation: str,
        reason: str,
        now: str,
    ) -> None:
        _identifier(reason, "reason")
        _parse_timestamp(now, "now")
        with self._transaction() as connection:
            self._reap_expired(connection, now)
            lease = self._owned_lease(
                connection, lease_id, runner_id=runner_id, generation=generation
            )
            if _parse_timestamp(now, "now") < _parse_timestamp(
                lease["heartbeat_at"], "heartbeat_at"
            ):
                raise StateConflict("lease release cannot predate its heartbeat")
            connection.execute(
                """
                INSERT OR IGNORE INTO rejections (
                    attempt_id, job_id, runner_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    lease["attempt_id"],
                    lease["job_id"],
                    runner_id,
                    reason,
                    now,
                ),
            )
            self._release_lease(connection, lease, reason="rejected", now=now)

    def complete_lease(
        self,
        result: dict[str, Any],
        *,
        generation: str,
        now: str,
    ) -> dict[str, Any]:
        result = validate_result(result)
        _identifier(generation, "generation")
        _parse_timestamp(now, "now")
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT result_json, result_digest FROM results
                WHERE attempt_id = ? AND job_id = ?
                """,
                (result["attempt_id"], result["job_id"]),
            ).fetchone()
            if existing is not None:
                if existing["result_digest"] != result["result_digest"]:
                    raise StateConflict(
                        "job result was replaced with different content"
                    )
                return json.loads(existing["result_json"])

            self._reap_expired(connection, now)
            lease = self._owned_lease(
                connection,
                result["lease_id"],
                runner_id=result["runner_id"],
                generation=generation,
            )
            for field in ("attempt_id", "job_id"):
                if result[field] != lease[field]:
                    raise StateConflict(f"result {field} does not match its lease")
            job = connection.execute(
                """
                SELECT repository FROM jobs
                WHERE attempt_id = ? AND job_id = ?
                """,
                (lease["attempt_id"], lease["job_id"]),
            ).fetchone()
            if job is None or result["repository"] != job["repository"]:
                raise StateConflict("result repository does not match its job")
            if _parse_timestamp(result["started_at"], "started_at") < _parse_timestamp(
                lease["acquired_at"], "acquired_at"
            ):
                raise StateConflict("result started before its lease")
            if _parse_timestamp(
                result["finished_at"], "finished_at"
            ) > _parse_timestamp(now, "now"):
                raise StateConflict("result finished in the future")

            connection.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                (
                    result["attempt_id"],
                    result["job_id"],
                    canonical_json(result).decode(),
                    result["result_digest"],
                    now,
                ),
            )
            job_state = _job_state_for_outcome(result["outcome"])
            connection.execute(
                """
                UPDATE jobs SET state = ?, updated_at = ?
                WHERE attempt_id = ? AND job_id = ?
                """,
                (job_state, now, result["attempt_id"], result["job_id"]),
            )
            connection.execute(
                """
                UPDATE leases
                SET released_at = ?, release_reason = 'completed'
                WHERE lease_id = ?
                """,
                (now, result["lease_id"]),
            )
        return result

    def reap_expired(self, *, now: str) -> int:
        _parse_timestamp(now, "now")
        with self._transaction() as connection:
            return self._reap_expired(connection, now)

    def list_leases(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM leases ORDER BY acquired_at, lease_id"
            ).fetchall()
        return [self._lease_record(row) for row in rows]

    def get_result(self, attempt_id: str, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT result_json FROM results
                WHERE attempt_id = ? AND job_id = ?
                """,
                (attempt_id, job_id),
            ).fetchone()
        return json.loads(row["result_json"]) if row is not None else None

    @staticmethod
    def _job_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["manifest"] = json.loads(record.pop("manifest_json"))
        return record

    @staticmethod
    def _lease_record(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _owned_lease(
        connection: sqlite3.Connection,
        lease_id: str,
        *,
        runner_id: str,
        generation: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        if row is None or row["released_at"] is not None:
            raise StateConflict("active lease does not exist")
        if row["runner_id"] != runner_id or row["generation"] != generation:
            raise StateConflict("lease owner does not match")
        return row

    @staticmethod
    def _release_lease(
        connection: sqlite3.Connection,
        lease: sqlite3.Row,
        *,
        reason: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE leases SET released_at = ?, release_reason = ?
            WHERE lease_id = ?
            """,
            (now, reason, lease["lease_id"]),
        )
        connection.execute(
            """
            UPDATE jobs SET state = 'queued', updated_at = ?
            WHERE attempt_id = ? AND job_id = ? AND state = 'leased'
            """,
            (now, lease["attempt_id"], lease["job_id"]),
        )

    @classmethod
    def _reap_expired(cls, connection: sqlite3.Connection, now: str) -> int:
        rows = connection.execute(
            """
            SELECT * FROM leases
            WHERE released_at IS NULL
              AND julianday(expires_at) <= julianday(?)
            """,
            (now,),
        ).fetchall()
        for lease in rows:
            cls._release_lease(connection, lease, reason="expired", now=now)
        return len(rows)

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
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
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


def _job_state_for_outcome(outcome: str) -> str:
    if outcome == "cancelled":
        return "cancelled"
    if outcome in {"infrastructure_failure", "declined"}:
        return "failed"
    return "completed"
