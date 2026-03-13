# CLAUDE.md — Project context for PyGioNav

## What this project is

PyGioNav is a Python rewrite of Giocoso (a Bash-based music player by Howard Rogers). It streams music from a Navidrome server via the Subsonic API instead of reading local FLAC files. Originally built for classical music, it works with any genre. The user has a Navidrome server with multiple libraries; the one they primarily use with this tool is called "Classical".

## Architecture

```
pygionav.py             Main entry point, CLI (argparse), session orchestration
config.py               INI config loading → PyGioNavConfig dataclass
subsonic_client.py      Subsonic REST API client (token auth, XML parsing)
database.py             Local SQLite for library cache + play tracking (PyGioNavDatabase)
player.py               Pre-cache download + ffmpeg concat gapless playback (AlbumPlayer)
display.py              ANSI terminal output and formatting
tests.py                59 unit tests (unittest)
```

## Key design decisions

- **Gapless playback**: All tracks in an album are downloaded to a temp dir BEFORE playback starts. ffmpeg's concat demuxer plays the local files. This eliminates network latency between tracks. The cache dir is cleaned up after each album.
- **Library support**: Navidrome music folders map to `musicFolderId` in the Subsonic API. The `library` config option / `--library` CLI flag resolves a folder name to its ID via `getMusicFolders`, then passes it through sync, filtering, and the SQLite `library_id` column.
- **Config file**: INI format with a `[pygionav]` section. Lives at `~/.local/share/pygionav/pygionav.conf`. The `.example` file has placeholders; the real file is gitignored.
- **Credentials never in repo**: `.gitignore` blocks `pygionav.conf`, `*.db`, `debug.log`, `albumart.jpg`.

## User's environment

- Runs on WSL (Windows Subsystem for Linux) — not bare-metal Linux
- Python 3.13
- Repo lives on an NTFS-mounted Windows drive (`/mnt/z/`), which causes permission issues with pytest cache — solved by `pyproject.toml` redirecting cache to `/tmp/`
- Prefers Python for projects
- Values robustness over speed
- Uses PulseAudio via WSL for audio output

## Running tests

```bash
python3 -m pytest tests.py -v
# or
python3 tests.py
```

All 66 tests should pass with 0 warnings. If pytest cache warnings appear, check that `pyproject.toml` has `cache_dir = "/tmp/.pytest_cache"`.

## Important conventions

- Class names: `PyGioNavConfig`, `PyGioNavDatabase`, `AlbumPlayer`, `SubsonicClient`
- Logger hierarchy: `pygionav`, `pygionav.api`, `pygionav.db`, `pygionav.player`
- Config section name in INI file: `[pygionav]`
- Config dir: `~/.local/share/pygionav/`
- Config filename: `pygionav.conf`
- The `--debug` flag or `debug = yes` in config enables logging to stderr and `debug.log`
- Filter negation uses `@` suffix (e.g. `--artist=Mozart@` means NOT Mozart) — inherited from original Giocoso
- DB migration: `_ensure_schema()` adds `library_id` column if absent (upgrade path from earlier versions)
- Subsonic auth: token-based (API 1.13+), `token = md5(password + salt)`, new random salt per request

## Common tasks

- **Add a new filter**: Add field to `PyGioNavConfig`, add to `_STR_KEYS`/`_INT_KEYS`/`_BOOL_KEYS` in config.py, add CLI arg in `parse_args()`, wire into `build_filters()` and `describe_filters()` in pygionav.py, add WHERE clause in `get_random_album()` in database.py, add test in tests.py
- **Add a new Subsonic API call**: Add method to `SubsonicClient` in subsonic_client.py, add data class if needed, add test
- **Change display layout**: Edit display.py — all terminal output goes through functions there

## Dependencies

- `requests` (HTTP client for Subsonic API)
- `ffmpeg` (external binary, must be on PATH)
- Optional: `img2sixel` for inline album art in terminal
- Standard library: `sqlite3`, `hashlib`, `xml.etree.ElementTree`, `argparse`, `configparser`, `logging`, `subprocess`, `tempfile`
