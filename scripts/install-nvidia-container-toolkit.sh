#!/bin/bash
# Instala NVIDIA Container Toolkit para soporte GPU en Docker
# Requiere: NVIDIA driver instalado en el host + Docker

set -e

DISTRO=$(lsb_release -is 2>/dev/null || echo "unknown")
NVIDIA_SMI=$(which nvidia-smi 2>/dev/null || echo "")
DOCKER_VERSION=$(docker --version 2>/dev/null || echo "")

echo "=== NVIDIA Container Toolkit Installer ==="
echo ""

if [ -z "$NVIDIA_SMI" ]; then
    echo "[ERROR] nvidia-smi no encontrado. Instala el driver NVIDIA primero."
    exit 1
fi

echo "[OK] Driver NVIDIA detectado:"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null || echo "  (no se pudo obtener detalle)"

if ! command -v docker &> /dev/null; then
    echo "[ERROR] Docker no encontrado."
    exit 1
fi

echo "[OK] Docker detectado: $DOCKER_VERSION"

DISTRO=$(. /etc/os-release 2>/dev/null && echo "$ID" || echo "unknown")
echo "[INFO] Distribucion: $DISTRO"

if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
    echo "[INFO] Instalando para Debian/Ubuntu..."

    DISTRO_VERSION=$(. /etc/os-release 2>/dev/null && echo "$VERSION_ID" || echo "")

    # Anadir repository NVIDIA
    curl -fsSL https://nvidia.github.io/nvidia-container-runtime/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null || true

    ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")
    REPO_URL="https://nvidia.github.io/nvidia-container-runtime/ubuntu/${DISTRO}/"
    echo "deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] ${REPO_URL} /" \
        | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null

    apt-get update -qq
    apt-get install -y -qq nvidia-container-toolkit

    # Configurar Docker runtime
    mkdir -p /etc/docker
    if [ -f /etc/docker/daemon.json ]; then
        echo "[WARN] /etc/docker/daemon.json ya existe. Revisa manualmente que incluya nvidia runtime."
    else
        cat > /etc/docker/daemon.json << 'EOF'
{
    "runtimes": {
        "nvidia": {
            "args": [],
            "path": "nvidia-container-runtime"
        }
    }
}
EOF
        echo "[OK] /etc/docker/daemon.json creado con runtime nvidia."
    fi

    systemctl restart docker
    echo "[OK] Docker reiniciado."

elif [ "$DISTRO" = "fedora" ] || [ "$DISTRO" = "rhel" ] || [ "$DISTRO" = "centos" ]; then
    echo "[INFO] Instalando para Fedora/RHEL/CentOS..."
    dnf install -y nvidia-container-toolkit
    mkdir -p /etc/docker
    if [ ! -f /etc/docker/daemon.json ]; then
        cat > /etc/docker/daemon.json << 'EOF'
{
    "runtimes": {
        "nvidia": {
            "args": [],
            "path": "nvidia-container-runtime"
        }
    }
}
EOF
    fi
    systemctl restart docker
    echo "[OK] Docker reiniciado."
else
    echo "[WARN] Distribucion no reconocida automaticamente. Instala manualmente:"
    echo "       https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
fi

# Verificar test
echo ""
echo "=== Verificacion ==="
if docker run --rm --gpus all ubuntu:nvidia nvidia-smi 2>/dev/null; then
    echo "[OK] GPU accesible desde Docker."
else
    echo "[WARN] La GPU no es accesible desde Docker. Prueba:"
    echo "       1. Reiniciar el servicio Docker: systemctl restart docker"
    echo "       2. Verificar que el modulo nvidia-uvm esta cargado: ls /dev/nvidia*"
    echo "       3. Revisar /etc/docker/daemon.json"
fi

echo ""
echo "=== Instalacion completada ==="
echo "Ejecuta 'scripts/up.sh' para arrancar con/sin GPU automaticamente."