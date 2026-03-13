#!/usr/bin/env bash
# ClassRally - Zastavi quiz server

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker

info "Zastavuji ClassRally..."
$COMPOSE_CMD down
info "Zastaveno."
