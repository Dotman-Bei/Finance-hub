# Subsystem 3 - Smart Exception Handling Module.
#
# Build context is the REPO ROOT (the service imports `shared.*`):
#
#   build:
#     context: .
#     dockerfile: services/exception_handler/Dockerfile

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /srv/shared/requirements.txt
COPY services/exception_handler/requirements.txt /srv/services/exception_handler/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/services/exception_handler/requirements.txt

COPY shared/ /srv/shared/
COPY services/__init__.py /srv/services/__init__.py
COPY services/exception_handler/ /srv/services/exception_handler/

# rf_classifier.pkl is written here by POST /models/train and by the Celery
# retrain task, so the directory must be writable by the unprivileged user.
RUN useradd --create-home --uid 10003 financehub \
 && mkdir -p /srv/services/exception_handler/models \
 && chown -R financehub:financehub /srv
USER financehub

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8003/health || exit 1

CMD ["uvicorn", "services.exception_handler.app.main:app", \
     "--host", "0.0.0.0", "--port", "8003"]
