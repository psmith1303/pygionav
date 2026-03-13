# PyGioNav

A Python rewrite of [Giocoso](https://bbritten.com/softwares/giocoso/giocoso) — the music player by Howard Rogers — adapted to stream from a [Navidrome](https://www.navidrome.org/) server instead of reading local FLAC files.

## What Is This?

The original Giocoso is a Bash-based, gapless, randomising FLAC player for Linux and macOS. It selects albums at random (with optional filters for artist, genre, duration, etc.), plays them via ffmpeg, tracks plays in a SQLite database, and scrobbles to Last.fm.

PyGioNav preserves the core philosophy and all major features, but replaces filesystem access with Navidrome's Subsonic API. Your FLAC files stay on the Navidrome server; PyGioNav streams them on demand.

Although Giocoso was originally built for classical music, PyGioNav works with any genre. If your Navidrome server hosts multiple libraries (e.g. "Classical", "Rock", "Jazz"), you can target a specific one with the `--library` flag or the `library` config option.

## Gapless Playback

True gapless playback is achieved by **pre-downloading all tracks** in an album to a local cache before playback begins. ffmpeg's concat demuxer then plays the local files back-to-back with zero inter-track silence. This is the same fundamental approach as the original Giocoso (which had all FLACs on disk) and eliminates any risk of network latency introducing gaps between movements.

The cache is cleaned up automatically after each album finishes.

## Features

- **Random album selection** from the full library or a named sub-library
- **Filtering** by artist, genre, album name, duration range
- **Gapless playback** via pre-cache + ffmpeg concat demuxer
- **Play tracking** in a local SQLite database
- **Time bar** — avoid repeating an artist within N hours
- **Unplayed-artist mode** — only pick artists never played before
- **Unplayed-works mode** — only pick albums never played before
- **Artist excludes list** — permanently skip certain artists
- **Scrobbling** — via Navidrome's Subsonic scrobble endpoint (propagates to Last.fm/ListenBrainz if configured in Navidrome)
- **Colourful terminal display** with progress bar
- **Album art display** (inline via sixel if `img2sixel` is available)
- **Multi-library support** — target a specific Navidrome music folder
- **Debug logging** — verbose output to stderr and a log file
- **Comprehensive test suite** (59 tests)

## Requirements

- **Python 3.8+**
- **ffmpeg** (must be on PATH)
- **requests** (`pip install requests`)
- **A running Navidrome server**
- **Optional:** `img2sixel` (from libsixel) for inline album art

### Debian/Ubuntu/WSL:

```bash
sudo apt install ffmpeg python3 python3-pip libsixel-bin
pip install requests
```

### Fedora:

```bash
sudo dnf install ffmpeg python3 python3-pip libsixel-utils
pip install requests
```

### Arch:

```bash
sudo pacman -S ffmpeg python python-pip libsixel
pip install requests
```

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/pygionav.git
cd pygionav
pip install -r requirements.txt

# Create your local config (this file is NOT tracked by git)
mkdir -p ~/.local/share/pygionav
cp pygionav.conf.example ~/.local/share/pygionav/pygionav.conf
nano ~/.local/share/pygionav/pygionav.conf
```

## Credential Safety

Your Navidrome password lives only in `~/.local/share/pygionav/pygionav.conf`, which is:

- **Outside** the repository directory entirely
- **Blocked** from accidental commits by `.gitignore` (which excludes `pygionav.conf`)

Only `pygionav.conf.example` — containing placeholder values `your_username` / `your_password` — is tracked in git. The `.gitignore` also blocks databases (`*.db`), debug logs, and album art from being committed.

## Getting Started

### 1. Configure

Edit `~/.local/share/pygionav/pygionav.conf`:

```ini
[pygionav]
server_url = http://your-navidrome:4533
username = your_username
password = your_password
library = Classical
```

### 2. List Available Libraries

```bash
python3 pygionav.py --list-libraries
```

### 3. Sync Your Library

```bash
# Sync only the "Classical" library
python3 pygionav.py --sync --library=Classical

# Sync everything
python3 pygionav.py --sync
```

### 4. Play Music

```bash
# One random album from the configured library
python3 pygionav.py

# Five albums, only Beethoven
python3 pygionav.py --selections=5 --artist=Beethoven

# Rock library, no scrobbling
python3 pygionav.py --library=Rock --no-scrobble

# Only unplayed works, with debug logging
python3 pygionav.py --unplayed-works --debug
```

### 5. Stats and History

```bash
python3 pygionav.py --stats
python3 pygionav.py --history
```

## Command-Line Reference

| Flag | Description |
|------|-------------|
| `--sync` | Sync Navidrome library to local cache |
| `--stats` | Show database statistics |
| `--history` | Show recent play history |
| `--list-libraries` | List available Navidrome libraries |
| `--config=PATH` | Use a specific config file |
| `--debug` | Enable verbose debug logging |
| `--library=NAME` | Target a specific Navidrome library |
| `--selections=N` | Play N albums (1–99) |
| `--artist=NAME` | Filter by artist (append `@` to negate) |
| `--genre=NAME` | Filter by genre (append `@` to negate) |
| `--album=NAME` | Filter by album name (append `@` to negate) |
| `--min-duration=N` | Minimum album duration in minutes |
| `--max-duration=N` | Maximum album duration in minutes |
| `--timebar=N` | Don't repeat an artist within N hours |
| `--unplayed-artists` | Only pick never-played artists |
| `--unplayed-works` | Only pick never-played albums |
| `--no-scrobble` | Disable scrobbling for this session |
| `--no-art` | Don't display album art |
| `--pause=N` | Seconds to pause between albums |
| `--ignore-excludes` | Ignore the excludes file |

## Debugging

Enable debug mode with `--debug` or set `debug = yes` in the config file. This produces:

- Verbose output to **stderr** (timestamped, showing API calls, SQL queries, ffmpeg commands, cache paths)
- A persistent log file at `~/.local/share/pygionav/debug.log`

Example:

```bash
python3 pygionav.py --debug --selections=1 2>debug_stderr.txt
```

## Running the Tests

```bash
# With pytest (recommended)
python3 -m pytest tests.py -v

# Or with unittest directly
python3 tests.py
```

The test suite (59 tests) covers config loading and edge cases, Subsonic XML data-class parsing, authentication token generation and salt uniqueness, URL construction, database schema creation and migration, album filtering with every filter type (including library-ID filtering), play tracking, statistics, display formatting, audio-output detection, and player state management.

## Architecture

```
pygionav.py             Main entry point, CLI, session orchestration
config.py               Configuration loading and defaults
subsonic_client.py      Subsonic/OpenSubsonic REST API client
database.py             Local SQLite for play tracking & library cache
player.py               Pre-cache download + ffmpeg gapless playback
display.py              Terminal display and formatting
tests.py                Comprehensive test suite (59 tests)
```

## Adding to GitHub

### First time setup

```bash
cd pygionav

git init
git add .

# SAFETY CHECK — verify your real config is NOT staged:
git status
# You should see pygionav.conf.example but NOT pygionav.conf
# If you see pygionav.conf listed, stop and check your .gitignore

git commit -m "Initial commit of PyGioNav"

git branch -M main
git remote add origin git@github.com:YOUR_USERNAME/pygionav.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.  If you prefer HTTPS over SSH:

```bash
git remote add origin https://github.com/YOUR_USERNAME/pygionav.git
```

### After making changes

```bash
git add -A
git status          # always check nothing sensitive is staged
git commit -m "Description of changes"
git push
```

### If you ever accidentally commit credentials

```bash
# Remove the config from git history (keeps your local copy)
git rm --cached pygionav.conf
git commit -m "Remove accidentally committed config"
git push

# Then change your Navidrome password, since it was exposed
```

For a more thorough scrub of git history, use `git filter-repo` or BFG Repo-Cleaner.

## License

Based on Giocoso by Howard Rogers, licensed under the GNU General Public License v2.0. This derivative work is distributed under the same license.

## Credits

- **Howard Rogers** — Original Giocoso concept, design, and implementation
- **Navidrome** — Self-hosted music streaming server
- **Subsonic API** — Open REST API standard for music streaming
