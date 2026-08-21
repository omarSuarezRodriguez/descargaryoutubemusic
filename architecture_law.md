# architecture_law.md — Contrato de Arquitectura

**Proyecto:** `descargaryoutubemusic`  
**Tipo:** Contrato de arquitectura **VIGENTE E INTOCABLE**  
**Idioma del producto:** Español (UI y documentación de usuario)  
**Fecha de fijación:** 2026-08-21  

---

## 0. Cláusula de inmutabilidad (SUPREMA)

1. Este documento es la **ley de arquitectura** del proyecto.
2. Es **intocable** por defecto: ningún agente, asistente, colaborador ni refactor “por mejora” puede alterarlo, reinterpretarlo en silencio ni contradecirlo en código.
3. Solo puede modificarse por **orden única y expresa del propietario del proyecto** (el usuario humano dueño del repo).
4. Esa orden debe estar **confirmada dos (2) veces** de forma explícita en el mismo hilo/conversación o instrucción escrita, por ejemplo:
   - Confirmación 1: «modifica architecture_law.md …»
   - Confirmación 2: «confirmo por segunda vez: modifica architecture_law.md …»
5. Sin esas **dos confirmaciones explícitas**, cualquier cambio a este archivo (o a la arquitectura aquí fijada) está **prohibido**.
6. Si una petición de feature contradice este contrato, **gana este contrato**, salvo que el propietario active el procedimiento de las dos confirmaciones.

---

## 1. Propósito y alcance

### 1.1 Qué es
Aplicación de escritorio personal en **Python + Tkinter** para descargar audio desde **YouTube / YouTube Music** en lote, con nombre limpio, sin duplicados, y **un solo archivo** por canción que incluye audio + carátula + letra embebidas (cuando las fuentes las provean).

### 1.2 Qué NO es (fuera de alcance)
- Producto comercial / tienda de apps / SaaS.
- Cliente oficial de YouTube Music ni sustituto de Premium.
- Descarga de vídeo.
- Extensión de Chrome obligatoria (el portapapeles y el pegado manual cubren el flujo).
- Login/cookies Premium embebidos (el techo sin Premium es el diseño actual).
- FLAC / lossless (YouTube no lo ofrece por este canal).

### 1.3 Usuario objetivo
Uso personal: biblioteca local, reproducción en **AIMP** (u otros players), estudio de inglés con letra, máxima calidad práctica desde YouTube Music.

---

## 2. Principios de diseño (leyes)

1. **Necesidad primero:** la app existe para un flujo real (pegar enlaces → descargar → escuchar).
2. **Mejora incremental:** se puede ampliar, pero sin romper lo ya estable.
3. **Integridad del audio Opus:** el modo “tal cual YouTube” no debe reconvertir el stream de audio; solo remux de contenedor si hace falta.
4. **Un archivo por canción:** salida final = un `.mp3` o un `.opus`. Prohibido dejar sidecars `.jpg` / `.png` / `.lrc` / `.txt` como entrega normal.
5. **Extras no tumban la descarga:** si falla letra o carátula, el audio se conserva; se registra aviso.
6. **Verificar antes de creer:** cambios relevantes se auditan (descarga real, tags embebidos, un solo archivo).
7. **Simpleza de UI:** caja de URLs, formato, carpeta, descargar/detener, log.

---

## 3. Stack tecnológico (fijado)

| Capa | Tecnología | Rol |
|---|---|---|
| Lenguaje | Python 3 | Runtime |
| UI | Tkinter / ttk | Interfaz gráfica |
| Descarga | yt-dlp (`python -m yt_dlp`) | Extracción de audio |
| Remux / MP3 | ffmpeg | Remux a `.opus` (`-c copy`); conversión a MP3 |
| Metadatos | mutagen | Embeber carátula/letra/tags |
| Letras | LRCLIB → fallback lyrics.ovh | Búsqueda de letra |
| Carátula | iTunes Search → fallback miniatura YouTube | Imagen de portada |
| Arranque Windows | `iniciar.bat` | Instala deps y lanza la app |
| Deps declaradas | `requirements.txt` | `yt-dlp`, `mutagen` |

### 3.1 Archivos núcleo
- `descargar_musica.py` — UI + orquestación de descarga
- `metadata_extras.py` — carátula, letra, remux Opus, embeber, verificación
- `iniciar.bat` — entrada en Windows
- `requirements.txt` — dependencias
- `readme.md` — documentación de usuario (no sustituye este contrato)
- `architecture_law.md` — **este contrato**

---

## 4. Comportamiento funcional (contrato de producto)

### 4.1 Entrada de enlaces
- Caja de texto multilínea: un enlace por línea (o pegado masivo parseado).
- Solo se aceptan URLs de YouTube / YouTube Music / youtu.be.
- Opción **pegar automáticamente del portapapeles** (vigilancia periódica):
  - No pega el contenido ya presente al abrir la app (solo cambios nuevos).
  - No pega duplicados ni mientras descarga.
  - Se puede desactivar con checkbox.

### 4.2 Descarga
- Botón **Descargar**: procesa **en orden, uno a uno**.
- Botón **Detener**: corta el flujo (termina/aborta el ítem en curso según implementación).
- `--no-playlist`: no descarga listas enteras por el parámetro `list=`.
- Carpeta de salida configurable (default: `Downloads/YouTubeMusic`).
- Log en vivo del progreso.

### 4.3 Formatos (selector)
| Modo UI | Comportamiento | Calidad de audio |
|---|---|---|
| **MP3 (compatible)** | `bestaudio` + extracción a MP3 quality `0` | Máxima calidad MP3 de la cadena actual; implica reconversión |
| **Opus (tal cual YouTube)** | `bestaudio` (casi siempre Opus en WebM) → remux a `.opus` con `ffmpeg -c:a copy` | **Máxima fidelidad disponible sin Premium** (mismo bitstream Opus); contenedor cambiado solo para metadatos |

**Ley de calidad Opus:** no se re-encodea el audio en el modo Opus. Remux ≠ recomprimir.

**Techo de YouTube (realidad del dominio):**
- Sin Premium / sin cookies: típicamente formato `251` Opus ~128–160 kbps vía `bestaudio`.
- Con Premium pueden existir formatos más altos (~256 kbps, p. ej. `774`/`141`); **no forman parte del alcance actual** salvo que el propietario active una mejora vía el procedimiento de las 2 confirmaciones sobre este contrato.

### 4.4 Nombre de archivo
- Patrón obligatorio: **`canción - artista`**
- Sanitizado para Windows (sin caracteres inválidos).
- Extensión según formato final: `.mp3` o `.opus`.

### 4.5 Duplicados
- Antes de descargar, buscar en la carpeta un audio con el mismo stem (`canción - artista`).
- Si existe → **no descargar** → contar como “Ya estaban descargadas” en el resumen.

### 4.6 Un solo archivo (carátula + letra)
Tras cada descarga **nueva** (no en skips):

1. Si el contenedor es WebM/MKA → remux a `.opus` sin re-encode.
2. Buscar carátula: **iTunes** → si falla → **miniatura YouTube Music**.
3. Buscar letra: **LRCLIB** (priorizar LRC con timestamps) → si falla → **lyrics.ovh**.
4. Embeber en el archivo:
   - MP3: APIC (carátula) + USLT (letra; preferible con timestamps LRC para AIMP).
   - Opus: `metadata_block_picture` + tags de lyrics.
5. **Verificar** tags embebidos.
6. **Eliminar** cualquier sidecar `.jpg/.png/.lrc/.txt` (incluidos restos de versiones viejas).
7. Resultado: **exactamente un archivo** de audio por canción en la entrega normal.

Si no hay letra o carátula en fuentes externas: aviso en log; el audio permanece.

### 4.7 Resumen final
Informar: Descargadas / Ya estaban / Fallidos.

---

## 5. Arquitectura lógica

```
[UI Tkinter]
    ├─ parse URLs / clipboard
    ├─ selector MP3 | Opus
    └─ worker thread
          ├─ yt-dlp (info + download bestaudio)
          ├─ skip si existe
          ├─ ffmpeg (MP3 extract | Opus remux -c copy)
          └─ metadata_extras
                ├─ cover (iTunes → YT thumb)
                ├─ lyrics (LRCLIB → lyrics.ovh)
                ├─ embed (mutagen)
                └─ verify + purge sidecars
```

- UI no debe bloquearse: descargas en hilo en segundo plano.
- Errores de extras no abortan el éxito de descarga de audio.

---

## 6. Decisiones históricas fijadas (no reabrir sin 2 confirmaciones)

1. WebM puro **no** es el formato de entrega para carátula embebida (WebM no soporta attachments de portada de forma fiable para AIMP).
2. Entrega “tal cual YouTube” = **`.opus`** (mismo audio, mejor metadatos).
3. Siempre **1 archivo**; sidecars fuera del diseño.
4. Portapapeles automático = opcional, no obligatorio.
5. Calidad Opus actual = mejor disponible **sin** Premium cookies.
6. La app es herramienta personal, no producto de tienda.

---

## 7. Reglas para agentes / colaboradores

1. Leer este archivo **antes** de cambiar arquitectura, formatos, flujo de metadatos o dependencias núcleo.
2. Prohibido “simplificar” quitando Opus, reintroduciendo sidecars, o reconvirtiendo Opus “por comodidad”.
3. Prohibido añadir Premium/cookies, extensiones Chrome obligatorias, o reescritura total, sin orden del propietario con **2 confirmaciones** que autoricen cambiar este contrato.
4. Mejoras incrementales **dentro** del alcance están permitidas si no violan este documento.
5. Ante duda: **no modificar** `architecture_law.md` y preguntar al propietario.

---

## 8. Criterio de éxito (definición de “correcto”)

Una release se considera alineada con este contrato si:

- [ ] Descarga en orden múltiples URLs de YouTube Music.
- [ ] Selector MP3 / Opus funciona.
- [ ] Opus no re-encodea el audio (`-c copy` en remux).
- [ ] Nombre `canción - artista`.
- [ ] Skip de duplicados + resumen.
- [ ] Un solo archivo final por canción nueva.
- [ ] Carátula embebida verificable en MP3 y Opus (cuando hubo imagen).
- [ ] Letra embebida verificable cuando hubo fuente.
- [ ] Sin sidecars en la entrega normal.
- [ ] Fallo de letra/carátula no borra el audio.

---

## 9. Procedimiento formal de enmienda

Para cambiar este contrato:

1. El propietario declara de forma explícita qué cláusula quiere cambiar y por qué.
2. **Confirmación 1** explícita: orden de modificar `architecture_law.md`.
3. **Confirmación 2** explícita: “confirmo por segunda vez…” referida a la misma enmienda.
4. Solo entonces se edita este archivo y, si aplica, el código para alinearlo.
5. Toda enmienda debe dejar constancia de fecha y resumen del cambio al final (historial breve).

### Historial de enmiendas
| Fecha | Cambio | Autorización |
|---|---|---|
| 2026-08-21 | Creación inicial del contrato (estado vigente del producto) | Orden del propietario al crear el archivo |

---

## 10. Declaración final

Este archivo constituye el **contrato de arquitectura vinculante** de `descargaryoutubemusic`.  
Todo lo demás (código, readme, issues, sugerencias de IA) se interpreta **a la luz de este documento**.  
**Sin dos confirmaciones expresas del propietario, este contrato no se toca.**
