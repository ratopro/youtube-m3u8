import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_DIR = Path("data")
DB_PATH = DB_DIR / "app.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS epg_channels (
                    provider_id TEXT NOT NULL,
                    xmltv_id    TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    icon        TEXT,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (provider_id, xmltv_id)
                );

                CREATE TABLE IF NOT EXISTS epg_programmes (
                    provider_id TEXT NOT NULL,
                    xmltv_id    TEXT NOT NULL,
                    start       TEXT NOT NULL,
                    stop        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    description TEXT,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (provider_id, xmltv_id, start, stop)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS epg_programmes_channel_start
                    ON epg_programmes (provider_id, xmltv_id, start);

                CREATE INDEX IF NOT EXISTS epg_programmes_range
                    ON epg_programmes (provider_id, start, stop);
                """
            )
            conn.commit()
        finally:
            conn.close()


def upsert_epg(provider_id: str, data: dict) -> dict:
    channels = data.get("channels", {}) or {}
    programmes = data.get("programmes", {}) or {}
    now = datetime.utcnow().isoformat()
    channels_count = 0
    programmes_count = 0
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM epg_channels WHERE provider_id = ?",
                (provider_id,),
            )
            conn.execute(
                "DELETE FROM epg_programmes WHERE provider_id = ?",
                (provider_id,),
            )
            channel_rows = [
                (
                    provider_id,
                    cid,
                    (meta.get("name") or cid).strip() if isinstance(meta, dict) else cid,
                    meta.get("icon") if isinstance(meta, dict) else "",
                    now,
                )
                for cid, meta in channels.items()
            ]
            if channel_rows:
                conn.executemany(
                    "INSERT INTO epg_channels (provider_id, xmltv_id, name, icon, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    channel_rows,
                )
                channels_count = len(channel_rows)

            programme_rows = []
            seen_keys = set()
            for cid, items in programmes.items():
                if not items:
                    continue
                for item in items:
                    start = item.get("start", "")
                    stop = item.get("stop", "") or item.get("end", "")
                    key = (cid, start, stop)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    programme_rows.append(
                        (
                            provider_id,
                            cid,
                            start,
                            stop,
                            item.get("title") or "Programa",
                            item.get("description") or "",
                            now,
                        )
                    )
            if programme_rows:
                conn.executemany(
                    "INSERT INTO epg_programmes (provider_id, xmltv_id, start, stop, title, description, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    programme_rows,
                )
                programmes_count = len(programme_rows)
            conn.commit()
        finally:
            conn.close()
    return {"channels": channels_count, "programmes": programmes_count}


def channel_count(provider_id: str) -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM epg_channels WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return int(row["c"] or 0) if row else 0
        finally:
            conn.close()


def programme_count(provider_id: str) -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM epg_programmes WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return int(row["c"] or 0) if row else 0
        finally:
            conn.close()


def list_channels(provider_id: str) -> dict[str, dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT xmltv_id, name, icon FROM epg_channels WHERE provider_id = ?",
                (provider_id,),
            ).fetchall()
            return {row["xmltv_id"]: {"id": row["xmltv_id"], "name": row["name"], "icon": row["icon"] or ""} for row in rows}
        finally:
            conn.close()


def programmes_for_channel(provider_id: str, xmltv_id: str) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT start, stop, title, description FROM epg_programmes "
                "WHERE provider_id = ? AND xmltv_id = ? ORDER BY start",
                (provider_id, xmltv_id),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def channels_with_programmes(provider_id: str, xmltv_ids: set[str] | None = None) -> dict[str, list[dict]]:
    with _lock:
        conn = _connect()
        try:
            if xmltv_ids:
                placeholders = ",".join("?" for _ in xmltv_ids)
                rows = conn.execute(
                    f"SELECT xmltv_id, start, stop, title, description FROM epg_programmes "
                    f"WHERE provider_id = ? AND xmltv_id IN ({placeholders}) ORDER BY xmltv_id, start",
                    (provider_id, *xmltv_ids),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT xmltv_id, start, stop, title, description FROM epg_programmes "
                    "WHERE provider_id = ? ORDER BY xmltv_id, start",
                    (provider_id,),
                ).fetchall()
            out: dict[str, list[dict]] = {}
            for row in rows:
                out.setdefault(row["xmltv_id"], []).append(dict(row))
            return out
        finally:
            conn.close()
