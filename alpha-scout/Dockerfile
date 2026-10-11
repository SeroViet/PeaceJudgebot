FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY alpha_scout ./alpha_scout
RUN pip install .

# Vorlage für die Konfiguration. Auf Render/Docker mit Disk: CONFIG_DIR=/app/data/config setzen,
# dann werden fehlende Dateien beim Start dorthin kopiert und bleiben bei Neustarts erhalten.
COPY config ./config
RUN mkdir -p /app/data

VOLUME ["/app/data"]
CMD ["python", "-m", "alpha_scout"]
