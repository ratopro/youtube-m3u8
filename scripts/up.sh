#!/bin/bash
# Arranca el contenedor seleccionando automaticamente GPU o CPU
# Uso: ./scripts/up.sh [--force-gpu] [--force-cpu]

FORCE_GPU=false
FORCE_CPU=false

for arg in "$@"; do
    case $arg in
        --force-gpu) FORCE_GPU=true ;;
        --force-cpu) FORCE_CPU=true ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

has_gpu() {
    if [ -d /dev/nvidia ] && command -v nvidia-smi &>/dev/null; then
        if nvidia-smi &>/dev/null; then
            return 0
        fi
    fi
    return 1
}

echo "=== youtube-m3u8 launcher ==="

if $FORCE_GPU; then
    echo "[FORCE-GPU] Forzando modo NVIDIA..."
    exec docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build

elif $FORCE_CPU; then
    echo "[FORCE-CPU] Forzando modo CPU..."
    exec docker compose up -d --build

elif has_gpu; then
    echo "[AUTO-GPU] GPU detectada. Usando docker-compose.nvidia.yml"
    exec docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build

else
    echo "[AUTO-CPU] Sin GPU. Usando CPU (libx264)"
    exec docker compose up -d --build
fi