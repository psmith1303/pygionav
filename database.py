"""
Local SQLite database for PyGioNav.

Tables:
  recordings  - Cached album metadata from Navidrome
  plays       - Log of every album play
"""

import logging
import os
import sqlite3
from typing import Dict, List, Optional

from subsonic_client import Album

log = logging.getLogger("pygionav.db")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


class PyGioNavDatabase:
    """Manages the local play-tracking and library-cache database."""

    def __init__(self, conf_dir: str, db_name: str = "music"):
        os.makedirs(conf_dir, exist_ok=True)
        self.db_path = os.path.join(conf_dir, f"{db_name}.db")
        log.debug("Database path: %s", self.db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self.db_path)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_schema(self):
        conn = _connect(self.db_path)
        try:
            # Phase 1: create tables (library_id included for fresh installs)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    album_id    TEXT UNIQUE NOT NULL,
                    artist      TEXT NOT NULL DEFAULT '',
                    album_name  TEXT NOT NULL DEFAULT '',
                    genre       TEXT NOT NULL DEFAULT '',
                    year        INTEGER NOT NULL DEFAULT 0,
                    duration    INTEGER NOT NULL DEFAULT 0,
                    song_count  INTEGER NOT NULL DEFAULT 0,
                    cover_art   TEXT NOT NULL DEFAULT '',
                    artist_id   TEXT NOT NULL DEFAULT '',
                    library_id  TEXT NOT NULL DEFAULT '',
                    performer   TEXT NOT NULL DEFAULT '',
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );

                CREATE TABLE IF NOT EXISTS plays (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    play_date   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    album_id    TEXT NOT NULL,
                    artist      TEXT NOT NULL DEFAULT '',
                    album_name  TEXT NOT NULL DEFAULT '',
                    genre       TEXT NOT NULL DEFAULT '',
                    duration    INTEGER NOT NULL DEFAULT 0,
                    performer   TEXT NOT NULL DEFAULT ''
                );
            """)

            # Phase 2: migrate old databases that lack newer columns
            cols = {r[1] for r in conn.execute("PRAGMA table_info(recordings)")}
            if "library_id" not in cols:
                conn.execute(
                    "ALTER TABLE recordings ADD COLUMN library_id TEXT NOT NULL DEFAULT ''")
                log.debug("Migrated: added library_id column to recordings")
            if "performer" not in cols:
                conn.execute(
                    "ALTER TABLE recordings ADD COLUMN performer TEXT NOT NULL DEFAULT ''")
                log.debug("Migrated: added performer column to recordings")

            # Phase 2b: migrate plays.duration from TEXT to INTEGER
            # SQLite doesn't support ALTER COLUMN, so we rebuild the table.
            play_cols = conn.execute("PRAGMA table_info(plays)").fetchall()
            dur_col = [c for c in play_cols if c[1] == "duration"]
            if dur_col and dur_col[0][2].upper() == "TEXT":
                conn.executescript("""
                    ALTER TABLE plays RENAME TO plays_old;
                    CREATE TABLE plays (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        play_date   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                        album_id    TEXT NOT NULL,
                        artist      TEXT NOT NULL DEFAULT '',
                        album_name  TEXT NOT NULL DEFAULT '',
                        genre       TEXT NOT NULL DEFAULT '',
                        duration    INTEGER NOT NULL DEFAULT 0,
                        performer   TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO plays (id, play_date, album_id, artist,
                                       album_name, genre, duration, performer)
                        SELECT id,
                               coalesce(play_date, datetime('now','localtime')),
                               album_id, artist,
                               album_name, genre, 0, performer
                        FROM plays_old;
                    DROP TABLE plays_old;
                """)
                log.debug("Migrated: plays.duration TEXT → INTEGER")

            # Phase 3: create indexes (safe now that all columns exist)
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_rec_artist   ON recordings (artist);
                CREATE INDEX IF NOT EXISTS idx_rec_genre    ON recordings (genre);
                CREATE INDEX IF NOT EXISTS idx_rec_album_id ON recordings (album_id);
                CREATE INDEX IF NOT EXISTS idx_rec_library  ON recordings (library_id);
                CREATE INDEX IF NOT EXISTS idx_play_album   ON plays (album_id);
                CREATE INDEX IF NOT EXISTS idx_play_artist  ON plays (artist);
                CREATE INDEX IF NOT EXISTS idx_play_date    ON plays (play_date);
            """)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Library cache
    # ------------------------------------------------------------------ #

    def upsert_album(self, album: Album, library_id: str = ""):
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO recordings
                (album_id, artist, album_name, genre, year, duration,
                 song_count, cover_art, artist_id, library_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(album_id) DO UPDATE SET
                artist     = excluded.artist,
                album_name = excluded.album_name,
                genre      = excluded.genre,
                year       = excluded.year,
                duration   = excluded.duration,
                song_count = excluded.song_count,
                cover_art  = excluded.cover_art,
                artist_id  = excluded.artist_id,
                library_id = excluded.library_id,
                updated_at = excluded.updated_at
        """, (album.id, album.artist, album.name, album.genre,
              album.year, album.duration, album.song_count,
              album.cover_art, album.artist_id, library_id))
        conn.commit()

    def get_recording_count(self, library_id: Optional[str] = None) -> int:
        conn = self._get_conn()
        if library_id:
            row = conn.execute(
                "SELECT count(*) FROM recordings WHERE library_id=?",
                (library_id,)).fetchone()
        else:
            row = conn.execute("SELECT count(*) FROM recordings").fetchone()
        return row[0]

    def get_random_album(self, filters: Optional[Dict] = None) -> Optional[Dict]:
        """Select a random album matching filters.

        Supported filter keys:
          artist, genre, album         - substring match (append @ to negate)
          min_duration, max_duration   - seconds
          excludes                     - list of uppercased artist names
          time_bar_hours               - int
          unplayed_artist              - bool
          unplayed_works               - bool
          library_id                   - restrict to this library
        """
        if filters is None:
            filters = {}

        clauses = ["1=1"]
        params: list = []

        for col, fkey in (("artist", "artist"), ("genre", "genre"),
                          ("album_name", "album"), ("performer", "performer")):
            val = filters.get(fkey)
            if val:
                if val.endswith("@"):
                    clauses.append(f"upper({col}) NOT LIKE ?")
                    params.append(f"%{val[:-1].upper()}%")
                else:
                    clauses.append(f"upper({col}) LIKE ?")
                    params.append(f"%{val.upper()}%")

        if filters.get("min_duration", 0) > 0:
            clauses.append("duration >= ?")
            params.append(filters["min_duration"])
        if filters.get("max_duration", 0) > 0:
            clauses.append("duration <= ?")
            params.append(filters["max_duration"])

        excludes = filters.get("excludes", [])
        if excludes:
            ph = ",".join(["?"] * len(excludes))
            clauses.append(f"upper(artist) NOT IN ({ph})")
            params.extend(excludes)

        tb = filters.get("time_bar_hours", 0)
        if tb > 0:
            clauses.append("""
                artist NOT IN (
                    SELECT DISTINCT artist FROM plays
                    WHERE play_date > datetime('now','localtime', ?)
                )""")
            params.append(f"-{tb} hours")

        if filters.get("unplayed_artist"):
            clauses.append("artist NOT IN (SELECT DISTINCT artist FROM plays)")

        if filters.get("unplayed_works"):
            clauses.append("album_id NOT IN (SELECT DISTINCT album_id FROM plays)")

        lib = filters.get("library_id")
        if lib:
            clauses.append("library_id = ?")
            params.append(lib)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM recordings WHERE {where} ORDER BY RANDOM() LIMIT 1"
        log.debug("Random album SQL: %s  params=%s", sql, params)

        conn = self._get_conn()
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Play tracking
    # ------------------------------------------------------------------ #

    def record_play(self, album_id: str, artist: str, album_name: str,
                    genre: str, duration: int, performer: str = ""):
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO plays (play_date, album_id, artist, album_name,
                               genre, duration, performer)
            VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?)
        """, (album_id, artist, album_name, genre, duration, performer))
        conn.commit()
        log.debug("Recorded play: %s — %s", artist, album_name)

    def get_play_count(self) -> int:
        return self._get_conn().execute("SELECT count(*) FROM plays").fetchone()[0]

    def get_unplayed_count(self, library_id: Optional[str] = None) -> int:
        conn = self._get_conn()
        base = "SELECT count(*) FROM recordings WHERE album_id NOT IN (SELECT DISTINCT album_id FROM plays)"
        if library_id:
            base += " AND library_id = ?"
            return conn.execute(base, (library_id,)).fetchone()[0]
        return conn.execute(base).fetchone()[0]

    def get_stats(self, library_id: Optional[str] = None) -> Dict:
        conn = self._get_conn()
        lib_clause = ""
        lib_params: tuple = ()
        if library_id:
            lib_clause = " WHERE library_id = ?"
            lib_params = (library_id,)

        rec = conn.execute(f"SELECT count(*) FROM recordings{lib_clause}",
                           lib_params).fetchone()[0]
        artists = conn.execute(
            f"SELECT count(DISTINCT artist) FROM recordings{lib_clause}",
            lib_params).fetchone()[0]
        total_dur = conn.execute(
            f"SELECT coalesce(sum(duration),0) FROM recordings{lib_clause}",
            lib_params).fetchone()[0]
        plays = conn.execute("SELECT count(*) FROM plays").fetchone()[0]
        unplayed = self.get_unplayed_count(library_id)
        return {
            "recordings": rec,
            "plays": plays,
            "unique_artists": artists,
            "unplayed": unplayed,
            "total_duration_days": round(total_dur / 86400, 2) if total_dur else 0,
        }

    def get_recent_plays(self, limit: int = 20) -> List[Dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM plays ORDER BY play_date DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_recordings(self, library_id: Optional[str] = None):
        conn = self._get_conn()
        if library_id:
            conn.execute("DELETE FROM recordings WHERE library_id=?",
                         (library_id,))
        else:
            conn.execute("DELETE FROM recordings")
        conn.commit()
