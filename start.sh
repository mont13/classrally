#!/usr/bin/env bash
# ClassRally - Spusti quiz server v Dockeru
# Pouziti:
#   ./start.sh
#   QUIZ_ADMIN_PASSWORD=heslo ./start.sh

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker

# Create dirs if missing
mkdir -p history questions static/audio

# Detect host IP for player URLs
detect_host_ip

info "Spoustim ClassRally v Dockeru..."
$COMPOSE_CMD up -d

wait_and_print_info
