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

Construir y arrancar el contenedor:

```bash
docker compose up --build
```

El directo quedara disponible en:

```text
http://localhost:5058
```

En esa pagina pega la URL del directo y pulsa `Conectar`. La playlist para reproductores queda en:

```text
http://localhost:5058/live.m3u8
```

Opcionalmente puedes arrancar el contenedor con una URL inicial:

```bash
YOUTUBE_URL="https://www.youtube.com/live/5P4kxc5bnO8?si=QkuzMRXdGD_tnRQ0" docker compose up --build
```

Tambien puedes usar `docker run`:

```bash
docker build -t youtube-hls .
docker run --rm -p 5058:5000 \
  youtube-hls
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
