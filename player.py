"""
Audio playback engine for PyGioNav.

Achieves gapless playback by pre-downloading all tracks in an album
to a local cache directory, then playing them via ffmpeg's concat
demuxer on the local files.  This eliminates any network latency
between tracks — the same approach as the original Giocoso, which
had all FLACs locally.
"""

import glob
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, List, Optional

from subsonic_client import Album, Song, SubsonicClient

log = logging.getLogger("pygionav.player")


# ------------------------------------------------------------------ #
# Audio output detection
# ------------------------------------------------------------------ #

def _detect_audio_output() -> List[str]:
    """Return ffmpeg output arguments for the current platform."""
    uname = platform.uname()

    # WSL
    if "microsoft" in uname.release.lower():
        return ["-f", "pulse", "-"]

    system = uname.system.lower()
    if system == "darwin":
        return ["-f", "audiotoolbox", "-"]

    # Linux — prefer Pulse/PipeWire, fall back to ALSA
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, timeout=3)
        if r.returncode == 0:
            return ["-f", "pulse", "default"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ["-f", "alsa", "default"]


def _audio_output_args(device: str, force_pulse: bool) -> List[str]:
    if force_pulse:
        return ["-f", "pulse", device if device != "default" else "-"]
    base = _detect_audio_output()
    if device and device != "default" and len(base) >= 3:
        base[-1] = device
    return base


# ------------------------------------------------------------------ #
# Player
# ------------------------------------------------------------------ #

class AlbumPlayer:
    """Pre-caches then plays all tracks of an album gaplessly."""

    def __init__(self, client: SubsonicClient, audio_device: str = "default",
                 force_pulse: bool = False, stream_format: str = "raw",
                 stream_bitrate: int = 0, conf_dir: str = ""):
        self.client = client
        self.audio_device = audio_device
        self.force_pulse = force_pulse
        self.stream_format = stream_format
        self.stream_bitrate = stream_bitrate
        self.conf_dir = conf_dir or os.path.expanduser(
            "~/.local/share/pygionav")
        self._process: Optional[subprocess.Popen] = None
        self._skip_requested = False
        self._stop_requested = False

    # ---- skip / stop control -------------------------------------- #

    def request_skip(self):
        self._skip_requested = True
        self._kill_ffmpeg()

    def request_stop(self):
        self._stop_requested = True
        self._skip_requested = True
        self._kill_ffmpeg()

    def is_skip_requested(self) -> bool:
        return self._skip_requested

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    def reset_skip(self):
        self._skip_requested = False

    def _kill_ffmpeg(self):
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass

    # ---- caching -------------------------------------------------- #

    def _cache_tracks(self, album: Album,
                      on_download: Optional[Callable[[int, int, str], None]] = None
                      ) -> Optional[str]:
        """Download every track in *album* to a temp directory.

        Returns the path to the cache directory, or None on failure.
        The caller is responsible for cleaning up the directory.
        """
        cache_dir = tempfile.mkdtemp(prefix="pygionav_cache_", dir=self.conf_dir)
        log.debug("Cache dir: %s", cache_dir)

        fmt = self.stream_format if self.stream_format != "raw" else None
        br = self.stream_bitrate if self.stream_bitrate > 0 else None

        for idx, song in enumerate(album.songs, 1):
            if self._skip_requested or self._stop_requested:
                shutil.rmtree(cache_dir, ignore_errors=True)
                return None

            # Use disc-track numbering in the filename to keep sort order
            # Sanitise extension — it comes from the server and could contain
            # characters that break the ffmpeg concat playlist format.
            ext = "".join(c for c in (song.suffix or "flac") if c.isalnum()) or "flac"
            fname = f"{song.disc:02d}-{song.track:03d}.{ext}"
            dest = os.path.join(cache_dir, fname)

            if on_download:
                on_download(idx, len(album.songs), song.title)

            ok = self.client.download_song(song.id, dest, fmt=fmt,
                                           max_bitrate=br)
            if not ok:
                log.error("Failed to cache track %d: %s", idx, song.title)
                # Continue — we can still play partial albums
                continue

        # Verify we have at least one file
        cached = sorted(glob.glob(os.path.join(cache_dir, "*")))
        if not cached:
            log.error("No tracks were cached for album %s", album.name)
            shutil.rmtree(cache_dir, ignore_errors=True)
            return None

        log.debug("Cached %d files for %s", len(cached), album.name)
        return cache_dir

    # ---- playback ------------------------------------------------- #

    def play_album(self, album: Album,
                   on_download: Optional[Callable[[int, int, str], None]] = None,
                   on_progress: Optional[Callable[[int, int], None]] = None,
                   ) -> bool:
        """Cache and play all tracks of an album gaplessly.

        on_download(done, total, song_title)  — called per track download.
        on_progress(elapsed_secs, total_secs) — called during playback.

        Returns True if playback completed normally.
        """
        if not album.songs:
            return False

        self._skip_requested = False
        self._stop_requested = False

        # ---- Phase 1: pre-cache ---------------------------------- #
        cache_dir = self._cache_tracks(album, on_download)
        if cache_dir is None:
            return False

        # ---- Phase 2: build concat playlist ---------------------- #
        cached_files = sorted(glob.glob(os.path.join(cache_dir, "*")))
        playlist_path = os.path.join(cache_dir, "playlist.txt")
        with open(playlist_path, "w", encoding="utf-8") as f:
            for fp in cached_files:
                safe = fp.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        log.debug("Playlist written with %d entries", len(cached_files))

        total_duration = album.duration or sum(s.duration for s in album.songs)
        audio_out = _audio_output_args(self.audio_device, self.force_pulse)

        # ---- Phase 3: play --------------------------------------- #
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", playlist_path,
            "-max_muxing_queue_size", "900000",
        ] + audio_out

        log.debug("ffmpeg command: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            start_time = time.time()
            while self._process.poll() is None:
                if self._skip_requested or self._stop_requested:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                    return False

                elapsed = int(time.time() - start_time)
                if on_progress:
                    on_progress(elapsed, total_duration)
                time.sleep(0.5)

            # Check for errors
            rc = self._process.returncode
            if rc != 0:
                stderr = self._process.stderr.read().decode(errors="replace")
                log.error("ffmpeg exited with code %d: %s", rc, stderr.strip())

            if on_progress:
                on_progress(total_duration, total_duration)

            return rc == 0

        except FileNotFoundError:
            log.error("ffmpeg not found on PATH")
            print(f"\n  ERROR: ffmpeg not found. Please install ffmpeg.")
            return False
        finally:
            self._process = None
            shutil.rmtree(cache_dir, ignore_errors=True)
            log.debug("Cleaned up cache dir %s", cache_dir)


# ------------------------------------------------------------------ #
# Cover art helper
# ------------------------------------------------------------------ #

def download_and_display_cover_art(client: SubsonicClient,
                                   cover_id: str, conf_dir: str,
                                   size: int = 320) -> Optional[str]:
    """Download cover art and optionally display it via sixel."""
    if not cover_id:
        return None

    os.makedirs(conf_dir, exist_ok=True)
    art_path = os.path.join(conf_dir, "albumart.jpg")

    if not client.download_cover_art(cover_id, art_path, size=size):
        return None

    if shutil.which("img2sixel"):
        try:
            subprocess.run(["img2sixel", "-w", str(size), art_path],
                           timeout=5, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return art_path
