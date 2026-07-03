"""SQLite database for persisting scored/enriched items and daily runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from ..models import ContentItem


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    content         TEXT,
    author          TEXT,
    published_at    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    ai_relevant     INTEGER,
    ai_score        REAL,
    ai_reason       TEXT,
    ai_summary      TEXT,
    ai_tags_json    TEXT NOT NULL DEFAULT '[]',
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    run_date        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_run_date ON items(run_date);
CREATE INDEX IF NOT EXISTS idx_items_ai_score ON items(ai_score);
CREATE INDEX IF NOT EXISTS idx_items_source_type ON items(source_type);
CREATE INDEX IF NOT EXISTS idx_items_published_at ON items(published_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, ai_summary, ai_reason, ai_tags_json,
    content='items', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, ai_summary, ai_reason, ai_tags_json)
    VALUES (new.rowid, new.title, new.ai_summary, new.ai_reason, new.ai_tags_json);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, ai_summary, ai_reason, ai_tags_json)
    VALUES ('delete', old.rowid, old.title, old.ai_summary, old.ai_reason, old.ai_tags_json);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, ai_summary, ai_reason, ai_tags_json)
    VALUES ('delete', old.rowid, old.title, old.ai_summary, old.ai_reason, old.ai_tags_json);
    INSERT INTO items_fts(rowid, title, ai_summary, ai_reason, ai_tags_json)
    VALUES (new.rowid, new.title, new.ai_summary, new.ai_reason, new.ai_tags_json);
END;

CREATE TABLE IF NOT EXISTS daily_runs (
    date            TEXT PRIMARY KEY,
    total_fetched   INTEGER NOT NULL DEFAULT 0,
    total_selected  INTEGER NOT NULL DEFAULT 0,
    languages       TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a DB row to a JSON-serializable dict matching the ContentItem shape."""
    return {
        "id": row["id"],
        "source_type": row["source_type"],
        "title": row["title"],
        "url": row["url"],
        "content": row["content"],
        "author": row["author"],
        "published_at": row["published_at"],
        "fetched_at": row["fetched_at"],
        "ai_relevant": bool(row["ai_relevant"]) if row["ai_relevant"] is not None else None,
        "ai_score": row["ai_score"],
        "ai_reason": row["ai_reason"],
        "ai_summary": row["ai_summary"],
        "ai_tags": json.loads(row["ai_tags_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "run_date": row["run_date"],
    }


class HorizonDB:
    """SQLite persistence for Horizon pipeline outputs."""

    def __init__(self, db_path: str = "data/horizon.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- write ----------------------------------------------------------------

    def save_items(self, items: List[ContentItem], run_date: str, total_fetched: int) -> int:
        """Persist scored/enriched items for a given date.

        Replaces any existing items for the same run_date (idempotent).
        """
        # Delete existing items for this date first
        self.conn.execute("DELETE FROM items WHERE run_date = ?", (run_date,))

        rows: list[tuple] = []
        for item in items:
            rows.append((
                item.id,
                item.source_type.value,
                item.title,
                str(item.url),
                item.content,
                item.author,
                _dt_iso(item.published_at),
                _dt_iso(item.fetched_at),
                1 if item.ai_relevant else 0,
                item.ai_score,
                item.ai_reason,
                item.ai_summary,
                json.dumps(item.ai_tags, ensure_ascii=False),
                json.dumps(item.metadata, ensure_ascii=False, default=str),
                run_date,
                _now_iso(),
            ))

        self.conn.executemany(
            """INSERT INTO items (
                id, source_type, title, url, content, author,
                published_at, fetched_at, ai_relevant, ai_score,
                ai_reason, ai_summary, ai_tags_json, metadata_json,
                run_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

        # Upsert daily run record
        languages = list({item.metadata.get("language", "unknown") for item in items}) if items else []
        self.conn.execute(
            """INSERT INTO daily_runs (date, total_fetched, total_selected, languages)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               total_fetched = excluded.total_fetched,
               total_selected = excluded.total_selected,
               languages = excluded.languages""",
            (run_date, total_fetched, len(items), json.dumps(languages)),
        )

        self.conn.commit()
        return len(items)

    # -- read ----------------------------------------------------------------

    def get_items(
        self,
        *,
        run_date: Optional[str] = None,
        category: Optional[str] = None,
        tag: Optional[str] = None,
        source_type: Optional[str] = None,
        search: Optional[str] = None,
        min_score: Optional[float] = None,
        sort: str = "ai_score",
        order: str = "desc",
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Paginated item query with optional filters."""
        where = []
        params: list[Any] = []

        if run_date:
            where.append("run_date = ?")
            params.append(run_date)

        if category:
            where.append("json_extract(metadata_json, '$.category') = ?")
            params.append(category)

        if source_type:
            where.append("source_type = ?")
            params.append(source_type)

        if min_score is not None:
            where.append("ai_score >= ?")
            params.append(min_score)

        where_clause = " AND ".join(where) if where else "1=1"

        # Build the base query
        if search:
            # FTS5 search — join on rowid
            base_from = (
                "FROM items JOIN items_fts ON items.rowid = items_fts.rowid "
                f"WHERE items_fts MATCH ? AND {where_clause}"
            )
            params.insert(0, search)
        elif tag:
            # Tag filter via JSON array containment
            tag_clause = "EXISTS (SELECT 1 FROM json_each(ai_tags_json) WHERE value = ?)"
            base_from = f"FROM items WHERE {tag_clause} AND {where_clause}"
            params.insert(0, tag)
        else:
            base_from = f"FROM items WHERE {where_clause}"

        # Count
        count_row = self.conn.execute(
            f"SELECT COUNT(*) as cnt {base_from}", params
        ).fetchone()
        total = count_row["cnt"] if count_row else 0

        # Fetch page
        allowed_sort = {"ai_score", "published_at", "created_at", "run_date"}
        sort_col = sort if sort in allowed_sort else "ai_score"
        order_dir = "DESC" if order.lower() == "desc" else "ASC"
        offset = (page - 1) * per_page

        rows = self.conn.execute(
            f"SELECT * {base_from} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

        return {
            "items": [_row_to_item(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    def get_item(self, item_id: str) -> Optional[dict[str, Any]]:
        """Get a single item by ID."""
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def get_tags(self, run_date: Optional[str] = None, min_count: int = 1) -> list[dict[str, Any]]:
        """Get all tags with occurrence counts."""
        where = "1=1"
        params: list[Any] = []
        if run_date:
            where = "run_date = ?"
            params.append(run_date)

        rows = self.conn.execute(
            f"""SELECT value AS tag, COUNT(*) AS count
                FROM items, json_each(ai_tags_json)
                WHERE {where}
                GROUP BY value
                HAVING COUNT(*) >= ?
                ORDER BY count DESC""",
            params + [min_count],
        ).fetchall()
        return [{"tag": r["tag"], "count": r["count"]} for r in rows]

    def get_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Get recent daily runs."""
        rows = self.conn.execute(
            "SELECT * FROM daily_runs ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "date": r["date"],
                "total_fetched": r["total_fetched"],
                "total_selected": r["total_selected"],
                "languages": json.loads(r["languages"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_run_dates(self, limit: int = 30) -> list[str]:
        """Get list of dates that have data."""
        rows = self.conn.execute(
            "SELECT DISTINCT run_date FROM items ORDER BY run_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["run_date"] for r in rows]

    def get_category_counts(self, run_date: Optional[str] = None) -> list[dict[str, Any]]:
        """Get item counts by category."""
        where = "1=1"
        params: list[Any] = []
        if run_date:
            where = "run_date = ?"
            params.append(run_date)

        rows = self.conn.execute(
            f"""SELECT json_extract(metadata_json, '$.category') AS category,
                       COUNT(*) AS count
                FROM items
                WHERE {where}
                GROUP BY category
                ORDER BY count DESC""",
            params,
        ).fetchall()
        return [{"category": r["category"] or "unknown", "count": r["count"]} for r in rows]

    def get_stats(self, run_date: Optional[str] = None) -> dict[str, Any]:
        """Get aggregate statistics."""
        where = "1=1"
        params: list[Any] = []
        if run_date:
            where = "run_date = ?"
            params.append(run_date)

        row = self.conn.execute(
            f"""SELECT
                    COUNT(*) AS total_items,
                    AVG(ai_score) AS avg_score,
                    MAX(ai_score) AS max_score,
                    COUNT(DISTINCT source_type) AS source_types
                FROM items WHERE {where}""",
            params,
        ).fetchone()

        return {
            "total_items": row["total_items"],
            "avg_score": round(row["avg_score"], 2) if row["avg_score"] else None,
            "max_score": row["max_score"],
            "source_types": row["source_types"],
        }

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search across title, summary, reason, and tags."""
        rows = self.conn.execute(
            """SELECT items.* FROM items
               JOIN items_fts ON items.rowid = items_fts.rowid
               WHERE items_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [_row_to_item(r) for r in rows]
