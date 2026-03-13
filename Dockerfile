FROM python:3.12-slim

LABEL maintainer="classrally"
LABEL description="ClassRally - Lokalni kvizova platforma pro tridu"

WORKDIR /app

# Copy application files
COPY server.py qrgen.py ./
COPY static/ static/
COPY questions/ questions/

# Create directories for volumes
RUN mkdir -p history static/audio

# Ensure Python output is not buffered (visible in docker logs immediately)
ENV PYTHONUNBUFFERED=1

# Default environment variables
ENV QUIZ_ADMIN_PASSWORD=""
ENV OLLAMA_HOST="host.docker.internal"
ENV OLLAMA_PORT="11434"
ENV OLLAMA_MODEL="gpt-oss:20b"
ENV QUIZ_HOST="0.0.0.0"
ENV QUIZ_PORT="8765"
ENV QUIZ_EXTERNAL_IP=""
ENV QUESTION_TIME="20"
ENV REVEAL_TIME="5"

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')" || exit 1

CMD python3 server.py \
    --host "${QUIZ_HOST}" \
    --port "${QUIZ_PORT}" \
    --admin-password "${QUIZ_ADMIN_PASSWORD}" \
    --external-ip "${QUIZ_EXTERNAL_IP}" \
    --ollama-host "${OLLAMA_HOST}" \
    --ollama-port "${OLLAMA_PORT}" \
    --ollama-model "${OLLAMA_MODEL}" \
    --question-time "${QUESTION_TIME}" \
    --reveal-time "${REVEAL_TIME}"
