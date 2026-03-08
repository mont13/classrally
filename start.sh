#!/usr/bin/env bash
# QuizWeb - Spusti quiz server v Dockeru
# Pouziti:
#   ./start.sh
#   QUIZ_ADMIN_PASSWORD=heslo ./start.sh

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker

# Create dirs if missing
mkdir -p history questions static/audio

# Detect host IP for player URLs
detect_host_ip

info "Spoustim QuizWeb v Dockeru..."
$COMPOSE_CMD up -d

PORT="${QUIZ_PORT:-8765}"
EXT_IP="${QUIZ_EXTERNAL_IP:-}"

# Wait for health check
info "Cekam na spusteni serveru..."
for i in $(seq 1 15); do
    if $DOCKER_CMD exec quiz-web python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/api/health')" &>/dev/null; then
        echo ""
        info "QuizWeb bezi!"
        echo ""
        echo -e "  ${GREEN}Host obrazovka:${NC}   http://localhost:${PORT}/host"
        echo -e "  ${GREEN}Admin portal:${NC}     http://localhost:${PORT}/admin"
        echo ""
        if [[ -n "$EXT_IP" ]]; then
            echo -e "  ${BOLD}${YELLOW}Pro zaky (mobily):${NC}"
            echo -e "  ${BOLD}  http://${EXT_IP}:${PORT}/play${NC}"
            echo ""
        else
            warn "LAN IP neni k dispozici - zaci se pripoji na http://<tvoje-IP>:${PORT}/play"
            echo ""
        fi

        # Extract host token from container logs
        TOKEN=$($DOCKER_CMD logs quiz-web 2>&1 | grep -oP 'HOST TOKEN: \K[a-f0-9]+' | tail -1 || true)
        if [[ -n "$TOKEN" ]]; then
            echo -e "  ${BOLD}${CYAN}Host token: ${TOKEN}${NC}"
            echo -e "  (zadej na /host pro ovladani hry)"
            echo ""
        else
            echo -e "  Host token: viz ${YELLOW}./logs.sh${NC} nebo ${YELLOW}/admin > Nastaveni${NC}"
            echo ""
        fi

        echo -e "  Zastaveni:  ${YELLOW}./stop.sh${NC}"
        echo -e "  Logy:       ${YELLOW}./logs.sh${NC}"
        echo -e "  Rebuild:    ${YELLOW}./rebuild.sh${NC}"
        exit 0
    fi
    printf "."
    sleep 1
done
echo ""
warn "Server se nespustil vcas. Zkontroluj logy:"
echo "  ./logs.sh"
