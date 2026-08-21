# architecture_law_v2.md — Contrato de Arquitectura (v2)

**Proyecto:** `descargaryoutubemusic`  
**Tipo:** Contrato de arquitectura **VIGENTE E INTOCABLE (v2)**  
**Versión:** 2.0 — extensión incremental sobre `architecture_law.md` (v1)  
**Idioma del producto:** Español (UI y documentación de usuario)  
**Fecha de fijación:** 2026-08-21  

### Relación con v1
- `architecture_law.md` (v1) permanece como contrato histórico de la app de **canciones sueltas**.
- **Este archivo (v2)** es el contrato de arquitectura del **proyecto completo**: canciones + playlists/álbumes, stack compartido y reglas comunes.
- Ante conflicto entre v1 y v2 sobre el alcance total del repo, **prevalece v2** para el estado actual del producto.
- v1 **no se edita** salvo el procedimiento de doble confirmación sobre ese archivo concreto.

---

## 0. Cláusula de inmutabilidad (SUPREMA)

1. Este documento (`architecture_law_v2.md`) es la **ley de arquitectura v2** del proyecto.
2. Es **intocable** por defecto: ningún agente, asistente, colaborador ni refactor “por mejora” puede alterarlo, reinterpretarlo en silencio ni contradecirlo en código.
3. Solo puede modificarse por **orden única y expresa del propietario del proyecto** (el usuario humano dueño del repo).
4. Esa orden debe estar **confirmada dos (2) veces** de forma explícita en el mismo hilo/conversación o instrucción escrita, por ejemplo:
   - Confirmación 1: «modifica architecture_law_v2.md …»
   - Confirmación 2: «confirmo por segunda vez: modifica architecture_law_v2.md …»
5. Sin esas **dos confirmaciones explícitas**, cualquier cambio a este archivo (o a la arquitectura aquí fijada) está **prohibido**.
6. Si una petición de feature contradice este contrato, **gana este contrato**, salvo que el propietario active el procedimiento de las dos confirmaciones.
7. Crear `architecture_law_v3.md` (u otra versión) no autoriza por sí solo a reescribir v2; cada archivo tiene su propia cláusula y doble confirmación.

---

## 1. Propósito y alcance (proyecto completo)

### 1.1 Qué es
Suite de aplicaciones de escritorio personales en **Python + Tkinter** para descargar audio desde **YouTube / YouTube Music**:

| App | Entrada | Salida |
|---|---|---|
| **Canciones** (`descargar_musica.py`) | Enlaces de pistas | Archivos sueltos en carpeta base |
| **Playlists** (`descargar_playlist.py`) | Enlaces de playlist/álbum | `CarpetaBase / Artista - Álbum (año) /` + pistas |

Ambas:
- Nombre limpio `canción - artista`
- Sin duplicados (skip)
- **Un solo archivo** por pista (audio + carátula + letra embebidas cuando haya fuentes)
- Formatos **MP3** o **Opus (tal cual YouTube)**

### 1.2 Qué NO es (fuera de alcance)
- Producto comercial / tienda de apps / SaaS.
- Cliente oficial de YouTube Music ni sustituto de Premium.
- Descarga de vídeo.
- Extensión de Chrome obligatoria.
- Login/cookies Premium embebidos (techo sin Premium = diseño actual).
- FLAC / lossless.
- Mezclar en una sola UI canciones sueltas + playlists (son **dos apps**; mejora futura solo con enmienda).

### 1.3 Usuario objetivo
Uso personal: biblioteca local, **AIMP**, estudio de inglés con letra, máxima calidad práctica desde YouTube Music, organización por álbum en playlists.

---

## 2. Principios de diseño (leyes)

1. **Necesidad primero:** flujos reales (pista suelta / playlist completa).
2. **Mejora incremental:** ampliar sin romper lo estable.
3. **Dos apps, un núcleo compartido:** la app de canciones **no se reescribe** para añadir playlists; playlists es clon incremental separado.
4. **Integridad del audio Opus:** modo “tal cual YouTube” = remux `-c copy`, no re-encode.
5. **Un archivo por canción:** solo `.mp3` / `.opus`; prohibidos sidecars `.jpg/.png/.lrc/.txt` en la entrega normal.
6. **Extras no tumban la descarga:** fallo de letra/carátula/año → aviso; el audio permanece.
7. **Verificar antes de creer:** auditar descargas, tags, carpetas, un solo archivo.
8. **Simpleza de UI:** URLs, formato, carpeta, descargar/detener, log.

---

## 3. Stack tecnológico (fijado)

| Capa | Tecnología | Rol |
|---|---|---|
| Lenguaje | Python 3 | Runtime |
| UI | Tkinter / ttk | Interfaz gráfica (2 apps) |
| Descarga | yt-dlp (`python -m yt_dlp`) | Audio + listado de playlists |
| Remux / MP3 | ffmpeg | Remux `.opus` (`-c copy`); conversión MP3 |
| Metadatos embebidos | mutagen | Carátula/letra/tags |
| Letras | LRCLIB → lyrics.ovh | Letra embebida |
| Carátula | iTunes → miniatura YouTube | Portada embebida |
| Año de álbum (solo playlists) | YouTube release_* → iTunes → MusicBrainz | Carpeta `(YYYY)` |
| Arranque Windows | `iniciar.bat`, `iniciar_playlist.bat` | Lanzadores |
| Deps | `requirements.txt` | `yt-dlp`, `mutagen` |

### 3.1 Archivos del proyecto (v2)

| Archivo | Rol | Regla |
|---|---|---|
| `descargar_musica.py` | App canciones sueltas | **No se modifica** para features de playlist |
| `descargar_playlist.py` | App playlists/álbumes | Clon + carpetas álbum/año |
| `metadata_extras.py` | Núcleo compartido extras | Usado por ambas apps |
| `iniciar.bat` | Lanza canciones | — |
| `iniciar_playlist.bat` | Lanza playlists | — |
| `requirements.txt` | Dependencias | — |
| `readme.md` | Docs usuario | No sustituye este contrato |
| `architecture_law.md` | Contrato v1 (histórico canciones) | Intocable salvo 2 confirmaciones propias |
| `architecture_law_v2.md` | **Este contrato (proyecto completo)** | Intocable salvo 2 confirmaciones |

---

## 4. App A — Canciones sueltas (`descargar_musica.py`)

### 4.1 Entrada
- Enlaces de **pistas** (uno por línea).
- Portapapeles automático opcional (mismos criterios que v1).
- `--no-playlist`: no expandir `list=` a playlist completa.

### 4.2 Descarga
- Orden, uno a uno; Detener; carpeta base configurable (`Downloads/YouTubeMusic`).
- Skip si existe `canción - artista` en la carpeta base.
- Resumen: Descargadas / Ya estaban / Fallidos.

### 4.3 Formatos
| Modo | Comportamiento | Calidad |
|---|---|---|
| MP3 | `bestaudio` + MP3 quality `0` | Compatible; reconvierte |
| Opus | `bestaudio` → remux `.opus` (`-c copy`) | Máxima fidelidad **sin Premium** |

**Ley Opus:** remux ≠ recomprimir.  
**Techo YT sin Premium:** típicamente `251` Opus ~128–160 kbps. Premium ~256 (`774`/`141`) **fuera de alcance** sin enmienda.

### 4.4 Archivo y extras
- Nombre: `canción - artista.ext`
- Tras descarga nueva: remux si hace falta → carátula (iTunes → thumb YT) → letra (LRCLIB → lyrics.ovh) → embeber → verificar → borrar sidecars.
- **1 solo archivo** por pista.

---

## 5. App B — Playlists / álbumes (`descargar_playlist.py`)

### 5.1 Entrada
- Enlaces de **playlist o álbum** (uno o varios, uno por línea).
- Misma UI base: portapapeles, MP3/Opus, carpeta base, Descargar/Detener, log.
- **No modifica** `descargar_musica.py`; reutiliza helpers y `metadata_extras`.

### 5.2 Flujo de descarga (fijado)
1. Por cada URL de playlist: `yt-dlp -J --flat-playlist` (sin `--no-playlist`).
2. Resolver pistas (`entries`) → URL `watch?v=ID` por pista.
3. Crear carpeta de álbum bajo la carpeta base.
4. Descargar **pista a pista** (mismo pipeline de calidad/extras/skip que la app de canciones), dentro de esa carpeta.
5. Progress sobre el total de pistas; resumen global al final.

### 5.3 Estructura de carpetas (ley de naming v2)
```
CarpetaBase / Artista - Álbum (YYYY) / canción - artista.opus|.mp3
```

Ejemplo:
```
Downloads/YouTubeMusic/Pink Floyd - The Dark Side of the Moon (1973)/Time - Pink Floyd.opus
```

**Reglas de nombre de carpeta:**
- Artista: metadatos playlist (limpiar sufijo ` - Topic`).
- Álbum: campo álbum → si no, título de playlist (sin duplicar artista).
- Año (cascada):
  1. YouTube `release_year` / `release_date` (álbum) — **no** usar `upload_date` como año de álbum.
  2. iTunes Search (entity album).
  3. MusicBrainz (release-group, con reintentos ante 429/503).
  4. Si no hay año → `Artista - Álbum` **sin** paréntesis.
- Sanitizado Windows (`clean_filename`).

### 5.4 Por cada pista (igual que App A)
- Nombre `canción - artista`
- Skip si ya existe en la carpeta del álbum
- MP3 u Opus según selector
- 1 archivo; carátula + letra embebidas; sin sidecars

### 5.5 Aislamiento
- Prohibido “meter playlists dentro de `descargar_musica.py`” sin enmienda de este contrato.
- Prohibido romper `--no-playlist` de la app de canciones.

---

## 6. Arquitectura lógica (v2)

```
                    ┌─────────────────────────┐
                    │   metadata_extras.py    │
                    │ cover/lyrics/remux/embed│
                    └───────────▲─────────────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         │                      │                      │
┌────────┴────────┐    ┌────────┴────────┐
│ descargar_musica│    │descargar_playlist│
│  (pistas)       │    │  (playlists)     │
│ --no-playlist   │    │ flat-playlist    │
│ carpeta base    │    │ Artista-Álbum(y) │
└────────┬────────┘    └────────┬─────────┘
         │                      │
    iniciar.bat           iniciar_playlist.bat
```

- UI no bloqueante (hilo worker).
- Extras nunca invalidan un audio ya descargado con éxito.

---

## 7. Decisiones históricas fijadas (no reabrir sin 2 confirmaciones de v2)

1. WebM no es entrega final para carátula embebida → entrega Opus.
2. Siempre **1 archivo** por pista; sin sidecars.
3. Portapapeles automático = opcional.
4. Calidad Opus = mejor sin Premium cookies.
5. Herramienta personal, no producto de tienda.
6. **Playlists = app separada**, no fusión en la app de canciones.
7. Carpeta de álbum = `Artista - Álbum (año)` con cascada YT → iTunes → MusicBrainz.
8. `upload_date` de YouTube **no** define el año del álbum.

---

## 8. Reglas para agentes / colaboradores

1. Leer **este** archivo antes de cambiar arquitectura, formatos, playlists, metadatos o deps núcleo.
2. Prohibido tocar `descargar_musica.py` para features de playlist.
3. Prohibido quitar Opus, reintroducir sidecars, o re-encodear Opus “por comodidad”.
4. Prohibido Premium/cookies, extensiones obligatorias, o rewrite total sin **2 confirmaciones** sobre `architecture_law_v2.md`.
5. Mejoras incrementales **dentro** del alcance de v2 están permitidas si no violan este documento.
6. Ante duda: no modificar este archivo; preguntar al propietario.

---

## 9. Criterio de éxito (v2)

### App canciones
- [ ] Descarga múltiples pistas en orden
- [ ] MP3 / Opus; Opus sin re-encode
- [ ] `canción - artista`; skip; 1 archivo; tags verificables
- [ ] `--no-playlist` intacto

### App playlists
- [ ] Acepta URLs de playlist/álbum
- [ ] Crea `Artista - Álbum (año)` (o sin año si no hay fuente)
- [ ] Descarga pistas una a una en esa carpeta
- [ ] Mismos estándares de calidad/extras/skip/1 archivo
- [ ] No modifica `descargar_musica.py`

### Compartido
- [ ] Fallo de letra/carátula/año no borra el audio
- [ ] Lanzadores `.bat` correctos

---

## 10. Procedimiento formal de enmienda (v2)

1. El propietario declara qué cláusula de **v2** quiere cambiar y por qué.
2. **Confirmación 1:** orden explícita de modificar `architecture_law_v2.md`.
3. **Confirmación 2:** “confirmo por segunda vez…” referida a la misma enmienda.
4. Solo entonces se edita este archivo y, si aplica, el código.
5. Registrar la enmienda en el historial siguiente.

### Historial de enmiendas (v2)
| Fecha | Cambio | Autorización |
|---|---|---|
| 2026-08-21 | Creación v2: incorpora v1 + app playlists, carpetas `Artista - Álbum (año)`, cascada de año, aislamiento de apps | Orden del propietario al crear `architecture_law_v2.md` |

---

## 11. Declaración final

`architecture_law_v2.md` constituye el **contrato de arquitectura vinculante del proyecto completo** (canciones + playlists).  
Código, readme, issues y sugerencias de IA se interpretan **a la luz de este documento**.  
**Sin dos confirmaciones expresas del propietario, este contrato no se toca.**
