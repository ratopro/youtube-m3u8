# YouTube Live HLS Proxy

Aplicacion Python para tomar un directo de YouTube, obtener su manifest HLS con `yt-dlp` y reemitirlo localmente como una playlist `.m3u8` servida por Flask.

## Uso

```bash
./venv/bin/python app.py --host 0.0.0.0 --port 5000
```

Despues abre:

```text
http://127.0.0.1:5000
```

Playlist HLS local:

```text
http://127.0.0.1:5000/live.m3u8
```

La pagina web pedira la URL del directo. Pegala, pulsa `Conectar` y el servidor mantendra esa conexion hasta que se reinicie o hasta que el stream falle.

## Instalar dependencias

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Uso con Docker

### Opcion A: imagen publicada en Docker Hub (recomendado)

```bash
mkdir -p youtube-m3u8/data
cd youtube-m3u8
curl -fsSL https://raw.githubusercontent.com/ratopro/youtube-m3u8/main/deploy/config.example.json -o data/config.json
docker run -d --name youtube-hls --restart unless-stopped \
  -p 5058:5000 \
  -e TZ=Europe/Madrid \
  -e APP_TZ=Europe/Madrid \
  -v "$(pwd)/data:/app/data" \
  ratopro/youtube-m3u8:latest
```

Abre `http://localhost:5058` y, si quieres, edita `data/config.json` con tus credenciales Xtream (el contenedor se reinicia automaticamente al recargar `data/state.json` solo si reinicias el contenedor).

Para parar y eliminar el contenedor:

```bash
docker stop youtube-hls && docker rm youtube-hls
```

Para actualizar a la ultima imagen:

```bash
docker pull ratopro/youtube-m3u8:latest
docker stop youtube-hls && docker rm youtube-hls
docker run -d --name youtube-hls --restart unless-stopped \
  -p 5058:5000 \
  -e TZ=Europe/Madrid \
  -e APP_TZ=Europe/Madrid \
  -v "$(pwd)/data:/app/data" \
  ratopro/youtube-m3u8:latest
```

### Opcion B: construir localmente

```bash
git clone https://github.com/ratopro/youtube-m3u8.git
cd youtube-m3u8
cp deploy/config.example.json data/config.json
docker compose up -d --build
```

El directo quedara disponible en:

```text
http://localhost:5058
```

## Emby

Primero abre la web y conecta el directo:

```text
http://IP_DEL_HOST_DOCKER:5058
```

Despues, en Emby anade una fuente `M3U Tuner` con esta URL:

```text
http://IP_DEL_HOST_DOCKER:5058/channels.m3u
```

Para forzar la maxima calidad disponible, usa esta lista en lugar de la anterior:

```text
http://IP_DEL_HOST_DOCKER:5058/channels-max.m3u
```

Ejemplo:

```text
http://192.168.1.50:5058/channels.m3u
```

La guia XMLTV minima esta disponible en:

```text
http://IP_DEL_HOST_DOCKER:5058/guide.xml
```

El canal de la lista M3U apunta internamente a:

```text
http://IP_DEL_HOST_DOCKER:5058/live.m3u8
```

El canal de maxima calidad apunta a:

```text
http://IP_DEL_HOST_DOCKER:5058/live-max.m3u8
```

`/live-max.m3u8` selecciona la variante HLS con mayor resolucion y bitrate cuando YouTube entrega un manifest adaptativo. Si YouTube solo entrega un MP4 directo, se usa ese stream directo.

No uses `localhost` en Emby si Emby esta en otro equipo o en otro contenedor.

## Archivos de video locales (MP4, MKV, AVI)

Ademas de canales de YouTube, HLS e IPTV, puedes subir archivos de video propios y usarlos como una fuente mas de la parrilla.

En la pagina principal, abre el panel **Archivos de video**, selecciona un archivo `.mp4`, `.mkv` o `.avi`, opcionalmente ponle un nombre y pulsa **Subir y anadir**.

- El archivo se guarda en el servidor dentro de `MEDIA_DIR` (por defecto `data/media`, o `/app/data/media` en Docker) con un nombre aleatorio; el nombre original queda como metadato.
- Al subirlo se leen duracion y codecs con `ffprobe`. Si el archivo no contiene video o esta corrupto, se rechaza.
- La fuente aparece en **Fuentes** con el tipo `archivo` y se puede programar en la parrilla como cualquier otra.
- La reproduccion se hace en bucle continuo. Los codecs compatibles con MPEG-TS se copian y el resto se transcodifican.
- Al eliminar la fuente se borra tambien el archivo, siempre que este dentro de `MEDIA_DIR`.

Variables de entorno relacionadas:

```yaml
environment:
  MEDIA_DIR: "/app/data/media"
  MAX_UPLOAD_MB: "8192"
```

- `MEDIA_DIR`: carpeta donde se almacenan los archivos subidos. Debe estar dentro del volumen persistente (`/app/data`).
- `MAX_UPLOAD_MB`: tamano maximo permitido por archivo, en megabytes (por defecto `8192`, es decir 8 GB).

> El limite real tambien depende del proxy inverso que pongas delante (por ejemplo nginx `client_max_body_size`).

## Cache de segmentos

El proxy guarda segmentos de video localmente para reducir cortes cuando el reproductor repite peticiones o Emby reintenta fragmentos.

Variables disponibles en `docker-compose.yml`:

```yaml
environment:
  CACHE_TTL_SECONDS: "1800"
  CACHE_MAX_MB: "512"
  CACHE_MAX_OBJECT_MB: "32"
  LIVE_WINDOW_SEGMENTS: "30"
  PRESENTATION_LOOP_COUNT: "1000"
```

- `CACHE_TTL_SECONDS`: tiempo maximo de vida de cada fragmento en cache.
- `CACHE_MAX_MB`: tamano total maximo de la cache.
- `CACHE_MAX_OBJECT_MB`: tamano maximo de un segmento individual cacheable.
- `LIVE_WINDOW_SEGMENTS`: numero de segmentos recientes que se entregan en playlists de medios; evita que Emby cargue listas DVR enormes.
- `PRESENTATION_LOOP_COUNT`: repeticiones del video `sofa.mp4` en la playlist de presentacion para mantener emision continua.

La cache no evita cortes si YouTube deja de entregar el directo, pero ayuda con microcortes, reintentos y peticiones repetidas del mismo segmento.

## Notas

- Las URLs firmadas de YouTube expiran.
- El servidor local actua como proxy HLS y reescribe manifests y segmentos para que se consuman desde `localhost`.
- Usa el stream solo cuando tengas permiso para reproducir o reemitir el contenido.
