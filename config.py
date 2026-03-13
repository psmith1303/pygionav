"""
Configuration management for PyGioNav.
Reads an INI-format config file and provides defaults.
"""

import logging
import os
import configparser
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional


DEFAULT_CONF_DIR = Path.home() / ".local" / "share" / "pygionav"

log = logging.getLogger("pygionav")


@dataclass
class PyGioNavConfig:
    """All configuration values with sensible defaults."""

    # Navidrome connection
    server_url: str = "http://localhost:4533"
    username: str = ""
    password: str = ""

    # Navidrome library (music folder) name — empty string means all libraries
    library: str = ""

    # Database
    db_name: str = "music"

    # Playback
    selections: int = 1
    pause_between: int = 10
    audio_device: str = "default"
    force_pulse: bool = False

    # Filtering
    genre: str = ""
    artist: str = ""
    album: str = ""
    performer: str = ""
    min_duration: int = 0     # minutes
    max_duration: int = 0     # 0 = no limit
    unplayed_artist: bool = False
    unplayed_works: bool = False

    # Time bar: don't repeat an artist within this many hours (0 = disabled)
    time_bar: int = 0

    # Scrobble via Navidrome's own scrobble endpoint
    scrobble: bool = True

    # Display
    show_album_art: bool = True
    art_size: int = 320

    # Excludes
    excludes_file: str = ""

    # Paths
    conf_dir: str = ""

    # Transcoding: "raw" streams original file; or specify "mp3", "opus", etc.
    stream_format: str = "raw"
    stream_bitrate: int = 0   # 0 = original quality

    # Debug
    debug: bool = False

    def __post_init__(self):
        if not self.conf_dir:
            self.conf_dir = str(DEFAULT_CONF_DIR)
        if not self.excludes_file:
            self.excludes_file = os.path.join(self.conf_dir, "excludes.txt")


# All simple-string keys in the config file
_STR_KEYS = (
    "server_url", "username", "password", "db_name", "library",
    "audio_device", "genre", "artist", "album",
    "performer", "excludes_file", "conf_dir", "stream_format",
)

# All integer keys
_INT_KEYS = (
    "selections", "pause_between", "min_duration", "max_duration",
    "time_bar", "art_size", "stream_bitrate",
)

# All boolean keys
_BOOL_KEYS = (
    "force_pulse", "scrobble", "show_album_art",
    "unplayed_artist", "unplayed_works", "debug",
)


def load_config(config_path: Optional[str] = None) -> PyGioNavConfig:
    """Load configuration from an INI-style file.

    The file uses a [pygionav] section.  Missing keys retain their defaults.
    """
    cfg = PyGioNavConfig()

    if config_path is None:
        config_path = os.path.join(cfg.conf_dir, "pygionav.conf")

    if not os.path.isfile(config_path):
        log.debug("Config file not found at %s — using defaults", config_path)
        return cfg

    log.debug("Loading config from %s", config_path)
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    if "pygionav" not in parser:
        log.debug("No [pygionav] section in config file")
        return cfg

    section = parser["pygionav"]

    for key in _STR_KEYS:
        if key in section:
            setattr(cfg, key, section[key])

    for key in _INT_KEYS:
        if key in section:
            try:
                setattr(cfg, key, int(section[key]))
            except ValueError:
                log.warning("Invalid integer for config key '%s'", key)

    for key in _BOOL_KEYS:
        if key in section:
            setattr(cfg, key, section[key].lower() in ("yes", "true", "1"))

    cfg.__post_init__()
    log.debug("Config loaded: server=%s user=%s library=%r db=%s",
              cfg.server_url, cfg.username, cfg.library or "(all)", cfg.db_name)
    return cfg


def get_excludes(cfg: PyGioNavConfig) -> List[str]:
    """Read the excludes file and return excluded artist names (uppercased)."""
    excludes: List[str] = []
    path = cfg.excludes_file
    if not path or not os.path.isfile(path):
        return excludes
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                excludes.append(line.upper())
    log.debug("Loaded %d excludes from %s", len(excludes), path)
    return excludes
