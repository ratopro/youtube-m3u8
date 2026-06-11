#!/bin/sh
set -e

# Si /app/data es un bind-mount, el directorio raíz es propiedad de root.
# Forzamos que el contenido sea escribible por appuser para que SQLite
# pueda crear app.db, state.json y el cache de EPG.
if [ -d /app/data ]; then
    chown -R appuser:appuser /app/data 2>/dev/null || true
    chmod -R u+rwX,go-rwx /app/data 2>/dev/null || true
fi

# Create the HLS temp directories and hand them to appuser.  ffmpeg
# launched by the application writes the live segments into these
# directories, so they must be owned by the user we drop to.
mkdir -p /tmp/presentation-hls /tmp/preview-hls /tmp/processed-hls /tmp/youtube-hls-cache
chown -R appuser:appuser /tmp/presentation-hls /tmp/preview-hls /tmp/processed-hls /tmp/youtube-hls-cache
chmod 755 /tmp/presentation-hls /tmp/preview-hls /tmp/processed-hls /tmp/youtube-hls-cache

# Drop privileges to appuser for the actual application process.
exec gosu appuser "$@"
