# Subsystem 1 - Hybrid Matching Engine.
#
# Build context is the REPO ROOT (the service imports `shared.*`):
#
#   build:
#     context: .
#     dockerfile: services/matching_engine/Dockerfile

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv \
    # scikit-learn's BLAS spawns a thread pool per process. Uvicorn already
    # runs the CPU-bound work in a threadpool, so letting BLAS oversubscribe
    # on top of that makes reconciliation slower, not faster.
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /srv/shared/requirements.txt
COPY services/matching_engine/requirements.txt /srv/services/matching_engine/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/services/matching_engine/requirements.txt

COPY shared/ /srv/shared/
COPY services/__init__.py /srv/services/__init__.py
COPY services/matching_engine/ /srv/services/matching_engine/

# Fitted .pkl models are written here at runtime by POST /models/fit, so the
# directory must be writable by the unprivileged user.
RUN useradd --create-home --uid 10002 financehub \
 && mkdir -p /srv/services/matching_engine/models \
 && chown -R financehub:financehub /srv
USER financehub

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8002/health || exit 1

CMD ["uvicorn", "services.matching_engine.app.main:app", \
     "--host", "0.0.0.0", "--port", "8002"]
