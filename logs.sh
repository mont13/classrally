#!/usr/bin/env bash
# ClassRally - Zobrazi logy kontejneru

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker
$COMPOSE_CMD logs -f classrally
