from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

VALID_STATUSES = {
    "queued",
    "validating",
    "ready",
    "posting",
    "posted",
    "failed",
}


@dataclass(slots=True)
class PostRecord:
    id: int
    account_name: str
    video_url: str
    caption: str
    hashtags: str
    scheduled_date: str
    status: str
    created_at: str
    posted_at: str | None
    instagram_creation_id: str | None
    instagram_media_id: str | None
    instagram_permalink: str | None
    error_message: str | None


class QueueStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    hashtags TEXT NOT NULL DEFAULT '',
                    scheduled_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    posted_at TEXT,
                    instagram_creation_id TEXT,
                    instagram_media_id TEXT,
                    instagram_permalink TEXT,
                    error_message TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_account_schedule_status "
                "ON posts(account_name, scheduled_date, status)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
            conn.commit()

    def add_post(
        self,
        *,
        account_name: str,
        video_url: str,
        caption: str,
        hashtags: str,
        scheduled_date: str,
        status: str = "queued",
    ) -> PostRecord:
        self._validate_status(status)
        now = _utc_now()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO posts (
                    account_name,
                    video_url,
                    caption,
                    hashtags,
                    scheduled_date,
                    status,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_name,
                    video_url.strip(),
                    caption.strip(),
                    hashtags.strip(),
                    scheduled_date,
                    status,
                    now,
                ),
            )
            conn.commit()
            post_id = int(cursor.lastrowid)
        return self.get_post(post_id)

    def get_post(self, post_id: int) -> PostRecord:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise KeyError(f"Post {post_id} was not found.")
        return _row_to_post(row)

    def get_next_post(
        self,
        account_name: str,
        *,
        on_or_before: str,
        statuses: tuple[str, ...] = ("queued", "ready"),
    ) -> PostRecord | None:
        if not statuses:
            raise ValueError("At least one queue status must be provided.")
        for status in statuses:
            self._validate_status(status)

        placeholders = ", ".join("?" for _ in statuses)
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM posts
                WHERE account_name = ?
                  AND scheduled_date <= ?
                  AND status IN ({placeholders})
                ORDER BY scheduled_date ASC, created_at ASC, id ASC
                LIMIT 1
                """,
                (account_name, on_or_before, *statuses),
            ).fetchone()
        return _row_to_post(row) if row else None

    def list_posts(self, *, limit: int = 20, account_name: str | None = None) -> list[PostRecord]:
        query = "SELECT * FROM posts"
        params: tuple[Any, ...]
        if account_name:
            query += " WHERE account_name = ?"
            params = (account_name,)
        else:
            params = ()
        query += " ORDER BY scheduled_date DESC, created_at DESC, id DESC LIMIT ?"
        params += (limit,)

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_post(row) for row in rows]

    def update_post(self, post_id: int, **fields: Any) -> PostRecord:
        if not fields:
            return self.get_post(post_id)

        if "status" in fields:
            self._validate_status(str(fields["status"]))

        allowed = set(PostRecord.__dataclass_fields__) - {"id", "created_at"}
        if fields.keys() - allowed:
            raise ValueError("Queue update contains an unsupported field.")
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [post_id]
        with self.connection() as conn:
            conn.execute(f"UPDATE posts SET {columns} WHERE id = ?", values)
            conn.commit()
        return self.get_post(post_id)

    def set_status(
        self,
        post_id: int,
        *,
        status: str,
        error_message: str | None = None,
    ) -> PostRecord:
        payload: dict[str, Any] = {"status": status, "error_message": error_message}
        if status == "posted":
            payload["posted_at"] = _utc_now()
        return self.update_post(post_id, **payload)

    def mark_failed(self, post_id: int, message: str) -> PostRecord:
        return self.set_status(post_id, status="failed", error_message=message.strip())

    def mark_posted(
        self,
        post_id: int,
        *,
        instagram_creation_id: str | None,
        instagram_media_id: str,
        instagram_permalink: str | None,
    ) -> PostRecord:
        return self.update_post(
            post_id,
            status="posted",
            posted_at=_utc_now(),
            instagram_creation_id=instagram_creation_id,
            instagram_media_id=instagram_media_id,
            instagram_permalink=instagram_permalink,
            error_message=None,
        )

    def count_posts_for_day_number(self, account_name: str) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM posts
                WHERE account_name = ?
                  AND status IN ('queued', 'validating', 'ready', 'posting', 'posted')
                """,
                (account_name,),
            ).fetchone()
        return int(row["count"])

    def count_posts_by_status(
        self,
        account_name: str,
        *,
        statuses: tuple[str, ...],
    ) -> int:
        if not statuses:
            return 0
        for status in statuses:
            self._validate_status(status)

        placeholders = ", ".join("?" for _ in statuses)
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM posts
                WHERE account_name = ?
                  AND status IN ({placeholders})
                """,
                (account_name, *statuses),
            ).fetchone()
        return int(row["count"])

    def scheduled_date_exists(self, account_name: str, scheduled_date: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM posts
                WHERE account_name = ?
                  AND scheduled_date = ?
                LIMIT 1
                """,
                (account_name, scheduled_date),
            ).fetchone()
        return row is not None

    def latest_scheduled_date(
        self,
        account_name: str,
        *,
        statuses: tuple[str, ...] | None = None,
        scheduled_after: str | None = None,
    ) -> str | None:
        params: list[Any] = [account_name]
        query = """
            SELECT scheduled_date
            FROM posts
            WHERE account_name = ?
        """

        if statuses:
            for status in statuses:
                self._validate_status(status)
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)

        if scheduled_after is not None:
            query += " AND scheduled_date > ?"
            params.append(scheduled_after)

        query += " ORDER BY scheduled_date DESC, id DESC LIMIT 1"

        with self.connection() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return str(row["scheduled_date"]) if row else None

    def upcoming_posts(
        self,
        account_name: str,
        *,
        limit: int = 10,
        scheduled_on_or_after: str | None = None,
        statuses: tuple[str, ...] = ("queued", "ready"),
    ) -> list[PostRecord]:
        if not statuses:
            return []
        for status in statuses:
            self._validate_status(status)

        placeholders = ", ".join("?" for _ in statuses)
        params: list[Any] = [account_name, *statuses]
        query = f"""
            SELECT *
            FROM posts
            WHERE account_name = ?
              AND status IN ({placeholders})
        """
        if scheduled_on_or_after is not None:
            query += " AND scheduled_date >= ?"
            params.append(scheduled_on_or_after)

        query += " ORDER BY scheduled_date ASC, created_at ASC, id ASC LIMIT ?"
        params.append(limit)

        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_row_to_post(row) for row in rows]

    def recent_posted_posts(self, account_name: str, *, limit: int = 10) -> list[PostRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM posts
                WHERE account_name = ?
                  AND status = 'posted'
                ORDER BY posted_at DESC, id DESC
                LIMIT ?
                """,
                (account_name, limit),
            ).fetchall()
        return [_row_to_post(row) for row in rows]

    def has_posted_between(
        self,
        account_name: str,
        *,
        start_posted_at: str,
        end_posted_at: str,
    ) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM posts
                WHERE account_name = ?
                  AND status = 'posted'
                  AND posted_at >= ?
                  AND posted_at < ?
                LIMIT 1
                """,
                (account_name, start_posted_at, end_posted_at),
            ).fetchone()
        return row is not None

    def status_counts(self, *, account_name: str | None = None) -> dict[str, int]:
        query = """
            SELECT status, COUNT(*) AS total
            FROM posts
        """
        params: tuple[Any, ...]
        if account_name:
            query += " WHERE account_name = ?"
            params = (account_name,)
        else:
            params = ()
        query += " GROUP BY status ORDER BY status ASC"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def account_status_counts(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT account_name, status, COUNT(*) AS total
                FROM posts
                GROUP BY account_name, status
                ORDER BY account_name ASC, status ASC
                """
            ).fetchall()
        return [
            {
                "account_name": str(row["account_name"]),
                "status": str(row["status"]),
                "total": int(row["total"]),
            }
            for row in rows
        ]

    def recent_failures(
        self, *, account_name: str | None = None, limit: int = 5
    ) -> list[PostRecord]:
        query = "SELECT * FROM posts WHERE status = 'failed'"
        params: tuple[Any, ...]
        if account_name:
            query += " AND account_name = ?"
            params = (account_name,)
        else:
            params = ()
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params += (limit,)

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_post(row) for row in rows]

    def recent_successes(
        self, *, account_name: str | None = None, limit: int = 5
    ) -> list[PostRecord]:
        query = "SELECT * FROM posts WHERE status = 'posted'"
        params: tuple[Any, ...]
        if account_name:
            query += " AND account_name = ?"
            params = (account_name,)
        else:
            params = ()
        query += " ORDER BY posted_at DESC, id DESC LIMIT ?"
        params += (limit,)

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_post(row) for row in rows]

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid queue status '{status}'. Expected one of {sorted(VALID_STATUSES)}."
            )


def _row_to_post(row: sqlite3.Row | None) -> PostRecord:
    if row is None:
        raise ValueError("Cannot convert an empty database row into a post.")
    return PostRecord(
        id=int(row["id"]),
        account_name=str(row["account_name"]),
        video_url=str(row["video_url"]),
        caption=str(row["caption"] or ""),
        hashtags=str(row["hashtags"] or ""),
        scheduled_date=str(row["scheduled_date"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        posted_at=str(row["posted_at"]) if row["posted_at"] else None,
        instagram_creation_id=str(row["instagram_creation_id"])
        if row["instagram_creation_id"]
        else None,
        instagram_media_id=str(row["instagram_media_id"]) if row["instagram_media_id"] else None,
        instagram_permalink=str(row["instagram_permalink"]) if row["instagram_permalink"] else None,
        error_message=str(row["error_message"]) if row["error_message"] else None,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
