# build.md — FinanceHub

**FinanceHub** — an Automated Financial Transaction Reconciliation and Reporting System.

This is the engineering build guide for the system specified in Chapters 1–3 of the project. It translates the four research objectives into an actionable, phase-by-phase implementation plan with the exact stack, schema, service contracts, and code scaffolding needed to ship a working product.

The thesis describes **what** and **why**. This document describes **how** and **in what order**.

---

## 1. What you're building

FinanceHub is a single cloud-based ecosystem with four cooperating subsystems talking over RESTful APIs and backed by one PostgreSQL database that acts as the single source of truth.

| # | Subsystem | Objective it satisfies | Core responsibility |
|---|-----------|------------------------|---------------------|
| 1 | Hybrid Matching Engine | Obj. 1 | Reconcile high-volume transactions from disparate sources using rule-based + unsupervised ML matching |
| 2 | Real-Time Validation Pipeline | Obj. 2 | Detect/quarantine ≥98% of structural inconsistencies at ingestion, before data hits the DB |
| 3 | Smart Exception Handling Module | Obj. 3 | Classify unmatched transactions into 4 categories and suggest remediations, with a learning feedback loop |
| 4 | Web-Based Reporting Dashboard | Obj. 4 | On-demand visibility: KPIs, match-rate charts, exception management, audit-ready PDF reports |

Data flows in one direction: **external sources → validation pipeline → matching engine → (matched → ledger) / (unmatched → exception queue) → dashboard**.

---

## 2. Technology stack

Locked to the choices named in Chapter 3. Do not substitute without a reason — the thesis defends these specifically.

**Backend / services**
- Python 3.11+
- FastAPI (async REST gateway + service endpoints, OpenAPI docs)
- Pandas (normalization / transformation)
- Scikit-learn (K-Means, DBSCAN, Local Outlier Factor for matching; Random Forest for exception classification)
- Pydantic (schema enforcement in the validation pipeline)
- Great Expectations (business-rule data-quality checks)
- SQLAlchemy ORM + psycopg2 (PostgreSQL access)
- Apache Kafka (event-streaming ingestion backbone)
- Redis (validation-result cache + dashboard query cache)
- Celery (periodic classifier retraining jobs)
- ReportLab + Jinja2 (audit-ready PDF generation)

**Frontend**
- React.js (decoupled single-page app)
- Chart.js + Recharts (bar / pie / time-series visualizations)
- Tailwind CSS (responsive styling)
- Axios (HTTP client)
- WebSocket client (live exception notifications)

**Infrastructure**
- PostgreSQL 15+ (single source of truth)
- Docker + Docker Compose (containerization, reproducible environments)

---

## 3. Repository structure

Monorepo. Each subsystem is an independently deployable service so the "microservices-by-design" architecture from §3.4.3 holds.

```
financehub/
├── docker-compose.yml
├── .env.example
├── build.md
├── db/
│   ├── schema.sql                # DDL for all 6 entities
│   └── migrations/               # alembic migrations
├── shared/
│   └── models/                   # Pydantic + SQLAlchemy models shared across services
│       ├── transaction.py
│       ├── match_result.py
│       └── enums.py
├── services/
│   ├── validation_pipeline/      # Subsystem 2
│   │   ├── app/
│   │   │   ├── main.py           # FastAPI entrypoint
│   │   │   ├── ingestion.py      # Kafka consumer / ETL staging
│   │   │   ├── schema_validator.py
│   │   │   ├── rule_processor.py # Great Expectations suite
│   │   │   ├── quarantine.py
│   │   │   └── cache.py          # Redis
│   │   ├── expectations/         # GE suites (JSON)
│   │   ├── tests/
│   │   └── Dockerfile
│   ├── matching_engine/          # Subsystem 1
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── rule_engine.py    # deterministic layer
│   │   │   ├── ml_model.py       # unsupervised layer
│   │   │   ├── pipeline.py       # sequential rule → ML orchestration
│   │   │   └── scoring.py        # confidence + threshold
│   │   ├── models/               # persisted .pkl clustering models
│   │   ├── tests/
│   │   └── Dockerfile
│   ├── exception_handler/        # Subsystem 3
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── classifier.py     # Random Forest
│   │   │   ├── resolution.py     # category → suggested action mapping
│   │   │   ├── feedback.py       # capture human decisions
│   │   │   └── retrain.py        # Celery task
│   │   ├── models/
│   │   ├── tests/
│   │   └── Dockerfile
│   └── reporting_api/            # Subsystem 4 backend
│       ├── app/
│       │   ├── main.py
│       │   ├── metrics.py        # aggregation endpoints
│       │   ├── reports.py        # ReportLab PDF service
│       │   ├── auth.py           # RBAC
│       │   └── ws.py             # WebSocket notifications
│       ├── templates/            # Jinja2 report templates
│       ├── tests/
│       └── Dockerfile
└── frontend/                     # Subsystem 4 UI
    ├── src/
    │   ├── components/
    │   │   ├── KpiSummaryCards.jsx
    │   │   ├── MatchRateChart.jsx
    │   │   ├── ExceptionPanel.jsx
    │   │   └── ReportsPanel.jsx
    │   ├── api/axiosClient.js
    │   ├── hooks/useWebSocket.js
    │   └── App.jsx
    ├── tailwind.config.js
    └── Dockerfile
```

---

## 4. Prerequisites

- Docker & Docker Compose
- Python 3.11+ and Node.js 20+ (for running services outside containers during dev)
- `make` (optional, for the shortcut targets below)

Clone, copy env, and bring up infra:

```bash
cp .env.example .env      # fill in POSTGRES_*, REDIS_URL, KAFKA_BROKER, JWT_SECRET
docker compose up -d postgres redis kafka
```

`.env.example` keys:

```
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=financehub
POSTGRES_USER=financehub
POSTGRES_PASSWORD=changeme
DATABASE_URL=postgresql+psycopg2://financehub:changeme@postgres:5432/financehub
REDIS_URL=redis://redis:6379/0
KAFKA_BROKER=kafka:9092
KAFKA_TOPIC_RAW=raw_transactions
MATCH_CONFIDENCE_THRESHOLD=0.85
RETRAIN_TRIGGER_COUNT=200
JWT_SECRET=change-this
```

---

## 5. Database schema — build this first

Everything depends on the shared PostgreSQL schema (the ER diagram, Figure 3.5). The six entities:

`TRANSACTIONS`, `MATCHEDRECORDS`, `EXCEPTIONQUEUE`, `LEDGERENTRIES`, `VALIDATIONLOGS`, `AUDITTRAIL`.

`db/schema.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TYPE match_status   AS ENUM ('MATCHED', 'UNMATCHED');
CREATE TYPE match_type      AS ENUM ('RULE', 'ML');
CREATE TYPE exception_cat   AS ENUM ('PARTIAL_PAYMENT', 'SPLIT_SETTLEMENT',
                                     'MISSING_REFERENCE_CODE', 'TIMING_DIFFERENCE');
CREATE TYPE exception_state AS ENUM ('OPEN', 'SUGGESTED', 'RESOLVED', 'REJECTED');
CREATE TYPE validation_state AS ENUM ('PASSED', 'QUARANTINED');

-- Clean, validated financial records (single source of truth)
CREATE TABLE transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT,
    source_type     TEXT NOT NULL,               -- bank_api | payment_gateway | erp
    amount          NUMERIC(18,2) NOT NULL,
    currency        CHAR(3) NOT NULL DEFAULT 'USD',
    txn_date        DATE NOT NULL,
    description     TEXT,
    reference_code  TEXT,
    raw_payload     JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_txn_ref    ON transactions (reference_code);
CREATE INDEX idx_txn_amount ON transactions (amount, txn_date);

-- Confirmed pairs from the matching engine
CREATE TABLE matchedrecords (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id    UUID NOT NULL REFERENCES transactions(id),
    counterpart_id    UUID NOT NULL REFERENCES transactions(id),
    match_type        match_type  NOT NULL,
    confidence_score  NUMERIC(5,4) NOT NULL,
    matched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unmatched items awaiting triage / resolution
CREATE TABLE exceptionqueue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id       UUID NOT NULL REFERENCES transactions(id),
    category             exception_cat,
    state                exception_state NOT NULL DEFAULT 'OPEN',
    classifier_confidence NUMERIC(5,4),
    suggested_resolution JSONB,
    resolved_by          TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at          TIMESTAMPTZ
);

-- Posted ledger entries derived from matched/resolved records
CREATE TABLE ledgerentries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID NOT NULL REFERENCES transactions(id),
    entry_type      TEXT NOT NULL,               -- debit | credit
    amount          NUMERIC(18,2) NOT NULL,
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every ingestion decision (pass or quarantine) for full traceability
CREATE TABLE validationlogs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID REFERENCES transactions(id),   -- null if quarantined pre-insert
    stage           TEXT NOT NULL,               -- schema | business_rule | checksum
    status          validation_state NOT NULL,
    violations      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Immutable, tamper-evident action log
CREATE TABLE audittrail (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type  TEXT NOT NULL,
    entity_id    UUID NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL,                  -- system | <username>
    old_state    JSONB,
    new_state    JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PostgreSQL trigger: auto-log every exception state change to audittrail (§3.3.2)
CREATE OR REPLACE FUNCTION log_exception_change() RETURNS trigger AS $$
BEGIN
    INSERT INTO audittrail (entity_type, entity_id, action, actor, old_state, new_state)
    VALUES ('exceptionqueue', NEW.id, TG_OP, COALESCE(NEW.resolved_by, 'system'),
            to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_exception_audit
    AFTER UPDATE ON exceptionqueue
    FOR EACH ROW EXECUTE FUNCTION log_exception_change();
```

Apply it:

```bash
docker compose exec -T postgres psql -U financehub -d financehub < db/schema.sql
```

---

## 6. Shared models

Define the canonical `Transaction` and `MatchResult` once (Figure 3.4) and import everywhere. This prevents schema drift between services.

`shared/models/enums.py`:

```python
from enum import Enum

class MatchStatus(str, Enum):
    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"

class MatchType(str, Enum):
    RULE = "RULE"
    ML = "ML"

class ExceptionCategory(str, Enum):
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    SPLIT_SETTLEMENT = "SPLIT_SETTLEMENT"
    MISSING_REFERENCE_CODE = "MISSING_REFERENCE_CODE"
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
```

`shared/models/transaction.py` (Pydantic — doubles as the validation schema):

```python
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator

class Transaction(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    external_id: str | None = None
    source_type: str
    amount: Decimal
    currency: str = "USD"
    txn_date: date
    description: str | None = None
    reference_code: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("currency")
    @classmethod
    def currency_len(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        return v.upper()

class MatchResult(BaseModel):
    transaction_id: UUID
    counterpart_id: UUID | None
    status: str          # MatchStatus
    match_type: str | None
    confidence_score: float
```

---

## 7. Build order (phased)

Build bottom-up so each phase produces something testable against the DB.

**Phase 0 — Foundation.** Schema (§5), shared models (§6), Docker Compose with Postgres + Redis + Kafka up and healthy.

**Phase 1 — Validation pipeline (Subsystem 2).** It's the front door; nothing reaches the DB without it. Build it first so all downstream data is clean.

**Phase 2 — Matching engine (Subsystem 1).** Consumes validated transactions, produces matched records + feeds the exception queue.

**Phase 3 — Exception handler (Subsystem 3).** Drains the exception queue, classifies, suggests resolutions.

**Phase 4 — Reporting API + frontend (Subsystem 4).** Reads everything the prior phases wrote.

**Phase 5 — Feedback loop + retraining.** Close the loop from human resolutions back into the classifier.

**Phase 6 — Hardening.** Load/latency tests, PDF audit reports, RBAC, WebSocket alerts.

---

## 8. Phase 1 — Real-Time Validation Pipeline

Four sequential stages (§3.2.1). Compliant records advance; non-compliant records are quarantined at the failing stage and logged.

```
ETL ingestion (Kafka) → schema validation (Pydantic) → business rules (Great Expectations)
    → checksum verify → INSERT into transactions + VALIDATIONLOGS(PASSED)
         │
         └─ on failure at any stage → quarantine partition + VALIDATIONLOGS(QUARANTINED)
```

**Ingestion.** Kafka consumer reads `raw_transactions`, buffers to a staging structure, hands each payload to the validator. Accept CSV and JSON payloads; normalize with Pandas to the canonical schema before validation.

`services/validation_pipeline/app/schema_validator.py`:

```python
from pydantic import ValidationError
from shared.models.transaction import Transaction

def validate_schema(payload: dict) -> tuple[Transaction | None, list[str]]:
    try:
        return Transaction(**payload), []
    except ValidationError as e:
        return None, [f"{err['loc']}: {err['msg']}" for err in e.errors()]
```

**Business rules** with Great Expectations — logical checks beyond structure: amount positive, `txn_date` not in the future, `reference_code` matches an approved format, currency in an allowed set. Define a GE suite in `expectations/` and run it per record batch.

**Checksum.** If the source provides a checksum/signature, verify it here to catch in-transit corruption before commit.

**Caching.** Cache frequently repeated schema/rule decisions in Redis keyed by a payload fingerprint to cut recomputation on high-velocity streams.

**Persistence.** On pass → `INSERT INTO transactions` + `validationlogs(status='PASSED')`. On fail → write to the quarantine schema partition + `validationlogs(status='QUARANTINED', violations=...)` and emit a dashboard alert.

**Acceptance test (ties to the 98% objective).** Feed a labeled corpus of good + deliberately malformed records; assert the pipeline quarantines ≥98% of the malformed set with zero good records lost. Put this in `tests/test_detection_rate.py` and treat it as a release gate.

---

## 9. Phase 2 — Hybrid Matching Engine

Two layers in one sequential pipeline (§3.1.1). Deterministic first; only the leftovers go to ML.

`services/matching_engine/app/rule_engine.py` — Layer 1 (exact match on ID + amount + date):

```python
import pandas as pd

def rule_match(internal: pd.DataFrame, external: pd.DataFrame):
    keys = ["reference_code", "amount", "txn_date"]
    merged = internal.merge(external, on=keys, how="inner", suffixes=("_int", "_ext"))
    matched_ids = set(merged["id_int"]) | set(merged["id_ext"])
    unmatched = pd.concat([
        internal[~internal["id"].isin(matched_ids)],
        external[~external["id"].isin(matched_ids)],
    ])
    return merged, unmatched     # merged → confirmed; unmatched → ML layer
```

`services/matching_engine/app/ml_model.py` — Layer 2 (unsupervised clustering + outlier detection):

```python
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import DBSCAN
from sklearn.neighbors import LocalOutlierFactor

def fuzzy_match(unmatched_df):
    # vectorize normalized descriptions, cluster semantically-similar txns,
    # flag isolated points as true outliers (real exceptions, not near-matches)
    vecs = TfidfVectorizer().fit_transform(unmatched_df["description"].fillna(""))
    clusters = DBSCAN(eps=0.4, min_samples=2, metric="cosine").fit_predict(vecs)
    lof = LocalOutlierFactor(n_neighbors=5).fit_predict(vecs.toarray())
    return clusters, lof   # same cluster → candidate pair; LOF=-1 → send to exception queue
```

**Scoring & threshold (§3.1.1).** Every candidate pair gets a confidence score. Only pairs above `MATCH_CONFIDENCE_THRESHOLD` (from `.env`) persist to `matchedrecords`; everything else goes to `exceptionqueue`. Keeping the threshold configurable is how you suppress the false positives the literature warns about.

**Persistence.** Matched → `matchedrecords` (with `match_type` RULE or ML) and a corresponding `ledgerentries` posting. Unmatched → `exceptionqueue(state='OPEN')`.

**API.** FastAPI `POST /reconcile` accepts a batch, runs rule → ML → scoring, returns a summary `{matched, unmatched, match_rate}`. Async endpoints so high transaction counts don't block.

**Model persistence.** Fit clustering models on historical data offline; save to `models/*.pkl`; load at service start.

---

## 10. Phase 3 — Smart Exception Handling Module

A supervised classifier + a resolution engine + a human-in-the-loop feedback cycle (§3.3).

**Classifier.** Random Forest (robust to the class imbalance typical of exception data). Features engineered from each unmatched transaction: amount ratio vs. nearest candidate, description similarity, presence of reference code, date delta. Output is one of four categories.

`services/exception_handler/app/classifier.py`:

```python
import joblib
from shared.models.enums import ExceptionCategory

CATEGORIES = [c.value for c in ExceptionCategory]

class ExceptionClassifier:
    def __init__(self, path="models/rf_classifier.pkl"):
        self.model = joblib.load(path)

    def classify(self, features):
        proba = self.model.predict_proba([features])[0]
        idx = proba.argmax()
        return CATEGORIES[idx], float(proba[idx])
```

**Resolution engine (§3.3.1).** Category → suggested action mapping:

| Category | Suggested resolution pathway |
|----------|------------------------------|
| Partial Payment | Propose partial-match journal entry; flag remaining balance for follow-up |
| Split Settlement | Open multi-line allocation resolution across the target obligations |
| Missing Reference Code | Surface likely counterpart candidates for manual reference assignment |
| Timing Difference | Suggest matching across accounting periods; hold pending settlement date |

Write the suggestion into `exceptionqueue.suggested_resolution` (JSONB) and set `state='SUGGESTED'`.

**API.** FastAPI exception routes let the dashboard query, display, and update exceptions in real time: `GET /exceptions`, `POST /exceptions/{id}/resolve` (accept/reject/edit).

**Feedback capture.** Every human decision (accept/reject/edit) is recorded; the `audittrail` trigger logs the state change automatically. These decisions become new labeled training data.

---

## 11. Phase 5 — Feedback loop & retraining

A Celery beat task periodically pulls all human-resolved exceptions and retrains the Random Forest once resolved-count crosses `RETRAIN_TRIGGER_COUNT`.

`services/exception_handler/app/retrain.py`:

```python
from celery import Celery
from sklearn.ensemble import RandomForestClassifier
import joblib

celery = Celery("retrain", broker="redis://redis:6379/1")

@celery.task
def retrain_if_ready():
    samples = fetch_resolved_exceptions()          # from exceptionqueue where state='RESOLVED'
    if len(samples) < RETRAIN_TRIGGER_COUNT:
        return
    X, y = build_training_matrix(samples)
    model = RandomForestClassifier(class_weight="balanced", n_estimators=200)
    model.fit(X, y)
    joblib.dump(model, "models/rf_classifier.pkl")  # hot-swapped on next classify()
```

This is the "gradually becomes more precise" mechanism from §3.3.1 — accuracy improves and manual intervention drops with each reconciliation round.

---

## 12. Phase 4 & 6 — Reporting API + Dashboard

**Backend (`reporting_api`).** FastAPI gateway exposing reporting-only endpoints that aggregate from Postgres. A Redis cache sits between the gateway and the DB so repeated dashboard polls don't hammer the database.

Endpoints:
- `GET /metrics/kpi` → total volume, overall match rate, open exceptions, reconciliation status
- `GET /metrics/match-rate?from=&to=` → time-series for charts
- `GET /exceptions` → sortable/filterable exception list with categories + suggested actions
- `POST /reports/generate` → produce an audit-ready PDF, store under a report ID in Postgres
- `WS /ws/exceptions` → push new exceptions live so teams don't manually refresh

**RBAC (§3.4.1).** Three roles — Finance Manager, Auditor, System Administrator — each with a scoped view. Enforce at the API layer with JWT + role claims.

**PDF reports (§3.4.2).** Jinja2 renders the report layout; ReportLab produces the PDF containing reconciliation summaries, exception logs, and match-rate analytics. Persist every generated report with its ID for provenance.

**Frontend (`frontend`).** React SPA, four panels matching §3.4.1:

- `KpiSummaryCards` — live KPI tiles
- `MatchRateChart` — Recharts bar + time-series line
- `ExceptionPanel` — filterable table; approve/reject/edit suggestions inline
- `ReportsPanel` — trigger + download PDF reports

Axios for REST, a `useWebSocket` hook for live exception alerts, Tailwind for responsive layout across desktop/tablet/mobile.

---

## 13. Docker Compose

`docker-compose.yml` (skeleton — one service block per subsystem):

```yaml
services:
  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: financehub
      POSTGRES_USER: financehub
      POSTGRES_PASSWORD: changeme
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]

  redis:
    image: redis:7
    ports: ["6379:6379"]

  kafka:
    image: bitnami/kafka:latest
    environment:
      KAFKA_CFG_NODE_ID: 0
      KAFKA_CFG_PROCESS_ROLES: controller,broker
    ports: ["9092:9092"]

  validation_pipeline:
    build: ./services/validation_pipeline
    env_file: .env
    depends_on: [postgres, redis, kafka]

  matching_engine:
    build: ./services/matching_engine
    env_file: .env
    depends_on: [postgres, validation_pipeline]

  exception_handler:
    build: ./services/exception_handler
    env_file: .env
    depends_on: [postgres, matching_engine, redis]

  reporting_api:
    build: ./services/reporting_api
    env_file: .env
    ports: ["8000:8000"]
    depends_on: [postgres, redis]

  frontend:
    build: ./frontend
    ports: ["3000:3000"]
    depends_on: [reporting_api]

volumes:
  pgdata:
```

Bring the whole system up:

```bash
docker compose up --build
```

---

## 14. Testing strategy (maps to the hypotheses)

Each objective has a measurable release gate. Wire these into CI.

| Test | Target | Where |
|------|--------|-------|
| Detection rate | ≥98% of malformed records quarantined, 0 valid records lost | `validation_pipeline/tests/test_detection_rate.py` |
| Match precision | Rule + ML matched pairs vs. labeled ground truth; track false-positive rate as threshold varies | `matching_engine/tests/test_precision.py` |
| Classifier accuracy | Per-category precision/recall on held-out exception set | `exception_handler/tests/test_classifier.py` |
| Latency | Reconcile N transactions under a fixed p95 budget; assert no regression | `matching_engine/tests/test_latency.py` |
| Feedback improvement | Classifier accuracy after retraining ≥ accuracy before, on rolling data | `exception_handler/tests/test_feedback.py` |

Generate synthetic high-volume datasets (the thesis references large synthetic corpora) so you can exercise scalability without real financial data.

---

## 15. Local dev quickstart

```bash
# infra
docker compose up -d postgres redis kafka
docker compose exec -T postgres psql -U financehub -d financehub < db/schema.sql

# one service at a time during dev
cd services/validation_pipeline && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8001
cd services/matching_engine    && uvicorn app.main:app --reload --port 8002
cd services/exception_handler  && uvicorn app.main:app --reload --port 8003
cd services/reporting_api      && uvicorn app.main:app --reload --port 8000

# frontend
cd frontend && npm install && npm run dev
```

---

## 16. Milestone checklist

- [ ] **Phase 0** — schema applied, shared models importable, infra healthy
- [ ] **Phase 1** — validation pipeline quarantines ≥98% malformed; logs to `validationlogs`
- [ ] **Phase 2** — rule + ML matching persists to `matchedrecords`; unmatched → `exceptionqueue`; threshold configurable
- [ ] **Phase 3** — classifier assigns all 4 categories; resolution suggestions written to queue
- [ ] **Phase 4** — dashboard shows live KPIs, match-rate charts, exception panel, PDF reports
- [ ] **Phase 5** — human resolutions retrain the classifier via Celery
- [ ] **Phase 6** — RBAC enforced, WebSocket alerts live, latency + audit-report gates passing

Ship Phase 1 → 4 for a working demo; Phases 5–6 make it defensible against the thesis's own hypotheses.
