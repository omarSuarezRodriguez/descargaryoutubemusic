## v1.05

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