# Subsystem 4 backend - Reporting API gateway.
#
# Build context is the REPO ROOT (the service imports `shared.*`):
#
#   build:
#     context: .
#     dockerfile: services/reporting_api/Dockerfile

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt /srv/shared/requirements.txt
COPY services/reporting_api/requirements.txt /srv/services/reporting_api/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /srv/services/reporting_api/requirements.txt

COPY shared/ /srv/shared/
COPY services/__init__.py /srv/services/__init__.py
COPY services/reporting_api/ /srv/services/reporting_api/

RUN useradd --create-home --uid 10004 financehub \
 && chown -R financehub:financehub /srv
USER financehub

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# --ws websockets: uvicorn's default wsproto does not handle the concurrent
# long-lived connections the dashboard opens.
CMD ["uvicorn", "services.reporting_api.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--ws", "websockets"]
