"""
Extras de metadatos: carátula (álbum → miniatura YT) y letra (LRCLIB).
No altera el audio descargado; solo sidecars / tags MP3.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

USER_AGENT = "descargaryoutubemusic/1.03 (local; +https://github.com/omarSuarezRodriguez/descargaryoutubemusic)"


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_json(url: str, timeout: float = 20.0) -> object:
    raw = _http_get(url, timeout=timeout)
    return json.loads(raw.decode("utf-8", errors="replace"))


def meta_artist(info: dict) -> str:
    artist = (
        info.get("artist")
        or info.get("album_artist")
        or info.get("creator")
        or ""
    )
    if isinstance(artist, list):
        artist = ", ".join(str(a) for a in artist if a)
    artist = str(artist).strip()
    if artist:
        return artist
    title = (info.get("title") or "").strip()
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    return (info.get("uploader") or "").strip()


def meta_track(info: dict) -> str:
    track = (info.get("track") or "").strip()
    if track:
        return track
    artist = meta_artist(info)
    title = (info.get("title") or "").strip()
    if artist and title.casefold().startswith(f"{artist.casefold()} - "):
        return title[len(artist) + 3 :].strip()
    if " - " in title:
        return title.split(" - ", 1)[1].strip()
    return title


def meta_album(info: dict) -> str:
    album = info.get("album") or info.get("playlist") or ""
    if isinstance(album, list):
        album = album[0] if album else ""
    return str(album).strip()


def youtube_thumbnail_url(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    best_url = None
    best_area = -1
    for thumb in thumbs:
        if not isinstance(thumb, dict):
            continue
        url = thumb.get("url")
        if not url:
            continue
        w = int(thumb.get("width") or 0)
        h = int(thumb.get("height") or 0)
        area = w * h
        if area >= best_area:
            best_area = area
            best_url = url
    return best_url or (info.get("thumbnail") or None)


def fetch_itunes_cover_url(artist: str, track: str, album: str = "") -> str | None:
    """Busca carátula de álbum vía iTunes Search API."""
    term = " ".join(p for p in (artist, track) if p).strip()
    if not term:
        return None
    query = urllib.parse.urlencode(
        {"term": term, "entity": "song", "limit": 8, "media": "music"}
    )
    data = _http_get_json(f"https://itunes.apple.com/search?{query}")
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    track_cf = track.casefold()
    artist_cf = artist.casefold()
    album_cf = album.casefold()

    def score(item: dict) -> int:
        s = 0
        if artist_cf and artist_cf in str(item.get("artistName") or "").casefold():
            s += 3
        if track_cf and track_cf in str(item.get("trackName") or "").casefold():
            s += 4
        if album_cf and album_cf in str(item.get("collectionName") or "").casefold():
            s += 2
        return s

    ranked = sorted(
        (r for r in results if isinstance(r, dict) and r.get("artworkUrl100")),
        key=score,
        reverse=True,
    )
    if not ranked or score(ranked[0]) < 3:
        # Intento con álbum si hay
        if album and artist:
            query2 = urllib.parse.urlencode(
                {
                    "term": f"{artist} {album}",
                    "entity": "album",
                    "limit": 5,
                    "media": "music",
                }
            )
            data2 = _http_get_json(f"https://itunes.apple.com/search?{query2}")
            if isinstance(data2, dict):
                for item in data2.get("results") or []:
                    art = item.get("artworkUrl100")
                    if art:
                        return art.replace("100x100bb", "600x600bb")
        return None
    art = ranked[0]["artworkUrl100"]
    return str(art).replace("100x100bb", "600x600bb")


def fetch_lrclib_lyrics(artist: str, track: str, album: str = "", duration: float | None = None) -> tuple[str | None, str]:
    """
    Devuelve (texto, extensión_sugerida) donde extensión es 'lrc' o 'txt'.
    """
    if not artist or not track:
        return None, "txt"

    params: dict[str, str] = {
        "artist_name": artist,
        "track_name": track,
    }
    if album:
        params["album_name"] = album
    if duration and duration > 0:
        params["duration"] = str(int(round(duration)))

    # 1) get exacto
    try:
        query = urllib.parse.urlencode(params)
        data = _http_get_json(f"https://lrclib.net/api/get?{query}")
        if isinstance(data, dict):
            synced = (data.get("syncedLyrics") or "").strip()
            plain = (data.get("plainLyrics") or "").strip()
            if synced:
                return synced, "lrc"
            if plain:
                return plain, "txt"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass

    # 2) search
    try:
        query = urllib.parse.urlencode(
            {"artist_name": artist, "track_name": track}
        )
        data = _http_get_json(f"https://lrclib.net/api/search?{query}")
        if isinstance(data, list) and data:
            best = data[0]
            if isinstance(best, dict):
                synced = (best.get("syncedLyrics") or "").strip()
                plain = (best.get("plainLyrics") or "").strip()
                if synced:
                    return synced, "lrc"
                if plain:
                    return plain, "txt"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass

    return None, "txt"


def _to_jpeg_bytes(data: bytes, ffmpeg: Path | None) -> bytes | None:
    if data.startswith(b"\xff\xd8\xff"):
        return data
    if data.startswith(b"\x89PNG"):
        # PNG sirve para embeber; para sidecar .jpg convertimos si hay ffmpeg
        if not ffmpeg:
            return data
    if not ffmpeg:
        # WebP u otros sin conversor: devolver raw (sidecar puede no ser .jpg real)
        return data

    import tempfile

    suffix = ".bin"
    if data.startswith(b"\x89PNG"):
        suffix = ".png"
    elif data[:4] == b"RIFF" and b"WEBP" in data[:16]:
        suffix = ".webp"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{suffix}"
        dst = Path(tmp) / "out.jpg"
        src.write_bytes(data)
        proc = subprocess.run(
            [
                str(ffmpeg),
                "-y",
                "-i",
                str(src),
                "-q:v",
                "2",
                str(dst),
            ],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and dst.is_file():
            return dst.read_bytes()
    return data if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG") else None


def download_cover_image(
    artist: str,
    track: str,
    album: str,
    info: dict,
    ffmpeg: Path | None,
    log: LogFn,
) -> tuple[bytes | None, str]:
    """
    Cascada: carátula iTunes → miniatura YouTube Music.
    Devuelve (bytes_imagen, fuente).
    """
    # 1) Oficial / catálogo
    try:
        cover_url = fetch_itunes_cover_url(artist, track, album)
        if cover_url:
            raw = _http_get(cover_url)
            jpeg = _to_jpeg_bytes(raw, ffmpeg)
            if jpeg:
                log("Carátula: catálogo iTunes")
                return jpeg, "itunes"
    except Exception as exc:  # noqa: BLE001
        log(f"Carátula iTunes no disponible ({exc.__class__.__name__})")

    # 2) Miniatura YouTube Music
    try:
        thumb = youtube_thumbnail_url(info)
        if thumb:
            raw = _http_get(thumb)
            jpeg = _to_jpeg_bytes(raw, ffmpeg)
            if jpeg:
                log("Carátula: miniatura YouTube Music (fallback)")
                return jpeg, "youtube"
    except Exception as exc:  # noqa: BLE001
        log(f"Miniatura YouTube no disponible ({exc.__class__.__name__})")

    return None, "none"


def embed_mp3_metadata(
    mp3_path: Path,
    cover_bytes: bytes | None,
    lyrics: str | None,
    artist: str,
    track: str,
    album: str,
) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
    from mutagen.id3 import Encoding

    try:
        tags = ID3(mp3_path)
    except Exception:
        tags = ID3()

    if track:
        tags.add(TIT2(encoding=Encoding.UTF8, text=track))
    if artist:
        tags.add(TPE1(encoding=Encoding.UTF8, text=artist))
    if album:
        tags.add(TALB(encoding=Encoding.UTF8, text=album))

    if cover_bytes:
        mime = "image/jpeg"
        if cover_bytes.startswith(b"\x89PNG"):
            mime = "image/png"
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=Encoding.UTF8,
                mime=mime,
                type=3,
                desc="Cover",
                data=cover_bytes,
            )
        )

    if lyrics:
        tags.delall("USLT")
        tags.add(
            USLT(
                encoding=Encoding.UTF8,
                lang="spa",
                desc="Lyrics",
                text=re.sub(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", "", lyrics).strip()
                or lyrics,
            )
        )

    tags.save(mp3_path, v2_version=3)


def attach_lyrics_and_cover(
    audio_path: Path,
    basename: str,
    info: dict,
    ffmpeg: Path | None,
    log: LogFn,
) -> None:
    """Guarda sidecars y embebe en MP3. Nunca lanza al llamador."""
    try:
        artist = meta_artist(info)
        track = meta_track(info)
        album = meta_album(info)
        duration = info.get("duration")
        try:
            duration_f = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_f = None

        out_dir = audio_path.parent
        cover_path = out_dir / f"{basename}.jpg"

        cover_bytes, _source = download_cover_image(
            artist, track, album, info, ffmpeg, log
        )
        if cover_bytes:
            # Preferir JPEG en disco
            if cover_bytes.startswith(b"\xff\xd8\xff"):
                cover_path.write_bytes(cover_bytes)
                log(f"Carátula guardada: {cover_path.name}")
            elif cover_bytes.startswith(b"\x89PNG"):
                png_path = out_dir / f"{basename}.png"
                png_path.write_bytes(cover_bytes)
                log(f"Carátula guardada: {png_path.name}")
                cover_path = png_path
            else:
                cover_path.write_bytes(cover_bytes)
                log(f"Carátula guardada: {cover_path.name}")
        else:
            log("AVISO: no se encontró carátula (ni iTunes ni miniatura YT)")

        lyrics, ext = fetch_lrclib_lyrics(artist, track, album, duration_f)
        lyrics_path = None
        if lyrics:
            lyrics_path = out_dir / f"{basename}.{ext}"
            lyrics_path.write_text(lyrics, encoding="utf-8")
            log(f"Letra guardada: {lyrics_path.name}")
        else:
            log("AVISO: no se encontró letra")

        if audio_path.suffix.lower() == ".mp3":
            try:
                embed_mp3_metadata(
                    audio_path,
                    cover_bytes,
                    lyrics,
                    artist,
                    track,
                    album,
                )
                log("Metadatos embebidos en MP3 (carátula/letra si había)")
            except Exception as exc:  # noqa: BLE001
                log(f"AVISO: no se pudo embeber en MP3 ({exc})")
    except Exception as exc:  # noqa: BLE001
        log(f"AVISO: extras de letra/carátula fallaron ({exc})")
