#!/usr/bin/env bash
# QuizWeb - Zastavi quiz server

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker

info "Zastavuji quiz-web..."
$COMPOSE_CMD down
info "Zastaveno."
