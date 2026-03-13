#!/usr/bin/env python3
"""
PyGioNav — A music player that streams from Navidrome.

Python rewrite of Giocoso (https://bbritten.com/softwares/giocoso/).
Instead of playing local FLAC files it streams them from a Navidrome
server via the Subsonic API.

Usage:
  pygionav.py                            Play with defaults
  pygionav.py --sync                     Sync library to local cache
  pygionav.py --stats                    Show database statistics
  pygionav.py --history                  Show recent play history
  pygionav.py --library=Classical        Only use the "Classical" library
  pygionav.py --selections=5             Play 5 random albums
  pygionav.py --artist=Mozart            Filter by artist
  pygionav.py --genre=Baroque            Filter by genre
  pygionav.py --debug                    Enable verbose debug logging
  pygionav.py --help                     Show help

Based on Giocoso by Howard Rogers (GPL v2).
"""

import argparse
import logging
import os
import signal
import sys
import time
from typing import Dict, Optional

from config import PyGioNavConfig, get_excludes, load_config
from database import PyGioNavDatabase
from display import (
    C, display_download_progress, display_header, display_intercycle_pause,
    display_message, display_now_playing, display_playback_finished,
    display_progress, display_recent_plays, display_scrobble_status,
    display_search_progress, display_stats, format_duration,
)
from player import AlbumPlayer, download_and_display_cover_art
from subsonic_client import SubsonicClient, SubsonicError

log = logging.getLogger("pygionav")

MAX_SEARCH_ATTEMPTS = 200


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PyGioNav: stream music from Navidrome",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Actions
    p.add_argument("--sync", action="store_true",
                   help="Sync Navidrome library to local cache")
    p.add_argument("--stats", action="store_true",
                   help="Show database statistics")
    p.add_argument("--history", action="store_true",
                   help="Show recent play history")
    p.add_argument("--list-libraries", action="store_true",
                   help="List available Navidrome libraries and exit")

    # Config / debug
    p.add_argument("--config", type=str, default=None,
                   help="Path to config file")
    p.add_argument("--debug", action="store_true",
                   help="Enable verbose debug logging to stderr and file")

    # Library
    p.add_argument("--library", type=str, default=None,
                   help="Navidrome library name to use (e.g. 'Classical')")

    # Playback overrides
    p.add_argument("--selections", type=int, default=None)
    p.add_argument("--artist", type=str, default=None,
                   help="Filter by artist (append @ to negate)")
    p.add_argument("--genre", type=str, default=None,
                   help="Filter by genre (append @ to negate)")
    p.add_argument("--album", type=str, default=None,
                   help="Filter by album name (append @ to negate)")
    p.add_argument("--min-duration", type=int, default=None,
                   help="Min album duration in minutes")
    p.add_argument("--max-duration", type=int, default=None,
                   help="Max album duration in minutes")
    p.add_argument("--timebar", type=int, default=None,
                   help="Don't repeat an artist within N hours")
    p.add_argument("--unplayed-artists", action="store_true", default=None)
    p.add_argument("--unplayed-works", action="store_true", default=None)
    p.add_argument("--no-scrobble", action="store_true")
    p.add_argument("--no-art", action="store_true")
    p.add_argument("--pause", type=int, default=None,
                   help="Seconds between albums")
    p.add_argument("--ignore-excludes", action="store_true")

    return p.parse_args()


# ------------------------------------------------------------------ #
# Logging setup
# ------------------------------------------------------------------ #

def setup_logging(debug: bool, conf_dir: str):
    root = logging.getLogger("pygionav")
    root.setLevel(logging.DEBUG if debug else logging.WARNING)

    if debug:
        # stderr handler
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s %(name)-20s %(levelname)-5s  %(message)s",
            datefmt="%H:%M:%S"))
        root.addHandler(sh)

        # file handler
        os.makedirs(conf_dir, exist_ok=True)
        fh = logging.FileHandler(
            os.path.join(conf_dir, "debug.log"), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s  %(message)s"))
        root.addHandler(fh)

        log.debug("Debug logging enabled — log file: %s/debug.log", conf_dir)


# ------------------------------------------------------------------ #
# Library resolution
# ------------------------------------------------------------------ #

def resolve_library(client: SubsonicClient,
                    cfg: PyGioNavConfig,
                    args: argparse.Namespace) -> Optional[str]:
    """Determine the musicFolderId to use, or None for all libraries."""
    name = args.library if args.library is not None else cfg.library
    if not name:
        return None
    lib_id = client.resolve_library_id(name)
    if lib_id is None:
        display_message(
            f"Library '{name}' not found on the Navidrome server.\n"
            "  Use --list-libraries to see available libraries.",
            is_error=True)
        sys.exit(1)
    log.debug("Using library %r  (id=%s)", name, lib_id)
    return lib_id


# ------------------------------------------------------------------ #
# Sync
# ------------------------------------------------------------------ #

def sync_library(client: SubsonicClient, db: PyGioNavDatabase,
                 library_id: Optional[str], library_name: str):
    display_header(mode="Library Sync", library=library_name)
    target = f"library '{library_name}'" if library_name else "all libraries"
    print(f"  {C.YELLOW}Syncing {target} from Navidrome…{C.RESET}\n")

    offset = 0
    page_size = 500
    total = 0

    while True:
        try:
            albums = client.get_album_list(
                list_type="alphabeticalByArtist",
                size=page_size, offset=offset,
                music_folder_id=library_id,
            )
        except SubsonicError as e:
            display_message(f"API error: {e.message}", is_error=True)
            break
        except Exception as e:
            display_message(f"Connection error: {e}", is_error=True)
            break

        if not albums:
            break

        for a in albums:
            db.upsert_album(a, library_id=library_id or "")
            total += 1

        sys.stdout.write(f"\r  {C.GREEN}Albums synced: {total}{C.RESET}   ")
        sys.stdout.flush()

        if len(albums) < page_size:
            break
        offset += page_size

    print(f"\n\n  {C.NORMAL}Sync complete. "
          f"{C.YELLOW}{total}{C.NORMAL} albums in local cache.{C.RESET}\n")


# ------------------------------------------------------------------ #
# Filter helpers
# ------------------------------------------------------------------ #

def build_filters(cfg: PyGioNavConfig, args: argparse.Namespace,
                  library_id: Optional[str]) -> Dict:
    f: Dict = {}

    artist = args.artist if args.artist is not None else cfg.artist
    if artist:
        f["artist"] = artist

    genre = args.genre if args.genre is not None else cfg.genre
    if genre:
        f["genre"] = genre

    album = args.album if args.album is not None else cfg.album
    if album:
        f["album"] = album

    mn = args.min_duration if args.min_duration is not None else cfg.min_duration
    mx = args.max_duration if args.max_duration is not None else cfg.max_duration
    if mn > 0:
        f["min_duration"] = mn * 60
    if mx > 0:
        f["max_duration"] = mx * 60

    tb = args.timebar if args.timebar is not None else cfg.time_bar
    if tb > 0:
        f["time_bar_hours"] = tb

    if args.unplayed_artists or cfg.unplayed_artist:
        f["unplayed_artist"] = True
    if args.unplayed_works or cfg.unplayed_works:
        f["unplayed_works"] = True

    if not args.ignore_excludes:
        excl = get_excludes(cfg)
        if excl:
            f["excludes"] = excl

    if library_id:
        f["library_id"] = library_id

    return f


def describe_filters(filters: Dict) -> str:
    parts = []
    for fkey, label in (("artist", "artist"), ("genre", "genre"),
                        ("album", "album")):
        v = filters.get(fkey)
        if v:
            if v.endswith("@"):
                parts.append(f"{label} ≠ {v[:-1]}")
            else:
                parts.append(f"{label} = {v}")
    if filters.get("min_duration"):
        parts.append(f"min {filters['min_duration'] // 60}m")
    if filters.get("max_duration"):
        parts.append(f"max {filters['max_duration'] // 60}m")
    if filters.get("time_bar_hours"):
        parts.append(f"timebar {filters['time_bar_hours']}h")
    if filters.get("unplayed_artist"):
        parts.append("unplayed artists only")
    if filters.get("unplayed_works"):
        parts.append("unplayed works only")
    if filters.get("excludes"):
        parts.append(f"{len(filters['excludes'])} artists excluded")
    if filters.get("library_id"):
        parts.append("library-filtered")
    return " · ".join(parts)


# ------------------------------------------------------------------ #
# Play session
# ------------------------------------------------------------------ #

def play_session(client: SubsonicClient, db: PyGioNavDatabase,
                 cfg: PyGioNavConfig, args: argparse.Namespace,
                 library_id: Optional[str], library_name: str):

    selections = args.selections if args.selections is not None else cfg.selections
    selections = max(1, min(selections, 99))
    pause_time = args.pause if args.pause is not None else cfg.pause_between
    do_scrobble = cfg.scrobble and not args.no_scrobble
    show_art = cfg.show_album_art and not args.no_art

    filters = build_filters(cfg, args, library_id)
    filters_desc = describe_filters(filters)

    player = AlbumPlayer(
        client=client, audio_device=cfg.audio_device,
        force_pulse=cfg.force_pulse, stream_format=cfg.stream_format,
        stream_bitrate=cfg.stream_bitrate, conf_dir=cfg.conf_dir,
    )

    # Ctrl+C: first press skips, second stops
    skip_count = [0]

    def on_sigint(signum, frame):
        skip_count[0] += 1
        if skip_count[0] >= 2:
            player.request_stop()
            display_message("Stopping playback…")
        else:
            player.request_skip()
            print(f"\n  {C.CYAN}Skipping… (Ctrl+C again to stop){C.RESET}")

    signal.signal(signal.SIGINT, on_sigint)

    # Verify cache has data
    rec_count = db.get_recording_count(library_id)
    if rec_count == 0:
        display_header(mode="Database Play", library=library_name)
        display_message(
            "No recordings in local cache. Run with --sync first.",
            is_error=True)
        return

    log.debug("Starting play session: selections=%d filters=%s rec_count=%d",
              selections, filters, rec_count)

    for play_num in range(1, selections + 1):
        if player.is_stop_requested():
            break
        skip_count[0] = 0
        player.reset_skip()

        display_header(play_num=play_num, total_plays=selections,
                       db_name=cfg.db_name, mode="Database Play",
                       library=library_name)

        # Search
        recording = None
        for attempt in range(1, MAX_SEARCH_ATTEMPTS + 1):
            display_search_progress(attempt, MAX_SEARCH_ATTEMPTS)
            recording = db.get_random_album(filters)
            if recording is not None:
                break
            time.sleep(0.02)

        if recording is None:
            print()
            display_message(
                f"No matching album after {MAX_SEARCH_ATTEMPTS} attempts.\n"
                "  Broaden your filters or re-sync.", is_error=True)
            break

        log.debug("Selected album: %s — %s (id=%s)",
                  recording["artist"], recording["album_name"],
                  recording["album_id"])

        # Fetch full album (with songs) from Navidrome
        try:
            album = client.get_album(recording["album_id"])
        except SubsonicError as e:
            display_message(f"API error: {e.message}", is_error=True)
            continue
        except Exception as e:
            display_message(f"Connection error: {e}", is_error=True)
            continue

        if not album.songs:
            display_message(f"Album '{album.name}' has no tracks.", is_error=True)
            continue

        total_dur = album.duration or sum(s.duration for s in album.songs)
        dur_str = format_duration(total_dur)

        # Display
        display_header(play_num=play_num, total_plays=selections,
                       db_name=cfg.db_name, mode="Database Play",
                       library=library_name)
        display_now_playing(album.artist, album.name, album.genre,
                            album.year, len(album.songs), dur_str,
                            filters_desc)

        # Album art
        if show_art and album.cover_art:
            download_and_display_cover_art(
                client, album.cover_art, cfg.conf_dir, cfg.art_size)

        # Download + Play
        def on_dl(done, total, title):
            display_download_progress(done, total, title)

        def on_prog(elapsed, total):
            display_progress(elapsed, total)

        print()  # newline before download progress
        completed = player.play_album(album, on_download=on_dl,
                                      on_progress=on_prog)
        print()  # newline after progress bar

        if player.is_stop_requested():
            break

        # Record play
        if not player.is_skip_requested():
            db.record_play(
                album_id=album.id, artist=album.artist,
                album_name=album.name, genre=album.genre,
                duration=dur_str)

            if do_scrobble:
                ok = all(client.scrobble(s.id) for s in album.songs)
                display_scrobble_status(album.name, ok)

        # Intercycle pause
        if play_num < selections and not player.is_stop_requested():
            for rem in range(pause_time, 0, -1):
                if player.is_stop_requested() or player.is_skip_requested():
                    break
                display_intercycle_pause(rem)
                time.sleep(1)
            print()

    display_playback_finished(do_scrobble)


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main():
    args = parse_args()
    cfg = load_config(args.config)
    os.makedirs(cfg.conf_dir, exist_ok=True)

    # Debug mode — from CLI flag or config
    debug = args.debug or cfg.debug
    setup_logging(debug, cfg.conf_dir)

    # Validate
    if not cfg.server_url or not cfg.username:
        display_header()
        display_message(
            "Navidrome server not configured.\n"
            f"  Edit {os.path.join(cfg.conf_dir, 'pygionav.conf')}\n"
            "  and set server_url, username, password under [pygionav].",
            is_error=True)
        sys.exit(1)

    client = SubsonicClient(cfg.server_url, cfg.username, cfg.password)

    # Test connectivity
    try:
        client.ping()
        log.debug("Ping OK")
    except SubsonicError as e:
        display_header()
        display_message(f"Auth failed: {e.message}", is_error=True)
        sys.exit(1)
    except Exception as e:
        display_header()
        display_message(f"Cannot connect to {cfg.server_url}\n  {e}",
                        is_error=True)
        sys.exit(1)

    # List libraries
    if args.list_libraries:
        display_header(mode="Libraries")
        folders = client.get_music_folders()
        if not folders:
            display_message("No libraries found on server.", is_error=True)
        else:
            for f in folders:
                print(f"  {C.YELLOW}{f.name}{C.NORMAL}  (id={f.id}){C.RESET}")
        print()
        sys.exit(0)

    # Resolve library
    library_id = resolve_library(client, cfg, args)
    library_name = (args.library or cfg.library) if library_id else ""

    db = PyGioNavDatabase(cfg.conf_dir, cfg.db_name)

    # Actions
    if args.sync:
        sync_library(client, db, library_id, library_name)
        sys.exit(0)

    if args.stats:
        display_header(mode="Statistics", library=library_name)
        display_stats(db.get_stats(library_id))
        sys.exit(0)

    if args.history:
        display_header(mode="Play History", library=library_name)
        display_recent_plays(db.get_recent_plays(20))
        sys.exit(0)

    play_session(client, db, cfg, args, library_id, library_name)


if __name__ == "__main__":
    main()
