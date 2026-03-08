#!/usr/bin/env bash
# QuizWeb - Zobrazi logy kontejneru

source "$(dirname "${BASH_SOURCE[0]}")/docker-common.sh"

check_docker
$COMPOSE_CMD logs -f quiz-web
