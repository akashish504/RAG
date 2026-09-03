# Install extras via: docker compose build --build-arg PIP_EXTRAS=parsers,airtable_ingestion (omit dev on small hosts)
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
    && apt-get install -y libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

ARG PIP_EXTRAS="dev,parsers,airtable_ingestion,api,voyage"

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ".[${PIP_EXTRAS}]"

COPY . .

CMD ["python", "scripts/run_pipeline.py", "--help"]
