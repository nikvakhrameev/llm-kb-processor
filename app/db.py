"""SQLite database wrapper.

Provides the foundational data layer for the entire pipeline:
- Schema migration
- Resource insertion and polling
- Atomic status transitions with CAS guard
- Retry scheduling with exponential backoff
- Event logging for audit trail
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.enums import ResourceStatus


class Database:
    """Single-file SQLite database in WAL mode with foreign keys enabled."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def connect(self) -> None:
        """Open the database connection with WAL mode and foreign keys."""
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def run_migrations(self) -> None:
        """Execute migration SQL from the migrations directory."""
        migrations_dir = Path(__file__).parent.parent / "migrations"
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            sql = migration_file.read_text()
            self.conn.executescript(sql)

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def transaction(self):
        """Context manager for explicit transactions."""
        return self.conn  # sqlite3 connection supports __enter__/__exit__

    # ------------------------------------------------------------------
    # Resource CRUD
    # ------------------------------------------------------------------

    def insert_resource(
        self,
        *,
        id: str,
        resource_type: str,
        status: str = ResourceStatus.RECEIVED,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        telegram_user_id: int | None = None,
        source_url: str | None = None,
        original_file_path: str | None = None,
        inline_text: str | None = None,
        **extra: Any,
    ) -> None:
        """Insert a new resource row and a corresponding event."""
        self.execute(
            """INSERT INTO resources (id, resource_type, status,
               telegram_chat_id, telegram_message_id, telegram_user_id,
               source_url, original_file_path, inline_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, resource_type, status,
             telegram_chat_id, telegram_message_id, telegram_user_id,
             source_url, original_file_path, inline_text),
        )
        self.conn.commit()

    def update_resource(self, resource_id: str, **fields: Any) -> None:
        """Update arbitrary columns on a resource row."""
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [resource_id]
        self.execute(
            f"UPDATE resources SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            tuple(values),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # State transitions (CAS guard)
    # ------------------------------------------------------------------

    def transition(
        self,
        resource_id: str,
        from_status: str,
        to_status: str,
        **fields: Any,
    ) -> None:
        """Atomically transition a resource between states.

        Uses a CAS guard (WHERE status = from_status) to prevent races.
        Also writes an event row for the audit log.
        Raises RuntimeError if the CAS guard fails.
        """
        columns = ["status = ?", "updated_at = datetime('now')"]
        values: list[Any] = [to_status]

        for col, val in fields.items():
            if val is not None:
                columns.append(f"{col} = ?")
                values.append(val)

        if to_status in (ResourceStatus.DONE, ResourceStatus.FAILED, ResourceStatus.REJECTED):
            columns.append("completed_at = datetime('now')")

        values.extend([resource_id, from_status])

        sql = f"""
            UPDATE resources
               SET {', '.join(columns)}
             WHERE id = ? AND status = ?
        """
        cursor = self.conn.execute(sql, values)
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Transition CAS failed: resource={resource_id} "
                f"expected status={from_status}, got different."
            )

        event_payload = {"from": from_status, "to": to_status}
        for col, val in fields.items():
            if val is not None:
                event_payload[col] = val

        self.execute(
            "INSERT INTO events (resource_id, event_type, payload) VALUES (?, 'status_change', ?)",
            (resource_id, json.dumps(event_payload)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Retry scheduling
    # ------------------------------------------------------------------

    def schedule_retry(
        self,
        resource_id: str,
        prev_status: str,
        error: str,
        *,
        max_retries: int = 3,
        backoff_base_seconds: int = 60,
    ) -> bool:
        """Schedule a retry with exponential backoff.

        Returns True if a retry was scheduled, False if retries are exhausted.
        """
        row = self.fetchone(
            "SELECT retry_count FROM resources WHERE id = ?", (resource_id,)
        )
        if row is None:
            return False

        if row["retry_count"] >= max_retries:
            return False

        delay = backoff_base_seconds * (2 ** row["retry_count"])
        self.execute(
            """UPDATE resources
               SET status = ?,
                   retry_count = retry_count + 1,
                   next_attempt_at = datetime('now', ?),
                   error_message = ?,
                   updated_at = datetime('now')
             WHERE id = ?""",
            (prev_status, f"+{delay} seconds", error, resource_id),
        )
        self.execute(
            """INSERT INTO events (resource_id, event_type, payload)
               VALUES (?, 'retry_scheduled', ?)""",
            (resource_id, json.dumps({
                "prev_status": prev_status,
                "delay_s": delay,
                "attempt": row["retry_count"] + 1,
                "error": error,
            })),
        )
        self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # Polling queries
    # ------------------------------------------------------------------

    def poll_resources(self, statuses: list[str], limit: int = 1) -> list[sqlite3.Row]:
        """Poll for resources ready for processing.

        Returns rows where status is in the given list AND next_attempt_at
        is NULL or in the past.
        """
        placeholders = ",".join("?" for _ in statuses)
        return self.fetchall(
            f"""SELECT * FROM resources
             WHERE status IN ({placeholders})
               AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
             ORDER BY created_at
             LIMIT ?""",
            tuple(statuses) + (limit,),
        )

    def find_by_short_id(self, short_id: str) -> sqlite3.Row | None:
        """Find a resource by the first 8 characters of its UUID."""
        return self.fetchone(
            "SELECT * FROM resources WHERE id LIKE ?",
            (f"{short_id}%",),
        )

    def recent_for_user(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        """Return the most recent resources for a given Telegram user."""
        return self.fetchall(
            """SELECT * FROM resources
             WHERE telegram_user_id = ?
             ORDER BY created_at DESC
             LIMIT ?""",
            (user_id, limit),
        )

    # ------------------------------------------------------------------
    # Sweeper
    # ------------------------------------------------------------------

    def reset_stuck_rows(self, stuck_minutes: int = 30) -> int:
        """Reset rows stuck in in-flight states for too long.

        Returns the number of rows reset.
        """
        cursor = self.conn.execute(
            """UPDATE resources
               SET status = CASE status
                              WHEN 'parsing'   THEN 'received'
                              WHEN 'gating'    THEN 'parsed'
                              WHEN 'ingesting' THEN 'approved'
                            END,
                   retry_count = retry_count + 1,
                   updated_at = datetime('now'),
                   error_message = COALESCE(error_message, '') || ' [sweeper:stuck]'
             WHERE status IN ('parsing', 'gating', 'ingesting')
               AND datetime(updated_at) < datetime('now', ?)""",
            (f"-{stuck_minutes} minutes",),
        )
        count = cursor.rowcount
        if count > 0:
            self.conn.commit()
        return count
