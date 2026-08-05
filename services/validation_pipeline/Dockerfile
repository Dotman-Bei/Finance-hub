# Subsystem 2 - Real-Time Validation Pipeline.
#
# Build context is the REPO ROOT, not this directory, because the service
# imports `shared.*`. docker-compose.yml sets it explicitly:
#
#   build:
#     context: .
#     dockerfile: services/validation_pipeline/Dockerfile

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

WORKDIR /srv

# psycopg2-binary ships wheels, but GE's transitive deps still want a compiler
# on some platforms. curl is here for the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

# Requirements first so the dependency layer caches across code edits.
COPY shared/requirements.txt /srv/shared/requirements.txt
COPY services/validation_pipeline/requirements.txt /srv/services/validation_pipeline/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/services/validation_pipeline/requirements.txt

COPY shared/ /srv/shared/
COPY services/__init__.py /srv/services/__init__.py
COPY services/validation_pipeline/ /srv/services/validation_pipeline/

# Drop privileges: nothing here needs root at runtime.
RUN useradd --create-home --uid 10001 financehub \
 && chown -R financehub:financehub /srv
USER financehub

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["uvicorn", "services.validation_pipeline.app.main:app", \
     "--host", "0.0.0.0", "--port", "8001"]
