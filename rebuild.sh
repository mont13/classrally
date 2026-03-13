#!/usr/bin/env bash
# ClassRally - Rebuild image a spusti znovu

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker

info "Rebuilduji image..."
$COMPOSE_CMD build --no-cache

# Create dirs if missing
mkdir -p history questions static/audio

# Detect host IP for player URLs
detect_host_ip

info "Spoustim ClassRally v Dockeru..."
$COMPOSE_CMD up -d --build

wait_and_print_info
