## v1.10

- Tras cada descarga nueva: busca letra (LRCLIB) y carátula.
- Carátula: catálogo iTunes → si no, miniatura de YouTube Music.
- Guarda `canción - artista.jpg` (+ `.lrc`/`.txt`).
- En MP3 también embebe carátula/letra.
- Si falla letra/carátula, la descarga de audio no se cancela.

### Conclusión
WebM = máxima fidelidad posible desde YouTube Music (el stream original Opus). No hay un “más alto” en esa plataforma para esa canción; el techo lo pone YouTube, no la app.


##############################
## v1.01
La app funciona usando el desbloqueador de clic derecho de las webs, addon, llamado Allow Right Click
Re-enable right-click on websites that overwrite it


#####################################
## v1.02

Qué añadí
Selector circular en la app:

MP3 (compatible) — método actual (por defecto)
WebM (tal cual YouTube) — sin reconvertir (Opus en .webm)


#############################
## v1.03

- Nombre: `canción - artista`
- Skip si ya existe








############################
## v1.05




**Auditoría: todo quedó correctamente implementado y funcional** (deps, UI, parseo, nombres, WebM/MP3, skip, letra, carátula iTunes, embebido MP3).

---

### Texto para tu README (cópialo tú)

```markdown
# Descargar YouTube Music

App de escritorio en Python (Tkinter) para descargar audio desde YouTube / YouTube Music en lote.

## Cómo abrirla
- Doble clic en `iniciar.bat`, o:
- `python descargar_musica.py`
- Dependencias: `yt-dlp`, `mutagen` (y `ffmpeg` para convertir a MP3)

## Qué hace

### Descarga
- Caja de texto para pegar varios enlaces (uno por línea)
- Botón **Descargar**: procesa en orden, uno a uno
- Botón **Detener**
- Registro de progreso en vivo
- Carpeta de salida configurable (por defecto `Downloads/YouTubeMusic`)

### Formatos
- **MP3 (compatible):** mejor audio + conversión a MP3 calidad máxima (`--audio-quality 0`)
- **WebM (tal cual YouTube):** descarga el mejor stream original (casi siempre Opus) sin reconvertir — máxima fidelidad posible desde YouTube Music

### Nombres de archivo
- Guarda como: `canción - artista.ext` (nombre limpio, sin caracteres inválidos)

### Evitar duplicados
- Antes de descargar, busca si ya existe ese tema en la carpeta
- Si existe, no vuelve a bajarlo
- En el resumen final indica cuántas **ya estaban descargadas**

### Portapapeles
- Opción para pegar automáticamente enlaces de YouTube/YouTube Music cuando los copias
- Ignora duplicados y URLs que no sean de YouTube
- No pega al abrir la app ni mientras descarga

### Letra y carátula (solo en descargas nuevas)
- Tras cada descarga exitosa:
  - Busca **letra** (LRCLIB) → guarda `.lrc` o `.txt`
  - Busca **carátula de álbum** (iTunes) → si no hay, usa la **miniatura de YouTube Music**
  - Guarda `canción - artista.jpg` (o `.png`)
  - En **MP3** también embebe carátula y letra dentro del archivo
- Si no encuentra letra/carátula, avisa en el log y **no cancela** la descarga del audio

### Resumen final
- Descargadas / Ya estaban / Fallidos

## Notas
- WebM = máxima fidelidad posible desde YouTube Music (stream Opus original). El techo lo pone YouTube, no la app.
- La calidad de reproducción en la app de YouTube Music no cambia lo que descarga esta herramienta.
- Para YouTube Music en el navegador, a veces hace falta un addon que reactive el clic derecho (p. ej. Allow Right Click) si la web lo bloquea.
```






#################
## v1.06




```markdown
# Descargar YouTube Music

App de escritorio en Python (Tkinter) para descargar audio desde YouTube / YouTube Music en lote.

## Cómo abrirla
- Doble clic en `iniciar.bat`, o:
- `python descargar_musica.py`
- Dependencias: `yt-dlp`, `mutagen` y `ffmpeg` (necesario para MP3 y Opus)

## Qué hace

### Descarga
- Caja de texto para varios enlaces (uno por línea)
- Botón **Descargar**: procesa en orden, uno a uno
- Botón **Detener**
- Registro de progreso en vivo
- Carpeta de salida configurable (por defecto `Downloads/YouTubeMusic`)

### Formatos
- **MP3 (compatible):** conversión a MP3 en máxima calidad (`--audio-quality 0`)
- **Opus (tal cual YouTube):** el mejor audio de YouTube Music (Opus) sin reconvertir; solo se cambia el contenedor de `.webm` a `.opus` para poder embeber carátula/letra. Misma fidelidad de audio.

### Nombre de archivo
- `canción - artista.ext` (limpio, sin caracteres inválidos)

### Un solo archivo por canción
- Queda **solo** el `.mp3` o `.opus`
- **Carátula** embebida dentro del archivo (iTunes → si no, miniatura de YouTube Music)
- **Letra** embebida dentro del archivo (LRCLIB → si no, lyrics.ovh)
- No genera `.jpg`, `.lrc` ni `.txt` al lado
- Pensado para que AIMP muestre portada y letra desde el propio archivo

### Evitar duplicados
- Si la canción ya existe en la carpeta, no la vuelve a descargar
- En el resumen: Descargadas / Ya estaban / Fallidos

### Portapapeles
- Opción para añadir automáticamente enlaces de YouTube / YouTube Music al copiarlos
- Ignora duplicados y URLs que no sean de YouTube
- No pega al abrir la app ni mientras descarga

## Notas
- Opus = máxima fidelidad posible desde YouTube Music (stream original). El techo lo pone YouTube, no la app.
- MP3 es más compatible; Opus es mejor calidad de origen.
- Si no hay letra en las fuentes usadas, el audio y la carátula igual se guardan; solo falta la letra (aviso en el log).
- En YouTube Music del navegador, a veces hace falta un addon que reactive el clic derecho (p. ej. Allow Right Click) si la web lo bloquea.
```




####################
## v1.07


# Descargar YouTube Music
Apps de escritorio en Python (Tkinter) para uso personal: descargar audio desde **YouTube / YouTube Music** a una carpeta local, con nombre limpio, sin duplicados, y **un solo archivo por canción** (audio + carátula + letra embebidas cuando las fuentes las provean).
Pensadas para reproducir en **AIMP** (u otros players). No son cliente oficial de YouTube Music ni sustituto de Premium.
Hay **dos apps**:
| App | Entrada | Lanzador |
|---|---|---|
| Canciones sueltas | Enlaces de pistas | `iniciar.bat` |
| Playlists / álbumes | Enlaces de playlist o álbum | `iniciar_playlist.bat` |
---
## Requisitos
- Windows + Python 3
- Dependencias Python: `yt-dlp`, `mutagen` (ver `requirements.txt`)
- **ffmpeg** (obligatorio para MP3 y para Opus: remux + metadatos)
- Conexión a internet
---
## Cómo abrirlas
### Canciones
- Doble clic en `iniciar.bat`, o:
- `python descargar_musica.py`
### Playlists / álbumes
- Doble clic en `iniciar_playlist.bat`, o:
- `python descargar_playlist.py`
Los `.bat` instalan las dependencias de `requirements.txt` y lanzan la app.
Carpeta de salida por defecto: `Downloads/YouTubeMusic` (configurable en la UI).
---
## App 1 — Canciones (`descargar_musica.py`)
Para pegar varios enlaces de **pistas** y descargarlos uno a uno.
### Entrada
- Caja multilínea: un enlace por línea (YouTube / YouTube Music / youtu.be)
- Opción **Pegar automáticamente del portapapeles**:
  - Solo pega cambios nuevos (no el contenido ya presente al abrir)
  - No pega duplicados ni mientras hay una descarga en curso
  - Se puede desactivar con el checkbox
- Usa `--no-playlist`: si el link trae `list=`, **no** descarga la lista entera (solo la pista)
### Controles
- **Descargar**: procesa en orden, uno a uno
- **Detener**: corta el flujo
- Log de progreso en vivo
- Carpeta de salida elegible
### Formatos
| Modo | Qué hace | Calidad |
|---|---|---|
| **MP3 (compatible)** | `bestaudio` → extracción a MP3 calidad `0` | Máxima compatibilidad; implica reconversión |
| **Opus (tal cual YouTube)** | `bestaudio` (casi siempre Opus en WebM) → remux a `.opus` con `ffmpeg -c copy` | Máxima fidelidad disponible **sin Premium** (mismo bitstream Opus; solo cambia el contenedor) |
> **Nota:** remux ≠ recomprimir. En modo Opus no se re-encodea el audio.
### Nombre de archivo
- Patrón: `canción - artista.mp3` o `canción - artista.opus`
- Sanitizado para Windows (sin caracteres inválidos)
### Duplicados
- Si ya existe un audio con el mismo stem (`canción - artista`) en la carpeta → **no vuelve a descargar**
- Cuenta como “Ya estaban” en el resumen
### Un solo archivo (carátula + letra)
Tras cada descarga **nueva** (no en skips):
1. Si el contenedor es WebM/MKA → remux a `.opus` sin re-encode
2. Carátula: **iTunes** → si falla → **miniatura de YouTube**
3. Letra: **LRCLIB** (prioriza LRC con timestamps) → si falla → **lyrics.ovh**
4. Embebe todo en el archivo (MP3 o Opus) con mutagen
5. Verifica tags embebidos
6. Elimina sidecars (`.jpg` / `.png` / `.lrc` / `.txt`)
Resultado: **exactamente un archivo** de audio por canción en la entrega normal.
Si falla letra o carátula, el audio se conserva y se registra un aviso en el log.
### Resumen final
Informa: **Descargadas / Ya estaban / Fallidos**.
---
## App 2 — Playlists (`descargar_playlist.py`)
Para pegar enlaces de **playlist o álbum** y bajar todas las pistas organizadas por carpeta.
Es una app **separada** (clon incremental). No modifica ni sustituye la app de canciones.
### Flujo
1. Lista las pistas de la playlist (`yt-dlp -J --flat-playlist`)
2. Crea la carpeta del álbum bajo la carpeta base
3. Descarga **pista a pista** con el mismo pipeline de calidad, nombre, skip y extras que la app de canciones
### Estructura de carpetas
```text
CarpetaBase / Artista - Álbum (YYYY) / canción - artista.opus|.mp3
Ejemplo:

Downloads/YouTubeMusic/Pink Floyd - The Dark Side of the Moon (1973)/Time - Pink Floyd.opus
Si no hay año fiable:

CarpetaBase / Artista - Álbum / canción - artista.opus|.mp3
Año del álbum (cascada)
Metadatos de release de YouTube (release_year / release_date) — no usa upload_date como año de álbum
iTunes Search
MusicBrainz (con reintentos ante rate limit)
Es “best effort”: si las fuentes no aciertan, la carpeta puede salir sin (YYYY).

Lo que comparte con la app de canciones
Selector MP3 / Opus
Nombre canción - artista
Skip de duplicados (dentro de la carpeta del álbum)
Un solo archivo embebido (sin sidecars)
Portapapeles automático opcional
Resumen Descargadas / Ya estaban / Fallidos
Calidad real (qué esperar)
Sin Premium / sin cookies: techo típico ≈ formato 251 Opus ~128–160 kbps vía bestaudio
Con Premium a veces existen formatos ~256 kbps; no forman parte del alcance actual
YouTube no ofrece FLAC / lossless por este canal
El techo lo pone YouTube, no la app
Archivos del proyecto
Archivo	Rol
descargar_musica.py
UI + descarga de pistas sueltas
descargar_playlist.py
UI + descarga de playlists / álbumes
metadata_extras.py
Carátula, letra, remux Opus, embeber, verificación
iniciar.bat
Arranque Windows (canciones)
iniciar_playlist.bat
Arranque Windows (playlists)
requirements.txt
Dependencias Python
architecture_law.md
Contrato de arquitectura v1 (canciones)
architecture_law_v2.md
Contrato de arquitectura v2 (proyecto completo)
El README documenta el uso. El contrato de arquitectura fija reglas de diseño (incl. inmutabilidad). Ante duda de producto, manda el contrato.

Notas y límites
Uso personal. Respeta los términos de YouTube y los derechos de autor del material que descargues.
ffmpeg debe estar instalado (PATH o rutas WinGet típicas; la app intenta detectarlo).
Playlists grandes = muchas llamadas de red (año, carátula, letra); el log avisa si algo falla.
No hay login Premium ni cookies dentro de la app.
Dos apps a propósito: pistas sueltas vs álbumes. No están fusionadas en una sola UI.
Checklist rápida
iniciar.bat / iniciar_playlist.bat abren la UI
MP3 y Opus descargan; Opus no re-encodea el audio
Nombre canción - artista
Skip de duplicados + resumen
Un solo archivo por pista (tags embebidos, sin sidecars)
Playlist crea Artista - Álbum (año) y guarda las pistas ahí
Fallo de letra/carátula/año no borra el audio
Estado actual (changelog corto)
Entrega final Opus (no WebM) para poder embeber carátula y letra de forma fiable
Un archivo por canción; sidecars fuera del diseño
App de playlists con carpetas Artista - Álbum (año) y cascada de año YouTube → iTunes → MusicBrainz
Núcleo compartido de extras en metadata_extras.py





############################
## v1.08



Mejoras implementadas




########################
## v1.09


## prompt ##

También quiero que audites, analices, mires la mejor opción añadas esta funcionalidad como mejora incremental, no quiero que toques nada relacionado a la calidad del audio ni formato ni nada ,los cambios serán exclusivamente relacionados a la velocidad de descarga de ambas app

4. Que al descargar, el orden quede así: / Artista / NombreAlbum / Canción - Artista
que revise cuando vaya a crear todo si existe y guardar dentro, y si no crear uno nuevo

conserva todo muy bien, que funcione super bien, mejora mínima incremental, solamente implementando lo que se te pide, al final revisa que todo quedó bien implementado y funciona como se espera, has comprobaciones y validaciones de que todo funciona correctamente



## resp cursor ##


Hecho
Estructura en ambas apps:

CarpetaBase / Artista / NombreAlbum / Canción - Artista.ext
Si Artista o NombreAlbum ya existen → guarda dentro (exist_ok=True)
Si no → los crea
Sin álbum en metadatos → carpeta Sin álbum
Skip de duplicados mira esa carpeta del álbum
Extra (velocidad playlists)
Ya no consulta año (iTunes/MusicBrainz) para nombrar carpetas; la ruta es solo Artista / NombreAlbum.

Sin tocar
Calidad, bestaudio, -N, paralelo ×2, formatos.

Validado
Crear/reutilizar carpetas, naming, sin año en ruta, calidad intacta, UI OK.






####################
## v1.10



Mejoras para las apps




#################################


