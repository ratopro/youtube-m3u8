#!/bin/sh
set -eu

IMAGE="ratopro/youtube-m3u8:latest"
CONTAINER_NAME="youtube-hls"
HOST_PORT="5058"
CONTAINER_PORT="5000"

# Cache/buffer tuning
CACHE_TTL_SECONDS="3600"
CACHE_MAX_MB="2048"
CACHE_MAX_OBJECT_MB="64"
LIVE_WINDOW_SEGMENTS="30"
PRESENTATION_LOOP_COUNT="5000"
APP_VERSION="2026.05.25-embyfix7"
AUTO_PRESENTATION_ON_END="1"

echo "[1/4] Descargando imagen mas reciente: ${IMAGE}"
docker pull "${IMAGE}"

echo "[2/4] Parando contenedor anterior (si existe)"
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

echo "[3/4] Levantando contenedor nuevo"
docker run -d \
  --name "${CONTAINER_NAME}" \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  -e CACHE_TTL_SECONDS="${CACHE_TTL_SECONDS}" \
  -e CACHE_MAX_MB="${CACHE_MAX_MB}" \
  -e CACHE_MAX_OBJECT_MB="${CACHE_MAX_OBJECT_MB}" \
  -e LIVE_WINDOW_SEGMENTS="${LIVE_WINDOW_SEGMENTS}" \
  -e PRESENTATION_LOOP_COUNT="${PRESENTATION_LOOP_COUNT}" \
  -e APP_VERSION="${APP_VERSION}" \
  -e AUTO_PRESENTATION_ON_END="${AUTO_PRESENTATION_ON_END}" \
  --restart unless-stopped \
  "${IMAGE}"

echo "[4/4] Estado del contenedor"
docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo
echo "Listo. Abre en navegador:"
echo "  http://TU_IP_ALPINE:${HOST_PORT}"
echo
echo "Para Emby (M3U Tuner):"
echo "  http://TU_IP_ALPINE:${HOST_PORT}/channels.m3u"
echo "o maxima calidad:"
echo "  http://TU_IP_ALPINE:${HOST_PORT}/channels-max.m3u"
echo "compatibilidad alta (direct):"
echo "  http://TU_IP_ALPINE:${HOST_PORT}/channels-emby-direct.m3u"
echo "video presentacion en bucle (si lo activas desde la web):"
echo "  http://TU_IP_ALPINE:${HOST_PORT}/presentation.m3u8"
