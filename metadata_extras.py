"""
Extras de metadatos: carátula y letra embebidas en un solo archivo (MP3/Opus).
Sin sidecars .jpg/.lrc/.txt.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

USER_AGENT = (
    "descargaryoutubemusic/1.04 "
    "(local; +https://github.com/omarSuarezRodriguez/descargaryoutubemusic)"
)


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


def _lyrics_from_lrclib_item(item: dict) -> str | None:
    synced = (item.get("syncedLyrics") or "").strip()
    plain = (item.get("plainLyrics") or "").strip()
    if synced:
        return synced
    if plain:
        return plain
    return None


def fetch_lrclib_lyrics(
    artist: str, track: str, album: str = "", duration: float | None = None
) -> str | None:
    if not artist or not track:
        return None

    params: dict[str, str] = {
        "artist_name": artist,
        "track_name": track,
    }
    if album:
        params["album_name"] = album
    if duration and duration > 0:
        params["duration"] = str(int(round(duration)))

    try:
        query = urllib.parse.urlencode(params)
        data = _http_get_json(f"https://lrclib.net/api/get?{query}")
        if isinstance(data, dict):
            text = _lyrics_from_lrclib_item(data)
            if text:
                return text
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass

    for qparams in (
        {"artist_name": artist, "track_name": track},
        {"q": f"{artist} {track}"},
    ):
        try:
            query = urllib.parse.urlencode(qparams)
            data = _http_get_json(f"https://lrclib.net/api/search?{query}")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    text = _lyrics_from_lrclib_item(item)
                    if text:
                        return text
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            continue

    return None


def fetch_lyrics_ovh(artist: str, track: str) -> str | None:
    """Fallback simple y público."""
    if not artist or not track:
        return None
    try:
        a = urllib.parse.quote(artist)
        t = urllib.parse.quote(track)
        data = _http_get_json(f"https://api.lyrics.ovh/v1/{a}/{t}")
        if isinstance(data, dict):
            text = (data.get("lyrics") or "").strip()
            if text:
                return text
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    return None


def fetch_lyrics(
    artist: str, track: str, album: str = "", duration: float | None = None
) -> tuple[str | None, str]:
    """Cascada de letras. Devuelve (texto, fuente)."""
    text = fetch_lrclib_lyrics(artist, track, album, duration)
    if text:
        return text, "lrclib"
    text = fetch_lyrics_ovh(artist, track)
    if text:
        return text, "lyrics.ovh"
    return None, "none"


def _to_jpeg_bytes(data: bytes, ffmpeg: Path | None) -> bytes | None:
    if data.startswith(b"\xff\xd8\xff"):
        return data
    if not ffmpeg:
        return data if data.startswith(b"\x89PNG") else None

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
    """Cascada: carátula iTunes → miniatura YouTube Music."""
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
        # AIMP: LRC con timestamps dentro de USLT
        tags.delall("USLT")
        tags.add(
            USLT(
                encoding=Encoding.UTF8,
                lang="eng",
                desc="Lyrics",
                text=lyrics,
            )
        )

    tags.save(mp3_path, v2_version=3)


def embed_opus_metadata(
    opus_path: Path,
    cover_bytes: bytes | None,
    lyrics: str | None,
    artist: str,
    track: str,
    album: str,
) -> None:
    """Embebe tags + carátula en .opus (Ogg Opus) para AIMP y similares."""
    import base64

    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus

    audio = OggOpus(opus_path)
    if track:
        audio["title"] = [track]
    if artist:
        audio["artist"] = [artist]
    if album:
        audio["album"] = [album]

    if lyrics:
        audio["lyrics"] = [lyrics]
        audio["unsyncedlyrics"] = [lyrics]

    if cover_bytes:
        pic = Picture()
        pic.data = cover_bytes
        pic.type = 3
        pic.mime = (
            "image/png" if cover_bytes.startswith(b"\x89PNG") else "image/jpeg"
        )
        pic.desc = "Cover"
        encoded = base64.b64encode(pic.write()).decode("ascii")
        audio["metadata_block_picture"] = [encoded]

    audio.save()


def verify_embedded_cover(audio_path: Path) -> bool:
    suffix = audio_path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            tags = ID3(audio_path)
            return any(str(k).startswith("APIC") for k in tags.keys())
        if suffix == ".opus":
            from mutagen.oggopus import OggOpus

            tags = OggOpus(audio_path)
            return bool(tags.get("metadata_block_picture"))
    except Exception:
        return False
    return False


def verify_embedded_lyrics(audio_path: Path) -> bool:
    suffix = audio_path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            tags = ID3(audio_path)
            return any(str(k).startswith("USLT") for k in tags.keys())
        if suffix == ".opus":
            from mutagen.oggopus import OggOpus

            tags = OggOpus(audio_path)
            return bool(tags.get("lyrics") or tags.get("unsyncedlyrics"))
    except Exception:
        return False
    return False


def remux_to_opus(audio_path: Path, ffmpeg: Path, log: LogFn) -> Path:
    """Remuxa a .opus sin re-encodear el audio (misma fidelidad)."""
    if audio_path.suffix.lower() == ".opus":
        return audio_path
    if not ffmpeg or not ffmpeg.is_file():
        raise RuntimeError("ffmpeg no disponible para remux a .opus")

    out_path = audio_path.with_suffix(".opus")
    if out_path.exists():
        out_path.unlink()

    proc = subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-c:a",
            "copy",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not out_path.is_file():
        detail = (proc.stderr or proc.stdout or "")[-300:]
        raise RuntimeError(f"remux opus falló: {detail}")

    try:
        audio_path.unlink()
    except OSError:
        pass
    log(f"Contenedor: {audio_path.suffix} -> .opus (sin reconvertir audio)")
    return out_path


def attach_lyrics_and_cover(
    audio_path: Path,
    basename: str,
    info: dict,
    ffmpeg: Path | None,
    log: LogFn,
) -> Path:
    """
    Embebe carátula y letra en el audio. Siempre 1 archivo (sin sidecars).
    Devuelve la ruta final del audio (puede cambiar .webm -> .opus).
    """
    try:
        artist = meta_artist(info)
        track = meta_track(info)
        album = meta_album(info)
        duration = info.get("duration")
        try:
            duration_f = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_f = None

        suffix = audio_path.suffix.lower()
        if suffix in {".webm", ".mka", ".mkv"} and ffmpeg:
            try:
                audio_path = remux_to_opus(audio_path, ffmpeg, log)
            except Exception as exc:  # noqa: BLE001
                log(f"AVISO: no se pudo pasar a .opus ({exc})")

        cover_bytes, _source = download_cover_image(
            artist, track, album, info, ffmpeg, log
        )
        if not cover_bytes:
            log("AVISO: no se encontró carátula (ni iTunes ni miniatura YT)")

        lyrics, lyrics_source = fetch_lyrics(artist, track, album, duration_f)
        if lyrics:
            log(f"Letra: {lyrics_source}")
        else:
            log("AVISO: no se encontró letra")

        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            try:
                embed_mp3_metadata(
                    audio_path, cover_bytes, lyrics, artist, track, album
                )
            except Exception as exc:  # noqa: BLE001
                log(f"AVISO: no se pudo embeber en MP3 ({exc})")
        elif suffix == ".opus":
            try:
                embed_opus_metadata(
                    audio_path, cover_bytes, lyrics, artist, track, album
                )
            except Exception as exc:  # noqa: BLE001
                log(f"AVISO: no se pudo embeber en Opus ({exc})")
        else:
            log(f"AVISO: formato {suffix} sin embeber nativo de carátula/letra")

        if cover_bytes:
            if verify_embedded_cover(audio_path):
                log("Carátula embebida OK (sin archivo .jpg)")
            else:
                log("AVISO: carátula no quedó embebida")
        if lyrics:
            if verify_embedded_lyrics(audio_path):
                log("Letra embebida OK (sin archivo .lrc/.txt)")
            else:
                log("AVISO: letra no quedó embebida")

        # Limpiar sidecars de versiones anteriores
        out_dir = audio_path.parent
        for extra in (
            out_dir / f"{basename}.jpg",
            out_dir / f"{basename}.png",
            out_dir / f"{basename}.lrc",
            out_dir / f"{basename}.txt",
            out_dir / f"{basename}.webp",
        ):
            if extra.is_file():
                try:
                    extra.unlink()
                    log(f"Eliminado sidecar: {extra.name}")
                except OSError:
                    pass

        log(f"1 archivo listo: {audio_path.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"AVISO: extras de letra/carátula fallaron ({exc})")
    return audio_path
