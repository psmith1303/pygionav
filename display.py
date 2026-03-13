"""
Terminal display routines for PyGioNav.
"""

import os
import shutil
import subprocess
import sys
from typing import List, Optional


class C:
    """ANSI colour codes."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    ITALIC  = "\033[3m"
    YELLOW  = "\033[1;93m"
    CYAN    = "\033[1;36m"
    GREEN   = "\033[1;32m"
    RED     = "\033[1;31m"
    BLUE    = "\033[1;34m"
    NORMAL  = "\033[0;37m"
    DIM     = "\033[2m"


PROG_NAME = "PyGioNav"
PROG_VERSION = "4.0.0"
LINE_SINGLE = "─"
LINE_DOUBLE = "═"


def _tw() -> int:
    return shutil.get_terminal_size((100, 28)).columns


def clear():
    subprocess.run(["cls" if os.name == "nt" else "clear"], check=False)


def draw_line(char: str = LINE_SINGLE, width: Optional[int] = None):
    w = width or min(_tw(), 103)
    print(f"  {char * (w - 4)}")


def display_header(play_num: int = 0, total_plays: int = 0,
                   db_name: str = "", mode: str = "",
                   library: str = ""):
    clear()
    w = min(_tw(), 103)
    draw_line(LINE_SINGLE, w)
    title = f"♬♪  {PROG_NAME}: Music Player  ♬♪"
    print(f"{C.CYAN}{title:^{w}}{C.RESET}")
    version_line = f"Version {PROG_VERSION} — Streaming from Navidrome"
    if mode:
        version_line += f" ({mode})"
    print(f"{C.GREEN}{version_line:^{w}}{C.RESET}")
    draw_line(LINE_SINGLE, w)
    info_parts = []
    if play_num > 0:
        info_parts.append(f"Selection {play_num:02d} of {total_plays:02d}")
    if db_name:
        info_parts.append(f"Database: {db_name}")
    if library:
        info_parts.append(f"Library: {library}")
    if info_parts:
        print(f"  {C.NORMAL}{' · '.join(info_parts)}{C.RESET}")
    print()


# Approximate terminal cell dimensions in pixels (used for sixel layout)
_CELL_PX_W = 8
_CELL_PX_H = 16


def _make_possessive(name: str) -> str:
    if name.endswith("ss"):
        return f"{name}'s"
    elif name.endswith("s"):
        return f"{name}'"
    return f"{name}'s"


def _build_track_info_lines(artist: str, album_name: str, genre: str,
                             year: int, track_count: int,
                             duration_str: str,
                             filters_desc: str = "") -> List[str]:
    """Build formatted track-info lines (no leading indent)."""
    lines: List[str] = []
    if filters_desc:
        lines.append(
            f"{C.NORMAL}Playing works…{C.CYAN} {filters_desc}{C.RESET}")
        lines.append("")

    lines.append(f"{C.YELLOW}{_make_possessive(artist)}{C.RESET}")
    lines.append(f"{C.CYAN}{C.ITALIC}{album_name}{C.RESET}")
    lines.append("")

    meta = []
    if genre:
        meta.append(genre)
    if year:
        meta.append(str(year))
    meta.append(f"{track_count} tracks")
    meta.append(f"Duration: {duration_str}")
    lines.append(f"{C.NORMAL}{' · '.join(meta)}{C.RESET}")
    return lines


def display_now_playing(artist: str, album_name: str, genre: str,
                        year: int, track_count: int, duration_str: str,
                        filters_desc: str = ""):
    lines = _build_track_info_lines(artist, album_name, genre, year,
                                     track_count, duration_str, filters_desc)
    for line in lines:
        print(f"    {line}")
    print()


def display_now_playing_with_art(
        art_path: Optional[str], art_size: int,
        artist: str, album_name: str, genre: str,
        year: int, track_count: int, duration_str: str,
        filters_desc: str = ""):
    """Display album art on the left with track details to the right.

    Falls back to text-only when art is unavailable, img2sixel is
    missing, or the terminal is too narrow for side-by-side layout.
    """
    lines = _build_track_info_lines(artist, album_name, genre, year,
                                     track_count, duration_str, filters_desc)

    can_sixel = bool(
        art_path and os.path.isfile(art_path)
        and shutil.which("img2sixel"))

    if not can_sixel:
        for line in lines:
            print(f"    {line}")
        print()
        return

    left_margin = 2
    gap = 3
    img_cols = art_size // _CELL_PX_W
    img_rows = art_size // _CELL_PX_H
    text_offset = left_margin + img_cols + gap

    # Fall back to stacked layout if the terminal is too narrow
    if _tw() < text_offset + 30:
        print(" " * left_margin, end="", flush=True)
        try:
            subprocess.run(["img2sixel", "-w", str(art_size), art_path],
                           timeout=5, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass
        print()
        for line in lines:
            print(f"    {line}")
        print()
        return

    # --- side-by-side layout ---
    # Print the sixel image (cursor ends up below the image, column 0)
    print(" " * left_margin, end="", flush=True)
    try:
        result = subprocess.run(
            ["img2sixel", "-w", str(art_size), art_path], timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        for line in lines:
            print(f"    {line}")
        print()
        return
    if result.returncode != 0:
        for line in lines:
            print(f"    {line}")
        print()
        return

    # Move cursor back up to the top of the image area
    sys.stdout.write(f"\033[{img_rows}A")

    # Print each text line, offset to the right of the image
    for line in lines:
        sys.stdout.write(f"\033[{text_offset}C{line}\n")

    # If the text was shorter than the image, advance past the image
    remaining = img_rows - len(lines)
    if remaining > 0:
        sys.stdout.write(f"\033[{remaining}B")

    print()
    sys.stdout.flush()


def display_progress(elapsed: int, total: int):
    e_str = format_duration(elapsed)
    r_str = format_duration(max(0, total - elapsed))
    t_str = format_duration(total)
    bw = 30
    filled = int(bw * elapsed / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bw - filled)
    sys.stdout.write(
        f"\r  {C.NORMAL}Played: {C.YELLOW}{e_str}{C.NORMAL}"
        f"  [{bar}]  "
        f"Remaining: {C.YELLOW}{r_str}{C.NORMAL}"
        f"  / Total: {C.YELLOW}{t_str}{C.RESET}"
    )
    sys.stdout.flush()


def display_download_progress(done: int, total: int, song_title: str):
    sys.stdout.write(
        f"\r  {C.GREEN}Caching track {done}/{total}: "
        f"{C.YELLOW}{song_title[:50]}{C.RESET}          "
    )
    sys.stdout.flush()


def display_search_progress(attempt: int, max_attempts: int):
    sys.stdout.write(
        f"\r  {C.GREEN}Searching for music to play: "
        f"{attempt} / {max_attempts}{C.RESET}   "
    )
    sys.stdout.flush()


def display_intercycle_pause(remaining: int):
    sys.stdout.write(
        f"\r  {C.NORMAL}Pausing between plays for "
        f"{C.CYAN}{remaining}{C.NORMAL} seconds  {C.RESET}"
    )
    sys.stdout.flush()


def display_scrobble_status(album_name: str, success: bool):
    st = f"{C.CYAN}OK{C.RESET}" if success else f"{C.RED}Error{C.RESET}"
    print(f"\n  {C.NORMAL}Scrobbling: {C.YELLOW}{album_name[:80]}{C.NORMAL} — {st}")


def display_message(text: str, is_error: bool = False):
    colour = C.RED if is_error else C.YELLOW
    print(f"\n  {colour}{text}{C.RESET}\n")


def display_stats(stats: dict):
    draw_line(LINE_SINGLE)
    print(f"  {C.YELLOW}{'Statistic':<35}{'Value':>10}{C.RESET}")
    draw_line(LINE_SINGLE)
    for label, key in (("Unique Artists:", "unique_artists"),
                       ("Recordings (total):", "recordings"),
                       ("Days of continuous play:", "total_duration_days"),
                       ("Count of Plays:", "plays"),
                       ("Recordings not yet played:", "unplayed")):
        print(f"  {C.NORMAL}{label:<35}{stats[key]:>10}{C.RESET}")
    draw_line(LINE_DOUBLE)
    print()


def display_recent_plays(plays: list):
    if not plays:
        print(f"  {C.YELLOW}No plays recorded yet.{C.RESET}")
        return
    draw_line(LINE_SINGLE)
    print(f"  {C.YELLOW}{'Date':<20}{'Artist':<30}{'Album':<40}{C.RESET}")
    draw_line(LINE_SINGLE)
    for p in plays:
        d = p.get("play_date", "")[:16]
        a = (p.get("artist", ""))[:28]
        al = (p.get("album_name", ""))[:38]
        print(f"  {C.NORMAL}{d:<20}{a:<30}{al:<40}{C.RESET}")
    draw_line(LINE_DOUBLE)
    print()


def display_playback_finished(scrobbled: bool):
    print()
    draw_line(LINE_SINGLE)
    if scrobbled:
        print(f"  {C.NORMAL}All scrobbling completed. Playback finished.{C.RESET}")
    else:
        print(f"  {C.NORMAL}Playback finished.{C.RESET}")
    draw_line(LINE_DOUBLE)
    print()


def format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
