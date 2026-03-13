"""
Subsonic/OpenSubsonic API client for Navidrome.

Implements the subset of the Subsonic REST API needed by PyGioNav:
  - Authentication (token-based, API 1.13+)
  - Browsing (getArtists, getArtist, getAlbum, getGenres, search3)
  - Library/folder enumeration (getMusicFolders)
  - Album lists (getAlbumList2, getRandomSongs)
  - Streaming (stream, download, getCoverArt)
  - Annotation (scrobble)
"""

import hashlib
import logging
import os
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_VERSION = "1.16.1"
CLIENT_NAME = "pygionav"
NS = {"sub": "http://subsonic.org/restapi"}

log = logging.getLogger("pygionav.api")


# ------------------------------------------------------------------ #
# Data classes
# ------------------------------------------------------------------ #

@dataclass
class Song:
    """A single track."""
    id: str = ""
    title: str = ""
    album: str = ""
    album_id: str = ""
    artist: str = ""
    artist_id: str = ""
    genre: str = ""
    duration: int = 0           # seconds
    track: int = 0
    disc: int = 0
    year: int = 0
    suffix: str = ""
    bitrate: int = 0
    content_type: str = ""
    cover_art: str = ""
    path: str = ""

    @classmethod
    def from_xml(cls, elem: ET.Element) -> "Song":
        return cls(
            id=elem.get("id", ""),
            title=elem.get("title", ""),
            album=elem.get("album", ""),
            album_id=elem.get("albumId", ""),
            artist=elem.get("artist", ""),
            artist_id=elem.get("artistId", ""),
            genre=elem.get("genre", ""),
            duration=int(elem.get("duration", 0)),
            track=int(elem.get("track", 0)),
            disc=int(elem.get("discNumber", 0)),
            year=int(elem.get("year", 0)),
            suffix=elem.get("suffix", ""),
            bitrate=int(elem.get("bitRate", 0)),
            content_type=elem.get("contentType", ""),
            cover_art=elem.get("coverArt", ""),
            path=elem.get("path", ""),
        )


@dataclass
class Album:
    """An album."""
    id: str = ""
    name: str = ""
    artist: str = ""
    artist_id: str = ""
    genre: str = ""
    year: int = 0
    duration: int = 0           # seconds
    song_count: int = 0
    cover_art: str = ""
    songs: List[Song] = field(default_factory=list)

    @classmethod
    def from_xml(cls, elem: ET.Element) -> "Album":
        return cls(
            id=elem.get("id", ""),
            name=elem.get("name", elem.get("title", "")),
            artist=elem.get("artist", ""),
            artist_id=elem.get("artistId", ""),
            genre=elem.get("genre", ""),
            year=int(elem.get("year", 0)),
            duration=int(elem.get("duration", 0)),
            song_count=int(elem.get("songCount", 0)),
            cover_art=elem.get("coverArt", ""),
        )


@dataclass
class Artist:
    """An artist."""
    id: str = ""
    name: str = ""
    album_count: int = 0

    @classmethod
    def from_xml(cls, elem: ET.Element) -> "Artist":
        return cls(
            id=elem.get("id", ""),
            name=elem.get("name", ""),
            album_count=int(elem.get("albumCount", 0)),
        )


@dataclass
class MusicFolder:
    """A Navidrome library / music folder."""
    id: str = ""
    name: str = ""

    @classmethod
    def from_xml(cls, elem: ET.Element) -> "MusicFolder":
        return cls(id=elem.get("id", ""), name=elem.get("name", ""))


class SubsonicError(Exception):
    """Raised when the Subsonic API returns an error."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Subsonic error {code}: {message}")


# ------------------------------------------------------------------ #
# Client
# ------------------------------------------------------------------ #

class SubsonicClient:
    """Client for the Subsonic REST API (targeting Navidrome)."""

    def __init__(self, server_url: str, username: str, password: str,
                 timeout: int = 30):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1,
                      status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _auth_params(self) -> Dict[str, str]:
        salt = secrets.token_hex(12)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "xml",
        }

    def _url(self, endpoint: str) -> str:
        return f"{self.server_url}/rest/{endpoint}"

    # ---- low-level ------------------------------------------------ #

    def _get(self, endpoint: str, **params) -> ET.Element:
        all_params = self._auth_params()
        all_params.update({k: str(v) for k, v in params.items() if v is not None})
        url = self._url(endpoint)
        log.debug("GET %s params=%s", url, {k: v for k, v in all_params.items()
                                             if k not in ("t", "s", "p")})
        resp = self.session.get(url, params=all_params, timeout=self.timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        if root.get("status") != "ok":
            err = root.find("sub:error", NS) or root.find("error")
            code = int(err.get("code", 0)) if err is not None else 0
            msg = err.get("message", "Unknown error") if err is not None else "Unknown"
            raise SubsonicError(code, msg)
        return root

    def _get_binary(self, endpoint: str, **params) -> requests.Response:
        all_params = self._auth_params()
        all_params.update({k: str(v) for k, v in params.items() if v is not None})
        url = self._url(endpoint)
        log.debug("GET (binary) %s id=%s", url, params.get("id", "?"))
        resp = self.session.get(url, params=all_params, timeout=self.timeout,
                                stream=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "xml" in ct or "json" in ct:
            root = ET.fromstring(resp.content)
            err = root.find("sub:error", NS) or root.find("error")
            code = int(err.get("code", 0)) if err is not None else 0
            msg = err.get("message", "Stream error") if err is not None else "Stream error"
            raise SubsonicError(code, msg)
        return resp

    # ---- system --------------------------------------------------- #

    def ping(self) -> bool:
        self._get("ping")
        return True

    # ---- music folders / libraries -------------------------------- #

    def get_music_folders(self) -> List[MusicFolder]:
        """Return all music folders (libraries) on the server."""
        root = self._get("getMusicFolders")
        folders: List[MusicFolder] = []
        for elem in root.iter():
            if elem.tag.endswith("musicFolder") and elem.get("id"):
                folders.append(MusicFolder.from_xml(elem))
        log.debug("Music folders: %s",
                  [(f.id, f.name) for f in folders])
        return folders

    def resolve_library_id(self, library_name: str) -> Optional[str]:
        """Look up a library name and return its ID, or None if not found.

        The match is case-insensitive.
        """
        if not library_name:
            return None
        folders = self.get_music_folders()
        for f in folders:
            if f.name.lower() == library_name.lower():
                log.debug("Resolved library %r → id=%s", library_name, f.id)
                return f.id
        log.warning("Library %r not found on server", library_name)
        return None

    # ---- browsing ------------------------------------------------- #

    def get_artists(self, music_folder_id: Optional[str] = None) -> List[Artist]:
        params: Dict[str, Any] = {}
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        root = self._get("getArtists", **params)
        artists: List[Artist] = []
        for elem in root.iter():
            if elem.tag.endswith("artist") and elem.get("id"):
                artists.append(Artist.from_xml(elem))
        return artists

    def get_artist(self, artist_id: str) -> List[Album]:
        root = self._get("getArtist", id=artist_id)
        albums: List[Album] = []
        for elem in root.iter():
            if elem.tag.endswith("album") and elem.get("id"):
                albums.append(Album.from_xml(elem))
        return albums

    def get_album(self, album_id: str) -> Album:
        root = self._get("getAlbum", id=album_id)
        album_elem = None
        for elem in root.iter():
            if elem.tag.endswith("album"):
                album_elem = elem
                break
        if album_elem is None:
            raise SubsonicError(70, f"Album {album_id} not found")

        album = Album.from_xml(album_elem)
        for child in album_elem:
            if child.tag.endswith("song") and child.get("id"):
                album.songs.append(Song.from_xml(child))
        album.songs.sort(key=lambda s: (s.disc, s.track))
        return album

    def get_genres(self) -> List[Dict[str, Any]]:
        root = self._get("getGenres")
        genres: List[Dict[str, Any]] = []
        for elem in root.iter():
            if elem.tag.endswith("genre"):
                genres.append({
                    "name": elem.text or "",
                    "song_count": int(elem.get("songCount", 0)),
                    "album_count": int(elem.get("albumCount", 0)),
                })
        return genres

    def get_song(self, song_id: str) -> Song:
        root = self._get("getSong", id=song_id)
        for elem in root.iter():
            if elem.tag.endswith("song") and elem.get("id"):
                return Song.from_xml(elem)
        raise SubsonicError(70, f"Song {song_id} not found")

    # ---- album / song lists -------------------------------------- #

    def get_album_list(self, list_type: str = "random", size: int = 500,
                       offset: int = 0, genre: Optional[str] = None,
                       from_year: Optional[int] = None,
                       to_year: Optional[int] = None,
                       music_folder_id: Optional[str] = None) -> List[Album]:
        params: Dict[str, Any] = {
            "type": list_type, "size": size, "offset": offset,
        }
        if genre:
            params["genre"] = genre
        if from_year is not None:
            params["fromYear"] = from_year
        if to_year is not None:
            params["toYear"] = to_year
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        root = self._get("getAlbumList2", **params)
        albums: List[Album] = []
        for elem in root.iter():
            if elem.tag.endswith("album") and elem.get("id"):
                albums.append(Album.from_xml(elem))
        return albums

    def get_random_songs(self, size: int = 50,
                         genre: Optional[str] = None,
                         from_year: Optional[int] = None,
                         to_year: Optional[int] = None,
                         music_folder_id: Optional[str] = None) -> List[Song]:
        params: Dict[str, Any] = {"size": size}
        if genre:
            params["genre"] = genre
        if from_year is not None:
            params["fromYear"] = from_year
        if to_year is not None:
            params["toYear"] = to_year
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        root = self._get("getRandomSongs", **params)
        songs: List[Song] = []
        for elem in root.iter():
            if elem.tag.endswith("song") and elem.get("id"):
                songs.append(Song.from_xml(elem))
        return songs

    # ---- search --------------------------------------------------- #

    def search(self, query: str, artist_count: int = 20,
               album_count: int = 20, song_count: int = 20,
               music_folder_id: Optional[str] = None) -> Dict[str, list]:
        params: Dict[str, Any] = {
            "query": query, "artistCount": artist_count,
            "albumCount": album_count, "songCount": song_count,
        }
        if music_folder_id:
            params["musicFolderId"] = music_folder_id
        root = self._get("search3", **params)
        result: Dict[str, list] = {"artists": [], "albums": [], "songs": []}
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "artist" and elem.get("id"):
                result["artists"].append(Artist.from_xml(elem))
            elif tag == "album" and elem.get("id"):
                result["albums"].append(Album.from_xml(elem))
            elif tag == "song" and elem.get("id"):
                result["songs"].append(Song.from_xml(elem))
        return result

    # ---- streaming / downloading ---------------------------------- #

    def stream_url(self, song_id: str, fmt: Optional[str] = None,
                   max_bitrate: Optional[int] = None) -> str:
        """Build a full authenticated stream URL for a song."""
        params = self._auth_params()
        params["id"] = song_id
        if fmt and fmt != "raw":
            params["format"] = fmt
        if max_bitrate and max_bitrate > 0:
            params["maxBitRate"] = str(max_bitrate)
        return f"{self._url('stream')}?{urlencode(params)}"

    def download_song(self, song_id: str, dest_path: str,
                      fmt: Optional[str] = None,
                      max_bitrate: Optional[int] = None) -> bool:
        """Download a song to a local file.  Returns True on success."""
        try:
            params: Dict[str, Any] = {"id": song_id}
            if fmt and fmt != "raw":
                params["format"] = fmt
            if max_bitrate and max_bitrate > 0:
                params["maxBitRate"] = max_bitrate
            resp = self._get_binary("stream", **params)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            log.debug("Downloaded song %s → %s (%d bytes)",
                      song_id, dest_path, os.path.getsize(dest_path))
            return True
        except Exception as exc:
            log.error("Failed to download song %s: %s", song_id, exc)
            return False

    def cover_art_url(self, cover_id: str, size: int = 450) -> str:
        params = self._auth_params()
        params["id"] = cover_id
        params["size"] = str(size)
        return f"{self._url('getCoverArt')}?{urlencode(params)}"

    def download_cover_art(self, cover_id: str, dest_path: str,
                           size: int = 450) -> bool:
        try:
            resp = self._get_binary("getCoverArt", id=cover_id, size=size)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as exc:
            log.error("Failed to download cover art %s: %s", cover_id, exc)
            return False

    # ---- annotation ----------------------------------------------- #

    def scrobble(self, song_id: str, submission: bool = True) -> bool:
        try:
            self._get("scrobble", id=song_id,
                       submission="true" if submission else "false")
            return True
        except SubsonicError:
            return False

