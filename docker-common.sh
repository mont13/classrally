#!/usr/bin/env bash
# Shared functions for ClassRally Docker scripts
# Sourced by start.sh, stop.sh, rebuild.sh, logs.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

DOCKER_CMD="docker"
COMPOSE_CMD="docker compose"

# --- Docker installation ---
install_docker() {
    info "Docker neni nainstalovany. Instaluji..."

    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v apt-get &>/dev/null; then
            info "Detekovany Debian/Ubuntu - pouzivam apt"
            sudo apt-get update
            sudo apt-get install -y ca-certificates curl gnupg
            sudo install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            sudo chmod a+r /etc/apt/keyrings/docker.gpg
            . /etc/os-release
            REPO_URL="https://download.docker.com/linux/${ID}"
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${REPO_URL} ${VERSION_CODENAME} stable" | \
                sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            sudo apt-get update
            sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        elif command -v dnf &>/dev/null; then
            info "Detekovany Fedora/RHEL - pouzivam dnf"
            sudo dnf -y install dnf-plugins-core
            sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
            sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        elif command -v pacman &>/dev/null; then
            info "Detekovany Arch Linux - pouzivam pacman"
            sudo pacman -Sy --noconfirm docker docker-compose
        else
            error "Neznama distribuce. Nainstaluj Docker rucne: https://docs.docker.com/engine/install/"
        fi

        sudo systemctl start docker
        sudo systemctl enable docker

        if ! groups "$USER" | grep -q docker; then
            sudo usermod -aG docker "$USER"
            warn "Uzivatel '$USER' pridan do skupiny 'docker'."
            warn "Pro pouziti bez sudo se odhlaste a prihlaste znovu,"
            warn "nebo spustte: newgrp docker"
            DOCKER_CMD="sudo docker"
            COMPOSE_CMD="sudo docker compose"
            return
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        error "Na macOS nainstaluj Docker Desktop rucne: https://docs.docker.com/desktop/install/mac-install/"
    else
        error "Nepodporovany OS. Nainstaluj Docker rucne: https://docs.docker.com/engine/install/"
    fi
}

check_docker() {
    if ! command -v docker &>/dev/null; then
        install_docker
    fi

    if ! $DOCKER_CMD info &>/dev/null; then
        if sudo docker info &>/dev/null; then
            DOCKER_CMD="sudo docker"
            COMPOSE_CMD="sudo docker compose"
            warn "Docker vyzaduje sudo. Pridej se do skupiny 'docker': sudo usermod -aG docker $USER"
        else
            error "Docker daemon nebezi. Spust: sudo systemctl start docker"
        fi
    fi

    info "Docker OK: $($DOCKER_CMD --version)"
}

# --- Host IP detection ---
detect_host_ip() {
    if [[ -n "${QUIZ_EXTERNAL_IP:-}" ]]; then
        return
    fi

    local candidates=()

    local udp_ip
    udp_ip=$(python3 -c "
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('1.1.1.1', 80))
    print(s.getsockname()[0])
    s.close()
except: pass
" 2>/dev/null || true)
    if [[ -n "$udp_ip" && "$udp_ip" != "127."* ]]; then
        candidates+=("$udp_ip")
    fi

    local all_ips
    all_ips=$(hostname -I 2>/dev/null || true)
    for ip in $all_ips; do
        if [[ "$ip" =~ ^(10\.|192\.168\.) ]]; then
            local found=0
            for c in "${candidates[@]:-}"; do
                [[ "$c" == "$ip" ]] && found=1 && break
            done
            [[ $found -eq 0 ]] && candidates+=("$ip")
        fi
    done

    if [[ ${#candidates[@]} -eq 0 ]]; then
        warn "Nelze detekovat LAN IP adresu hosta."
        warn "Zaci se nebudou moci pripojit z mobilu."
        warn "Nastav rucne: QUIZ_EXTERNAL_IP=1.2.3.4 ./start.sh"
        return
    fi

    if [[ ${#candidates[@]} -eq 1 ]]; then
        export QUIZ_EXTERNAL_IP="${candidates[0]}"
        info "Detekovana LAN IP hosta: ${QUIZ_EXTERNAL_IP}"
        return
    fi

    echo ""
    warn "Nalezeno vice sitovych rozhrani:"
    echo ""
    local i=1
    for ip in "${candidates[@]}"; do
        if [[ $i -eq 1 ]]; then
            echo -e "  ${BOLD}${i})${NC} ${GREEN}${ip}${NC}  ${CYAN}<-- doporucena (hlavni sitove rozhrani)${NC}"
        else
            echo -e "  ${BOLD}${i})${NC} ${ip}"
        fi
        ((i++))
    done
    echo ""
    echo -n -e "Vyber IP pro zaky [${GREEN}1${NC}]: "

    local choice
    read -t 15 choice 2>/dev/null || choice=""
    choice="${choice:-1}"

    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#candidates[@]} ]]; then
        export QUIZ_EXTERNAL_IP="${candidates[$((choice-1))]}"
    else
        export QUIZ_EXTERNAL_IP="${candidates[0]}"
    fi
    info "Pouzivam IP: ${QUIZ_EXTERNAL_IP}"
}

# --- Startup info (shared by start.sh and rebuild.sh) ---
wait_and_print_info() {
    local PORT="${QUIZ_PORT:-8765}"
    local EXT_IP="${QUIZ_EXTERNAL_IP:-}"

    info "Cekam na spusteni serveru..."
    for i in $(seq 1 15); do
        if $DOCKER_CMD exec classrally python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/api/health')" &>/dev/null; then
            echo ""
            info "ClassRally bezi!"
            echo ""
            local BASE="${EXT_IP:-localhost}"
            echo -e "  ${GREEN}Host obrazovka:${NC}   http://${BASE}:${PORT}/host"
            echo -e "  ${GREEN}Admin portal:${NC}     http://${BASE}:${PORT}/admin"
            echo -e "  ${GREEN}Hracska stranka:${NC}  http://${BASE}:${PORT}/play"
            echo ""
            if [[ -z "$EXT_IP" ]]; then
                warn "LAN IP neni k dispozici - URL vyse ukazuji na localhost (jen tento pocitac)."
                echo ""
            fi

            local TOKEN
            TOKEN=$($DOCKER_CMD logs classrally 2>&1 | grep -oP 'HOST TOKEN: \K[a-f0-9]+' | tail -1 || true)
            if [[ -n "$TOKEN" ]]; then
                echo -e "  ${BOLD}${CYAN}Pristupovy klic: ${TOKEN}${NC}"
                echo -e "  (zadej na /host pro ovladani hry)"
                echo ""
            else
                echo -e "  Pristupovy klic: viz ${YELLOW}./logs.sh${NC} nebo ${YELLOW}/admin > Pokrocile nastaveni${NC}"
                echo ""
            fi

            echo -e "  Zastaveni:  ${YELLOW}./stop.sh${NC}"
            echo -e "  Logy:       ${YELLOW}./logs.sh${NC}"
            echo -e "  Rebuild:    ${YELLOW}./rebuild.sh${NC}"
            return 0
        fi
        printf "."
        sleep 1
    done
    echo ""
    warn "Server se nespustil vcas. Zkontroluj logy:"
    echo "  ./logs.sh"
    return 1
}
