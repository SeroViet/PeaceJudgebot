FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY alpha_scout ./alpha_scout
RUN pip install .

COPY config ./config
RUN useradd --create-home scout && mkdir -p /app/data && chown -R scout /app
USER scout

VOLUME ["/app/data"]
CMD ["python", "-m", "alpha_scout"]
