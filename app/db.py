"""SQLite persistence for calibration files, deactivation events and orders."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT UNIQUE,
    kind             TEXT NOT NULL,
    detector         TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    exposure_key     TEXT,
    exposure_value   TEXT,
    filter_name      TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    deactivated_at   TEXT,
    deactivate_reason TEXT
);

CREATE TABLE IF NOT EXISTS file_status_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES calibration_files(id),
    action      TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_status_events_file ON file_status_events(file_id);

CREATE TABLE IF NOT EXISTS orders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no               TEXT UNIQUE NOT NULL,
    status                 TEXT NOT NULL,
    request_json           TEXT NOT NULL,
    images_json            TEXT NOT NULL,
    candidate_ids_json     TEXT NOT NULL,
    plan_json              TEXT,
    issued_manifest_json   TEXT,
    total_bytes            INTEGER,
    missing_json           TEXT,
    recomputed_at_publish  INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    computed_at            TEXT,
    published_at           TEXT
);

CREATE TABLE IF NOT EXISTS order_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    event_type  TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id);
"""


class Database:
    """Thin wrapper around a file-based SQLite database.

    A process-wide re-entrant lock serialises the check -> recompute -> publish
    critical section against (de)activations.  Every method opens a short-lived
    connection, so FastAPI's thread pool can use the database safely.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self.lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    # ---------------- calibration files ----------------

    def insert_calibration_file(self, data: dict[str, Any], now: str) -> sqlite3.Row:
        with self.lock, self.connect() as conn:
            explicit_id = data.get("explicit_id")
            if explicit_id is not None:
                conn.execute(
                    """
                    INSERT INTO calibration_files
                        (id, code, kind, detector, batch_id, size_bytes,
                         exposure_key, exposure_value, filter_name,
                         is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        explicit_id, data.get("code"), data["kind"], data["detector"],
                        data["batch_id"], int(data["size_bytes"]),
                        data.get("exposure_key"), data.get("exposure_value"),
                        data.get("filter_name"), now,
                    ),
                )
                file_id = explicit_id
            else:
                cur = conn.execute(
                    """
                    INSERT INTO calibration_files
                        (code, kind, detector, batch_id, size_bytes,
                         exposure_key, exposure_value, filter_name,
                         is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        data.get("code"), data["kind"], data["detector"],
                        data["batch_id"], int(data["size_bytes"]),
                        data.get("exposure_key"), data.get("exposure_value"),
                        data.get("filter_name"), now,
                    ),
                )
                file_id = cur.lastrowid
            conn.execute(
                "INSERT INTO file_status_events (file_id, action, reason, created_at)"
                " VALUES (?, 'registered', NULL, ?)",
                (file_id, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM calibration_files WHERE id = ?", (file_id,)
            ).fetchone()
            return row

    def get_file_by_pk(self, file_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM calibration_files WHERE id = ?", (file_id,)
            ).fetchone()

    def get_file_by_code(self, code: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM calibration_files WHERE code = ?", (code,)
            ).fetchone()

    def list_files(
        self,
        active: bool | None = None,
        kind: str | None = None,
        detector: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM calibration_files WHERE 1 = 1"
        params: list[Any] = []
        if active is True:
            sql += " AND is_active = 1"
        elif active is False:
            sql += " AND is_active = 0"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if detector:
            sql += " AND detector = ?"
            params.append(detector)
        sql += " ORDER BY id"
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def set_active(
        self, file_id: int, active: bool, action: str, reason: str | None, now: str
    ) -> tuple[sqlite3.Row | None, bool]:
        """Flip active flag. Returns (row, changed)."""
        with self.lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM calibration_files WHERE id = ?", (file_id,)
            ).fetchone()
            if row is None:
                return None, False
            changed = bool(row["is_active"]) != active
            if changed:
                if active:
                    conn.execute(
                        "UPDATE calibration_files SET is_active = 1,"
                        " deactivated_at = NULL, deactivate_reason = NULL WHERE id = ?",
                        (file_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE calibration_files SET is_active = 0,"
                        " deactivated_at = ?, deactivate_reason = ? WHERE id = ?",
                        (now, reason, file_id),
                    )
            conn.execute(
                "INSERT INTO file_status_events (file_id, action, reason, created_at)"
                " VALUES (?, ?, ?, ?)",
                (file_id, action, reason, now),
            )
            conn.commit()
            return conn.execute(
                "SELECT * FROM calibration_files WHERE id = ?", (file_id,)
            ).fetchone(), changed

    def list_status_events(self, action: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT e.*, c.code AS file_code, c.kind AS file_kind "
            "FROM file_status_events e LEFT JOIN calibration_files c ON c.id = e.file_id"
        )
        params: list[Any] = []
        if action:
            sql += " WHERE e.action = ?"
            params.append(action)
        sql += " ORDER BY e.id"
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    # ---------------- orders ----------------

    def insert_order(self, now: str, **fields: Any) -> int:
        with self.lock, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO orders
                    (order_no, status, request_json, images_json,
                     candidate_ids_json, plan_json, total_bytes, missing_json,
                     created_at, computed_at)
                VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["status"], fields["request_json"], fields["images_json"],
                    fields["candidate_ids_json"], fields.get("plan_json"),
                    fields.get("total_bytes"), fields.get("missing_json"),
                    now, now,
                ),
            )
            order_id = cur.lastrowid
            order_no = f"ORD-{order_id:06d}"
            conn.execute("UPDATE orders SET order_no = ? WHERE id = ?", (order_no, order_id))
            conn.commit()
            return order_id

    def get_order(self, identifier: str | int) -> sqlite3.Row | None:
        with self.connect() as conn:
            if isinstance(identifier, int):
                return conn.execute("SELECT * FROM orders WHERE id = ?", (identifier,)).fetchone()
            text = str(identifier).strip()
            if text.isdigit():
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (int(text),)).fetchone()
                if row is not None:
                    return row
            row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (text,)).fetchone()
            if row is not None:
                return row
            # Tolerate lowercase / whitespace variants of the order number.
            return conn.execute(
                "SELECT * FROM orders WHERE lower(order_no) = lower(?)", (text,)
            ).fetchone()

    def list_orders(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM orders ORDER BY id").fetchall())

    def update_computation(
        self,
        order_id: int,
        status: str,
        plan_json: str | None,
        total_bytes: int | None,
        missing_json: str | None,
        now: str,
    ) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                "UPDATE orders SET status = ?, plan_json = ?, total_bytes = ?,"
                " missing_json = ?, computed_at = ? WHERE id = ?",
                (status, plan_json, total_bytes, missing_json, now, order_id),
            )
            conn.commit()

    def publish_order(
        self,
        order_id: int,
        manifest_json: str,
        total_bytes: int,
        now: str,
        recomputed: bool,
    ) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                "UPDATE orders SET status = 'published', issued_manifest_json = ?,"
                " total_bytes = ?, published_at = ?, recomputed_at_publish = ?,"
                " missing_json = '[]' WHERE id = ?",
                (manifest_json, total_bytes, now, 1 if recomputed else 0, order_id),
            )
            conn.commit()

    def add_order_event(self, order_id: int, event_type: str, detail_json: str | None, now: str) -> None:
        with self.lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO order_events (order_id, event_type, detail_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (order_id, event_type, detail_json, now),
            )
            conn.commit()

    def list_order_events(self, order_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)
            ).fetchall())
