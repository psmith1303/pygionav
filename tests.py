#!/usr/bin/env python3
"""
Test suite for PyGioNav.

Run with:  python3 -m pytest tests.py -v
    or:    python3 tests.py

Covers:
  - Config loading, defaults, and edge cases
  - Subsonic client data-class parsing, URL generation, auth
  - Database schema creation, upsert, filtering, play tracking, stats
  - Display formatting helpers
  - Player audio-output detection
  - Library resolution
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from config import PyGioNavConfig, get_excludes, load_config
from subsonic_client import (
    Album, Artist, MusicFolder, Song, SubsonicClient, SubsonicError,
)
from database import PyGioNavDatabase
from display import format_duration
from player import _detect_audio_output, _audio_output_args


# ====================================================================
# Config
# ====================================================================

class TestConfigDefaults(unittest.TestCase):
    def test_defaults(self):
        cfg = PyGioNavConfig()
        self.assertEqual(cfg.server_url, "http://localhost:4533")
        self.assertEqual(cfg.username, "")
        self.assertEqual(cfg.selections, 1)
        self.assertEqual(cfg.db_name, "music")
        self.assertEqual(cfg.library, "")
        self.assertFalse(cfg.debug)
        self.assertTrue(cfg.scrobble)
        self.assertEqual(cfg.stream_format, "raw")

    def test_conf_dir_created(self):
        cfg = PyGioNavConfig()
        self.assertTrue(cfg.conf_dir.endswith("pygionav"))
        self.assertTrue(cfg.excludes_file.endswith("excludes.txt"))


class TestConfigLoad(unittest.TestCase):
    def test_load_missing_file(self):
        cfg = load_config("/nonexistent/path.conf")
        self.assertEqual(cfg.server_url, "http://localhost:4533")

    def test_load_valid_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                         delete=False) as f:
            f.write(textwrap.dedent("""\
                [pygionav]
                server_url = http://myserver:4533
                username = alice
                password = secret
                library = Classical
                selections = 5
                time_bar = 8
                scrobble = no
                debug = yes
                unplayed_artist = true
                stream_format = opus
            """))
            f.flush()
            try:
                cfg = load_config(f.name)
                self.assertEqual(cfg.server_url, "http://myserver:4533")
                self.assertEqual(cfg.username, "alice")
                self.assertEqual(cfg.password, "secret")
                self.assertEqual(cfg.library, "Classical")
                self.assertEqual(cfg.selections, 5)
                self.assertEqual(cfg.time_bar, 8)
                self.assertFalse(cfg.scrobble)
                self.assertTrue(cfg.debug)
                self.assertTrue(cfg.unplayed_artist)
                self.assertEqual(cfg.stream_format, "opus")
            finally:
                os.unlink(f.name)

    def test_load_bad_int_value(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                         delete=False) as f:
            f.write("[pygionav]\nselections = banana\n")
            f.flush()
            try:
                cfg = load_config(f.name)
                self.assertEqual(cfg.selections, 1)  # kept default
            finally:
                os.unlink(f.name)

    def test_load_no_section(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                         delete=False) as f:
            f.write("[other]\nfoo = bar\n")
            f.flush()
            try:
                cfg = load_config(f.name)
                self.assertEqual(cfg.selections, 1)
            finally:
                os.unlink(f.name)


class TestExcludes(unittest.TestCase):
    def test_empty(self):
        cfg = PyGioNavConfig()
        cfg.excludes_file = "/nonexistent"
        self.assertEqual(get_excludes(cfg), [])

    def test_with_comments(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as f:
            f.write("# A comment\nJohn Cage\n\nKarlheinz Stockhausen\n")
            f.flush()
            try:
                cfg = PyGioNavConfig()
                cfg.excludes_file = f.name
                excl = get_excludes(cfg)
                self.assertEqual(excl, ["JOHN CAGE", "KARLHEINZ STOCKHAUSEN"])
            finally:
                os.unlink(f.name)


# ====================================================================
# Subsonic data classes
# ====================================================================

class TestSongParsing(unittest.TestCase):
    def test_from_xml(self):
        xml = ('<song id="abc" title="Allegro" album="Sym 40" '
               'artist="Mozart" genre="Classical" duration="420" '
               'track="1" discNumber="2" year="1788" suffix="flac" '
               'bitRate="1411" contentType="audio/flac" '
               'coverArt="ca1" path="/music/mozart/track01.flac"/>')
        elem = ET.fromstring(xml)
        s = Song.from_xml(elem)
        self.assertEqual(s.id, "abc")
        self.assertEqual(s.title, "Allegro")
        self.assertEqual(s.artist, "Mozart")
        self.assertEqual(s.duration, 420)
        self.assertEqual(s.track, 1)
        self.assertEqual(s.disc, 2)
        self.assertEqual(s.year, 1788)
        self.assertEqual(s.suffix, "flac")
        self.assertEqual(s.bitrate, 1411)

    def test_missing_attributes(self):
        s = Song.from_xml(ET.fromstring('<song id="x"/>'))
        self.assertEqual(s.id, "x")
        self.assertEqual(s.title, "")
        self.assertEqual(s.duration, 0)


class TestAlbumParsing(unittest.TestCase):
    def test_from_xml(self):
        xml = ('<album id="a1" name="Symphony 40" artist="Mozart" '
               'artistId="ar1" genre="Classical" year="1788" '
               'duration="1800" songCount="4" coverArt="ca1"/>')
        a = Album.from_xml(ET.fromstring(xml))
        self.assertEqual(a.id, "a1")
        self.assertEqual(a.name, "Symphony 40")
        self.assertEqual(a.artist, "Mozart")
        self.assertEqual(a.duration, 1800)
        self.assertEqual(a.song_count, 4)

    def test_title_fallback(self):
        a = Album.from_xml(ET.fromstring('<album id="a2" title="Fallback"/>'))
        self.assertEqual(a.name, "Fallback")


class TestArtistParsing(unittest.TestCase):
    def test_from_xml(self):
        a = Artist.from_xml(ET.fromstring(
            '<artist id="ar1" name="Mozart" albumCount="42"/>'))
        self.assertEqual(a.id, "ar1")
        self.assertEqual(a.name, "Mozart")
        self.assertEqual(a.album_count, 42)


class TestMusicFolderParsing(unittest.TestCase):
    def test_from_xml(self):
        mf = MusicFolder.from_xml(
            ET.fromstring('<musicFolder id="1" name="Classical"/>'))
        self.assertEqual(mf.id, "1")
        self.assertEqual(mf.name, "Classical")


# ====================================================================
# Subsonic client
# ====================================================================

class TestSubsonicClientAuth(unittest.TestCase):
    def test_auth_params_structure(self):
        c = SubsonicClient("http://localhost", "user", "pass123")
        params = c._auth_params()
        self.assertEqual(params["u"], "user")
        self.assertIn("t", params)
        self.assertIn("s", params)
        self.assertEqual(params["v"], "1.16.1")
        self.assertEqual(params["c"], "pygionav")
        # Verify token = md5(password + salt)
        expected = hashlib.md5(("pass123" + params["s"]).encode()).hexdigest()
        self.assertEqual(params["t"], expected)

    def test_auth_params_different_salts(self):
        c = SubsonicClient("http://localhost", "u", "p")
        p1 = c._auth_params()
        p2 = c._auth_params()
        self.assertNotEqual(p1["s"], p2["s"])
        self.assertNotEqual(p1["t"], p2["t"])


class TestSubsonicClientURLs(unittest.TestCase):
    def setUp(self):
        self.c = SubsonicClient("http://myhost:4533", "user", "pwd")

    def test_stream_url_basic(self):
        url = self.c.stream_url("song123")
        self.assertIn("rest/stream", url)
        self.assertIn("id=song123", url)
        self.assertIn("u=user", url)

    def test_stream_url_with_format(self):
        url = self.c.stream_url("s1", fmt="mp3", max_bitrate=320)
        self.assertIn("format=mp3", url)
        self.assertIn("maxBitRate=320", url)

    def test_stream_url_raw_no_format(self):
        url = self.c.stream_url("s1", fmt="raw")
        self.assertNotIn("format=", url)

    def test_cover_art_url(self):
        url = self.c.cover_art_url("ca1", size=320)
        self.assertIn("rest/getCoverArt", url)
        self.assertIn("id=ca1", url)
        self.assertIn("size=320", url)

    def test_url_construction(self):
        self.assertEqual(self.c._url("ping"),
                         "http://myhost:4533/rest/ping")

    def test_trailing_slash_stripped(self):
        c2 = SubsonicClient("http://host:4533/", "u", "p")
        self.assertEqual(c2._url("ping"), "http://host:4533/rest/ping")


class TestSubsonicError(unittest.TestCase):
    def test_str(self):
        e = SubsonicError(40, "Wrong password")
        self.assertIn("40", str(e))
        self.assertIn("Wrong password", str(e))


# ====================================================================
# Database
# ====================================================================

class TestDatabaseSchema(unittest.TestCase):
    def test_creates_tables(self):
        with tempfile.TemporaryDirectory() as td:
            db = PyGioNavDatabase(td, "test")
            conn = sqlite3.connect(db.db_path)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertIn("recordings", tables)
            self.assertIn("plays", tables)

    def test_library_id_column_exists(self):
        with tempfile.TemporaryDirectory() as td:
            db = PyGioNavDatabase(td, "test")
            conn = sqlite3.connect(db.db_path)
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(recordings)")}
            conn.close()
            self.assertIn("library_id", cols)


class TestDatabaseUpsert(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = PyGioNavDatabase(self.td, "test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _make_album(self, **overrides):
        defaults = dict(id="a1", name="Sym 40", artist="Mozart",
                        genre="Classical", year=1788, duration=1800,
                        song_count=4, cover_art="ca1", artist_id="ar1")
        defaults.update(overrides)
        return Album(**defaults)

    def test_insert(self):
        self.db.upsert_album(self._make_album(), library_id="lib1")
        self.assertEqual(self.db.get_recording_count(), 1)
        self.assertEqual(self.db.get_recording_count("lib1"), 1)
        self.assertEqual(self.db.get_recording_count("lib2"), 0)

    def test_upsert_update(self):
        self.db.upsert_album(self._make_album(genre="Classical"))
        self.db.upsert_album(self._make_album(genre="Romantic"))
        self.assertEqual(self.db.get_recording_count(), 1)
        rec = self.db.get_random_album()
        self.assertEqual(rec["genre"], "Romantic")

    def test_multiple_albums(self):
        for i in range(10):
            self.db.upsert_album(self._make_album(id=f"a{i}", artist=f"Artist{i}"))
        self.assertEqual(self.db.get_recording_count(), 10)


class TestDatabaseFiltering(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = PyGioNavDatabase(self.td, "test")
        albums = [
            Album(id="a1", name="Sym 40", artist="Mozart",
                  genre="Classical", year=1788, duration=1800,
                  song_count=4, cover_art="", artist_id=""),
            Album(id="a2", name="Abbey Road", artist="Beatles",
                  genre="Rock", year=1969, duration=2800,
                  song_count=17, cover_art="", artist_id=""),
            Album(id="a3", name="Toccata", artist="Bach",
                  genre="Baroque", year=1708, duration=600,
                  song_count=1, cover_art="", artist_id=""),
            Album(id="a4", name="Requiem", artist="Mozart",
                  genre="Classical", year=1791, duration=3200,
                  song_count=12, cover_art="", artist_id=""),
        ]
        for a in albums:
            lib = "classical" if a.genre in ("Classical", "Baroque") else "pop"
            self.db.upsert_album(a, library_id=lib)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_no_filter(self):
        rec = self.db.get_random_album()
        self.assertIsNotNone(rec)

    def test_filter_artist(self):
        rec = self.db.get_random_album({"artist": "Beatles"})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["artist"], "Beatles")

    def test_filter_artist_negate(self):
        for _ in range(20):
            rec = self.db.get_random_album({"artist": "Mozart@"})
            if rec:
                self.assertNotEqual(rec["artist"], "Mozart")

    def test_filter_genre(self):
        rec = self.db.get_random_album({"genre": "Rock"})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["genre"], "Rock")

    def test_filter_no_match(self):
        rec = self.db.get_random_album({"genre": "Jazz"})
        self.assertIsNone(rec)

    def test_filter_album_name(self):
        rec = self.db.get_random_album({"album": "Requiem"})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["album_name"], "Requiem")

    def test_filter_min_duration(self):
        rec = self.db.get_random_album({"min_duration": 3000})
        self.assertIsNotNone(rec)
        self.assertGreaterEqual(rec["duration"], 3000)

    def test_filter_max_duration(self):
        rec = self.db.get_random_album({"max_duration": 700})
        self.assertIsNotNone(rec)
        self.assertLessEqual(rec["duration"], 700)

    def test_filter_excludes(self):
        for _ in range(30):
            rec = self.db.get_random_album({"excludes": ["MOZART", "BACH"]})
            if rec:
                self.assertEqual(rec["artist"], "Beatles")

    def test_filter_library_id(self):
        rec = self.db.get_random_album({"library_id": "pop"})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["artist"], "Beatles")

    def test_filter_library_id_classical(self):
        for _ in range(30):
            rec = self.db.get_random_album({"library_id": "classical"})
            self.assertIsNotNone(rec)
            self.assertIn(rec["genre"], ("Classical", "Baroque"))

    def test_filter_unplayed_works(self):
        self.db.record_play("a1", "Mozart", "Sym 40", "Classical", "00:30")
        self.db.record_play("a2", "Beatles", "Abbey Road", "Rock", "00:46")
        self.db.record_play("a3", "Bach", "Toccata", "Baroque", "00:10")
        self.db.record_play("a4", "Mozart", "Requiem", "Classical", "00:53")
        rec = self.db.get_random_album({"unplayed_works": True})
        self.assertIsNone(rec)  # all played

    def test_filter_unplayed_artist(self):
        self.db.record_play("a1", "Mozart", "Sym 40", "Classical", "00:30")
        # Mozart now played, Beatles and Bach not
        for _ in range(30):
            rec = self.db.get_random_album({"unplayed_artist": True})
            self.assertIsNotNone(rec)
            self.assertNotEqual(rec["artist"], "Mozart")

    def test_combined_filters(self):
        rec = self.db.get_random_album({
            "genre": "Classical", "min_duration": 1000,
            "excludes": ["BACH"],
        })
        self.assertIsNotNone(rec)
        self.assertEqual(rec["artist"], "Mozart")


class TestDatabasePlayTracking(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = PyGioNavDatabase(self.td, "test")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def test_record_and_count(self):
        self.assertEqual(self.db.get_play_count(), 0)
        self.db.record_play("a1", "Mozart", "Sym 40", "Classical", "00:30")
        self.assertEqual(self.db.get_play_count(), 1)
        self.db.record_play("a2", "Bach", "Toccata", "Baroque", "00:10")
        self.assertEqual(self.db.get_play_count(), 2)

    def test_recent_plays(self):
        self.db.record_play("a1", "Mozart", "Sym 40", "Classical", "00:30")
        plays = self.db.get_recent_plays(10)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["artist"], "Mozart")

    def test_stats(self):
        a = Album(id="a1", name="Sym 40", artist="Mozart", genre="Classical",
                  year=1788, duration=1800, song_count=4, cover_art="",
                  artist_id="")
        self.db.upsert_album(a)
        self.db.record_play("a1", "Mozart", "Sym 40", "Classical", "00:30")
        stats = self.db.get_stats()
        self.assertEqual(stats["recordings"], 1)
        self.assertEqual(stats["plays"], 1)
        self.assertEqual(stats["unique_artists"], 1)
        self.assertEqual(stats["unplayed"], 0)

    def test_unplayed_count(self):
        for i in range(5):
            a = Album(id=f"a{i}", name=f"Album {i}", artist=f"Artist {i}",
                      genre="G", year=2000, duration=100, song_count=1,
                      cover_art="", artist_id="")
            self.db.upsert_album(a)
        self.assertEqual(self.db.get_unplayed_count(), 5)
        self.db.record_play("a0", "Artist 0", "Album 0", "G", "00:01")
        self.assertEqual(self.db.get_unplayed_count(), 4)

    def test_clear_recordings(self):
        a = Album(id="a1", name="X", artist="Y", genre="G", year=2000,
                  duration=100, song_count=1, cover_art="", artist_id="")
        self.db.upsert_album(a, library_id="lib1")
        a2 = Album(id="a2", name="Z", artist="W", genre="G", year=2000,
                   duration=100, song_count=1, cover_art="", artist_id="")
        self.db.upsert_album(a2, library_id="lib2")
        self.assertEqual(self.db.get_recording_count(), 2)
        self.db.clear_recordings("lib1")
        self.assertEqual(self.db.get_recording_count(), 1)
        self.assertEqual(self.db.get_recording_count("lib2"), 1)


class TestDatabaseMigration(unittest.TestCase):
    """Verify that opening a DB without the library_id column adds it."""
    def test_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE recordings (
                    id INTEGER PRIMARY KEY, album_id TEXT UNIQUE,
                    artist TEXT DEFAULT '', album_name TEXT DEFAULT '',
                    genre TEXT DEFAULT '', year INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 0, song_count INTEGER DEFAULT 0,
                    cover_art TEXT DEFAULT '', artist_id TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                );
                CREATE TABLE plays (
                    id INTEGER PRIMARY KEY, play_date TEXT,
                    album_id TEXT, artist TEXT DEFAULT '',
                    album_name TEXT DEFAULT '', genre TEXT DEFAULT '',
                    duration TEXT DEFAULT '', performer TEXT DEFAULT ''
                );
            """)
            conn.close()
            # Opening via PyGioNavDatabase should add the column
            db = PyGioNavDatabase.__new__(PyGioNavDatabase)
            db.db_path = path
            db._ensure_schema()
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(recordings)")}
            conn.close()
            self.assertIn("library_id", cols)


# ====================================================================
# Display
# ====================================================================

class TestFormatDuration(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_duration(0), "00:00:00")

    def test_negative(self):
        self.assertEqual(format_duration(-5), "00:00:00")

    def test_one_hour_one_minute_one_second(self):
        self.assertEqual(format_duration(3661), "01:01:01")

    def test_90_seconds(self):
        self.assertEqual(format_duration(90), "00:01:30")

    def test_large(self):
        self.assertEqual(format_duration(86400), "24:00:00")


# ====================================================================
# Player
# ====================================================================

class TestAudioOutputDetection(unittest.TestCase):
    @patch("player.platform.uname")
    def test_wsl(self, mock_uname):
        mock_uname.return_value = type("U", (), {
            "system": "Linux", "release": "5.15.0-1-Microsoft-standard"})()
        self.assertEqual(_detect_audio_output(), ["-f", "pulse", "-"])

    @patch("player.platform.uname")
    def test_macos(self, mock_uname):
        mock_uname.return_value = type("U", (), {
            "system": "Darwin", "release": "23.0.0"})()
        self.assertEqual(_detect_audio_output(), ["-f", "audiotoolbox", "-"])

    def test_force_pulse(self):
        self.assertEqual(_audio_output_args("default", True),
                         ["-f", "pulse", "-"])
        self.assertEqual(_audio_output_args("hw:1,0", True),
                         ["-f", "pulse", "hw:1,0"])


class TestAlbumPlayerState(unittest.TestCase):
    def test_skip_stop_flags(self):
        from player import AlbumPlayer
        client = MagicMock()
        p = AlbumPlayer(client)
        self.assertFalse(p.is_skip_requested())
        self.assertFalse(p.is_stop_requested())
        p.request_skip()
        self.assertTrue(p.is_skip_requested())
        self.assertFalse(p.is_stop_requested())
        p.reset_skip()
        self.assertFalse(p.is_skip_requested())
        p.request_stop()
        self.assertTrue(p.is_stop_requested())
        self.assertTrue(p.is_skip_requested())


class TestAlbumPlayerEmptyAlbum(unittest.TestCase):
    def test_empty_songs(self):
        from player import AlbumPlayer
        client = MagicMock()
        p = AlbumPlayer(client)
        album = Album(id="a1", name="Empty", songs=[])
        self.assertFalse(p.play_album(album))


# ====================================================================
# Integration-style: config → DB → filter round-trip
# ====================================================================

class TestConfigToFilterRoundTrip(unittest.TestCase):
    """Verify that config values correctly influence DB queries."""
    def test_library_filter(self):
        with tempfile.TemporaryDirectory() as td:
            db = PyGioNavDatabase(td, "test")
            db.upsert_album(
                Album(id="c1", name="Sym 5", artist="Beethoven",
                      genre="Classical", year=1808, duration=2000,
                      song_count=4, cover_art="", artist_id=""),
                library_id="classical")
            db.upsert_album(
                Album(id="r1", name="Dark Side", artist="Pink Floyd",
                      genre="Rock", year=1973, duration=2580,
                      song_count=10, cover_art="", artist_id=""),
                library_id="rock")
            # Only classical
            for _ in range(20):
                rec = db.get_random_album({"library_id": "classical"})
                self.assertIsNotNone(rec)
                self.assertEqual(rec["artist"], "Beethoven")
            # Only rock
            for _ in range(20):
                rec = db.get_random_album({"library_id": "rock"})
                self.assertIsNotNone(rec)
                self.assertEqual(rec["artist"], "Pink Floyd")


# ====================================================================
# Run
# ====================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
