"""
Extras de metadatos: carátula y letra embebidas en un solo archivo (MP3/Opus).
Sin sidecars .jpg/.lrc/.txt.
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

USER_AGENT = (
    "descargaryoutubemusic/1.10 "
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


_LRC_TS = re.compile(
    r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]"
)


def is_lrc_text(text: str) -> bool:
    if not text:
        return False
    return bool(_LRC_TS.search(text))


def lrc_timestamps_seconds(text: str) -> list[float]:
    out: list[float] = []
    for m in _LRC_TS.finditer(text or ""):
        mm = int(m.group(1))
        ss = int(m.group(2))
        frac = m.group(3) or "0"
        # normalizar a fracción 0-1
        if len(frac) == 1:
            frac_f = int(frac) / 10.0
        elif len(frac) == 2:
            frac_f = int(frac) / 100.0
        else:
            frac_f = int(frac[:3]) / 1000.0
        out.append(mm * 60 + ss + frac_f)
    return out


def lyrics_content_lines(text: str) -> list[str]:
    """Líneas con contenido (sin timestamps vacíos)."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # quitar tags LRC para contar texto real
        plain = _LRC_TS.sub("", line).strip()
        if plain:
            lines.append(plain)
    return lines


def score_lyrics_candidate(
    text: str,
    *,
    source: str,
    duration: float | None = None,
    synced_preferred: bool = True,
) -> int:
    """
    Puntuación de calidad. Más alto = mejor.
    Penaliza LRC con duración desalineada o texto demasiado corto.
    """
    if not text or not text.strip():
        return -10_000
    lines = lyrics_content_lines(text)
    n_lines = len(lines)
    n_chars = sum(len(x) for x in lines)
    synced = is_lrc_text(text)
    score = 0

    # Completitud básica
    score += min(n_lines, 120) * 3
    score += min(n_chars // 20, 80)

    if n_lines < 6:
        score -= 40
    if n_chars < 80:
        score -= 30

    if synced:
        score += 50 if synced_preferred else 10
        stamps = lrc_timestamps_seconds(text)
        if len(stamps) >= 4:
            span = max(stamps) - min(stamps)
            score += min(int(span), 400) // 5
            if duration and duration > 0:
                # cubrir buena parte de la canción y no pasarse mucho
                cover = span / duration
                if 0.55 <= cover <= 1.15:
                    score += 80
                elif 0.35 <= cover < 0.55:
                    score += 20
                else:
                    score -= 60
                # último timestamp cerca del final
                end_gap = abs(max(stamps) - duration)
                if end_gap <= 8:
                    score += 25
                elif end_gap > 45:
                    score -= 40
        else:
            score -= 30  # LRC pobre
    else:
        # plano: útil pero no auto-scroll
        score += 5

    # Preferencias suaves por fuente
    src = source.casefold()
    if "youtube" in src and synced:
        score += 15
    if src == "lrclib" and synced:
        score += 10
    if src == "lyrics.ovh":
        score -= 5  # a menudo incompleto

    return score


def lyrics_quality_ok(
    text: str, duration: float | None = None, *, min_lines: int = 8
) -> bool:
    if score_lyrics_candidate(text, source="check", duration=duration) < 20:
        return False
    n_lines = len(lyrics_content_lines(text))
    # LRC bien alineado puede tener menos líneas “densas”
    effective_min = min_lines
    if is_lrc_text(text):
        effective_min = min(min_lines, 5)
    if n_lines < effective_min:
        if duration and duration < 90:
            return n_lines >= 4
        return False
    if is_lrc_text(text) and duration and duration > 0:
        stamps = lrc_timestamps_seconds(text)
        if len(stamps) < 4:
            return False
        span = max(stamps) - min(stamps)
        if span < duration * 0.35:
            return False
    return True


def _vtt_to_lrc(vtt: str) -> str | None:
    """Convierte WEBVTT simple a LRC."""
    import re as _re

    lines_out: list[str] = []
    # 00:00:01.000 --> 00:00:04.000
    cue = _re.compile(
        r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\.(\d{3})\s*-->\s*"
        r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\.(\d{3})"
    )
    blocks = _re.split(r"\n\s*\n", vtt.replace("\r\n", "\n"))
    for block in blocks:
        blines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not blines:
            continue
        if blines[0].upper().startswith("WEBVTT"):
            continue
        time_line = None
        text_lines: list[str] = []
        for ln in blines:
            if "-->" in ln:
                time_line = ln
            elif time_line is not None and not ln.isdigit():
                # quitar tags tipo <c>
                cleaned = _re.sub(r"<[^>]+>", "", ln).strip()
                if cleaned:
                    text_lines.append(cleaned)
        if not time_line or not text_lines:
            continue
        m = cue.search(time_line)
        if not m:
            continue
        hh = int(m.group(1) or 0)
        mm = int(m.group(2))
        ss = int(m.group(3))
        ms = int(m.group(4))
        total_mm = hh * 60 + mm
        cs = ms // 10  # centésimas
        text = " ".join(text_lines)
        lines_out.append(f"[{total_mm:02d}:{ss:02d}.{cs:02d}]{text}")
    if len(lines_out) < 3:
        return None
    return "\n".join(lines_out)


def _pick_subtitle_track(tracks: object) -> dict | None:
    if not isinstance(tracks, list):
        return None
    # Preferir vtt / srv3 / json3
    preferred_ext = ("vtt", "srv3", "json3", "ttml", "srv1", "srv2")
    ranked: list[tuple[int, dict]] = []
    for t in tracks:
        if not isinstance(t, dict) or not t.get("url"):
            continue
        ext = str(t.get("ext") or "").casefold()
        try:
            prio = preferred_ext.index(ext)
        except ValueError:
            prio = 50
        ranked.append((prio, t))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def extract_youtube_lyrics_from_info(info: dict) -> tuple[str | None, str]:
    """
    Intenta letra/subtítulos desde metadatos yt-dlp (subtitles / automatic_captions).
    Prefer manual > auto; idiomas es/en primero.
    """
    if not isinstance(info, dict):
        return None, "none"

    lang_pref = (
        "es",
        "es-419",
        "es-ES",
        "en",
        "en-US",
        "en-GB",
    )

    def _from_map(submap: object, label: str) -> tuple[str | None, str]:
        if not isinstance(submap, dict) or not submap:
            return None, "none"
        # ordenar idiomas
        keys = list(submap.keys())
        ordered: list[str] = []
        for pref in lang_pref:
            for k in keys:
                if str(k).casefold() == pref.casefold() or str(k).casefold().startswith(
                    pref.casefold() + "-"
                ):
                    if k not in ordered:
                        ordered.append(k)
        for k in keys:
            if k not in ordered:
                ordered.append(k)

        for lang in ordered:
            track = _pick_subtitle_track(submap.get(lang))
            if not track:
                continue
            url = str(track.get("url") or "")
            ext = str(track.get("ext") or "").casefold()
            if not url:
                continue
            try:
                raw = _http_get(url, timeout=25).decode("utf-8", errors="replace")
            except Exception:
                continue
            text: str | None = None
            if ext == "vtt" or "WEBVTT" in raw[:80].upper():
                text = _vtt_to_lrc(raw)
            elif ext in {"srv3", "json3"}:
                # a veces JSON de YouTube; intento extracciones simples
                try:
                    data = json.loads(raw)
                    events = data.get("events") if isinstance(data, dict) else None
                    if isinstance(events, list):
                        lrc_lines: list[str] = []
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            t_ms = ev.get("tStartMs")
                            segs = ev.get("segs")
                            if t_ms is None or not isinstance(segs, list):
                                continue
                            parts = [
                                str(s.get("utf8") or "")
                                for s in segs
                                if isinstance(s, dict)
                            ]
                            body = "".join(parts).replace("\n", " ").strip()
                            if not body or body == "\n":
                                continue
                            total_s = int(t_ms) / 1000.0
                            mm = int(total_s // 60)
                            ss = int(total_s % 60)
                            cs = int((total_s - int(total_s)) * 100)
                            lrc_lines.append(f"[{mm:02d}:{ss:02d}.{cs:02d}]{body}")
                        if len(lrc_lines) >= 3:
                            text = "\n".join(lrc_lines)
                except Exception:
                    text = None
            else:
                # texto plano residual
                plain = "\n".join(
                    ln.strip()
                    for ln in raw.splitlines()
                    if ln.strip()
                    and not ln.strip().isdigit()
                    and "-->" not in ln
                    and not ln.upper().startswith("WEBVTT")
                )
                if len(lyrics_content_lines(plain)) >= 6:
                    text = plain

            if text and lyrics_quality_ok(text, None, min_lines=4):
                return text, f"{label}:{lang}"
        return None, "none"

    text, src = _from_map(info.get("subtitles"), "youtube-sub")
    if text:
        return text, src
    text, src = _from_map(info.get("automatic_captions"), "youtube-auto")
    if text:
        return text, src
    return None, "none"


def fetch_lrclib_lyrics(
    artist: str, track: str, album: str = "", duration: float | None = None
) -> str | None:
    """Mejor candidato LRCLIB (no el primero a ciegas)."""
    if not artist or not track:
        return None

    candidates: list[tuple[int, str]] = []

    def _consider(item: dict) -> None:
        text = _lyrics_from_lrclib_item(item)
        if not text:
            return
        item_dur = item.get("duration")
        try:
            item_dur_f = float(item_dur) if item_dur is not None else None
        except (TypeError, ValueError):
            item_dur_f = None
        # bonus si duration del item calza
        sc = score_lyrics_candidate(
            text, source="lrclib", duration=duration or item_dur_f
        )
        if duration and item_dur_f and duration > 0:
            gap = abs(item_dur_f - duration)
            if gap <= 2:
                sc += 40
            elif gap <= 5:
                sc += 15
            elif gap > 20:
                sc -= 50
        candidates.append((sc, text))

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
            _consider(data)
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
                for item in data[:12]:
                    if isinstance(item, dict):
                        _consider(item)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ):
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def fetch_lyrics_ovh(artist: str, track: str) -> str | None:
    """Fallback simple y público (plano)."""
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
    artist: str,
    track: str,
    album: str = "",
    duration: float | None = None,
    info: dict | None = None,
) -> tuple[str | None, str]:
    """
    Cascada con score:
    1) YouTube subtitles/auto (si hay en info)
    2) LRCLIB (mejor match por duración/completitud)
    3) lyrics.ovh
    Elige el mejor; si el top no pasa calidad, prueba el siguiente.
    """
    ranked: list[tuple[int, str, str]] = []  # score, text, source

    if info:
        yt_text, yt_src = extract_youtube_lyrics_from_info(info)
        if yt_text:
            ranked.append(
                (
                    score_lyrics_candidate(
                        yt_text, source=yt_src, duration=duration
                    ),
                    yt_text,
                    yt_src,
                )
            )

    lrclib = fetch_lrclib_lyrics(artist, track, album, duration)
    if lrclib:
        ranked.append(
            (
                score_lyrics_candidate(
                    lrclib, source="lrclib", duration=duration
                ),
                lrclib,
                "lrclib",
            )
        )

    ovh = fetch_lyrics_ovh(artist, track)
    if ovh:
        ranked.append(
            (
                score_lyrics_candidate(
                    ovh, source="lyrics.ovh", duration=duration
                ),
                ovh,
                "lyrics.ovh",
            )
        )

    if not ranked:
        return None, "none"

    ranked.sort(key=lambda t: t[0], reverse=True)

    for sc, text, src in ranked:
        if lyrics_quality_ok(text, duration):
            return text, f"{src} (score={sc})"
    # último recurso: el de mayor score aunque no pase quality_ok estricto
    best_sc, best_text, best_src = ranked[0]
    if best_sc >= 10:
        return best_text, f"{best_src} (score={best_sc}, laxo)"
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


def verify_lyrics_text_quality(
    text: str | None, duration: float | None = None
) -> bool:
    """Verificación de contenido (no solo presencia de tag)."""
    if not text:
        return False
    return lyrics_quality_ok(text, duration)


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

        lyrics, lyrics_source = fetch_lyrics(
            artist, track, album, duration_f, info=info
        )
        if lyrics:
            kind = "LRC (auto-scroll)" if is_lrc_text(lyrics) else "plana (manual)"
            log(f"Letra: {lyrics_source} [{kind}]")
        else:
            log("AVISO: no se encontró letra de calidad suficiente")

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
            if verify_embedded_lyrics(audio_path) and verify_lyrics_text_quality(
                lyrics, duration_f
            ):
                log("Letra embebida OK (sin archivo .lrc/.txt)")
            elif verify_embedded_lyrics(audio_path):
                log("AVISO: letra embebida pero calidad dudosa (corta o sync floja)")
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
