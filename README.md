# FinanceHub

> **Picking this up cold?** Read [HANDOFF.md](HANDOFF.md) first. It records what
> is genuinely verified versus what only looks verified, the invariants that
> must not be broken, and what to build next in priority order.

Automated Financial Transaction Reconciliation and Reporting System.
Built to [build.md](build%20%281%29.md) — four subsystems over RESTful APIs,
backed by one PostgreSQL database as the single source of truth.

**No mock data anywhere.** Services read real data from real dependencies or
fail visibly. Synthetic corpora exist only inside the release-gate tests
build.md §14 calls for.

---

## Status

| Phase | Scope | Status |
|-------|-------|--------|
| **0** | Schema, shared models, infra | ✅ **done** |
| **1** | Validation pipeline (Subsystem 2) | ✅ **done** — gate: 100% detection, 0% false positive |
| **2** | Matching engine (Subsystem 1) | ✅ **done** — gate: 100% precision, 98% coverage, p95 583ms |
| **3** | Exception handler (Subsystem 3) | ✅ **done** — gate: forest macro F1 1.00, bootstrap 0.89 |
| **4** | Reporting API (Subsystem 4 backend) | ✅ **done** — RBAC, ReportLab PDFs, live WS relay |
| **4** | Dashboard (Subsystem 4 UI) | ✅ **done** — mock layer removed, reads the live gateway |
| **5** | Feedback loop + retraining | ✅ **done** — gate: 0.84 → 1.00 over rolling rounds |
| **6** | Hardening | ✅ **done** — audit gate, e2e, load test, CI |
| **—** | Whole-system verification | ✅ **done** — 318 tests green, every endpoint exercised on the running deployment, dashboard driven in a browser |
| **—** | Deployed and run as real services | ✅ **done** — VPS behind TLS at https://financehub-demo.duckdns.org — six systemd units, data through every hop, RBAC and audit trigger verified live |

---

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r shared/requirements.txt   # Windows
# .venv/bin/python -m pip install -r shared/requirements.txt     # macOS/Linux

cp .env.example .env
docker compose up -d          # postgres + redis + kafka
docker compose ps             # all three should read (healthy)
.venv/Scripts/python -m pytest
```

Postgres applies [db/schema.sql](db/schema.sql) automatically through its
entrypoint on a fresh volume, so there is no manual schema step. `make` targets
wrap all of the above — run `make help`.

---

## Layout

```
financehub/
├── docker-compose.yml     # postgres, redis, kafka (healthchecked)
├── .env.example           # every key build.md §4 defines
├── db/
│   ├── schema.sql         # canonical DDL: 6 tables, 5 enums, audit trigger
│   ├── alembic.ini
│   └── migrations/        # 0001_baseline replays schema.sql
├── shared/
│   ├── config.py          # the one .env loader
│   ├── db.py              # engine, session_scope, healthcheck
│   ├── models/
│   │   ├── enums.py       # mirrors the Postgres types 1:1
│   │   ├── transaction.py # Pydantic; doubles as the §8 validation schema
│   │   ├── match_result.py
│   │   └── orm.py         # SQLAlchemy mappings onto schema.sql
│   └── tests/
├── services/
│   └── validation_pipeline/    # Subsystem 2 (Sec. 8)
│       ├── app/
│       │   ├── ingestion.py        # stage 0: Kafka + Pandas CSV/JSON normalisation
│       │   ├── schema_validator.py # stage 1: Pydantic
│       │   ├── rule_processor.py   # stage 2: Great Expectations
│       │   ├── checksum.py         # stage 3: checksum / HMAC
│       │   ├── quarantine.py       # stage 4: persistence + quarantine + alert
│       │   ├── pipeline.py         # orchestration (no I/O)
│       │   ├── cache.py            # Redis fingerprint cache
│       │   └── main.py             # FastAPI, port 8001
│       ├── expectations/           # the GE suite, as JSON
│       └── tests/                  # incl. test_detection_rate.py (release gate)
│   └── matching_engine/        # Subsystem 1 (Sec. 9)
│       ├── app/
│       │   ├── rule_engine.py      # layer 1: exact reference+amount+date
│       │   ├── ml_model.py         # layer 2: TF-IDF + DBSCAN + LOF + blocking
│       │   ├── scoring.py          # confidence + configurable threshold
│       │   ├── pipeline.py         # rule -> ML orchestration (no I/O)
│       │   ├── persistence.py      # matchedrecords + ledgerentries + exceptions
│       │   └── main.py             # FastAPI, port 8002
│       ├── models/                 # persisted .pkl (gitignored)
│       └── tests/                  # test_precision.py, test_latency.py (gates)
│   └── exception_handler/      # Subsystem 3 (Sec. 10) + feedback loop (Sec. 11)
│       ├── app/
│       │   ├── features.py         # the 4 features Sec. 10 names, + corroborating
│       │   ├── classifier.py       # Random Forest + cold-start bootstrap rules
│       │   ├── resolution.py       # Sec. 10's category -> pathway table
│       │   ├── feedback.py         # queue access + human-decision capture
│       │   ├── retrain.py          # Celery beat task; hot-swaps the .pkl
│       │   └── main.py             # FastAPI, port 8003
│       ├── models/                 # rf_classifier.pkl (gitignored)
│       └── tests/                  # test_classifier.py, test_feedback.py (gates)
│   └── reporting_api/          # Subsystem 4 backend (Sec. 12)
│       ├── app/
│       │   ├── metrics.py          # KPI + match-rate aggregation, Redis-cached
│       │   ├── reports.py          # Jinja2 -> ReportLab audit PDFs
│       │   ├── auth.py             # JWT + the 3 roles of Sec. 3.4.1
│       │   ├── audit.py            # audittrail reads for the Auditor role
│       │   ├── ws.py               # WS /ws/exceptions live relay
│       │   └── main.py             # FastAPI gateway, port 8000
│       ├── templates/              # report layouts
│       └── tests/
├── frontend/              # Subsystem 4 UI — React SPA, port 3000
│   └── src/
│       ├── components/         # KpiSummaryCards, MatchRateChart,
│       │                       # ExceptionPanel, ReportsPanel
│       ├── hooks/useWebSocket.js   # live exception alerts
│       └── api/                # Axios client + response normalisation
└── tests/                 # cross-subsystem: end-to-end, integration (DB)
```

### Cold start

A Random Forest needs labels that do not exist on day one. Rather than ship a
model fitted on invented data, `classifier.py` serves a **deterministic
bootstrap rule set** over the same real features until human decisions
accumulate. Every suggestion records which engine produced it
(`engine: "bootstrap" | "random_forest"`), so a reviewer can weigh them
differently and no consumer can mistake a rule for a learned prediction.
Rejections are never used as training labels — a rejection says the suggestion
was wrong, not what was right.

### Release gates

| Objective | Gate | Where | Status |
|---|---|---|---|
| 2 | >=98% malformed quarantined, 0 valid lost | `validation_pipeline/tests/test_detection_rate.py` | ✅ 100% / 0% |
| 1 | Match precision vs. labelled ground truth | `matching_engine/tests/test_precision.py` | ✅ 100% precision at every threshold 0.50–0.95, 0% false positives, 98.5% coverage |
| 1 | p95 latency + no superlinear scaling | `matching_engine/tests/test_latency.py` | ✅ 583ms / 600 txns |
| 3 | Per-category precision/recall, held-out | `exception_handler/tests/test_classifier.py` | ✅ forest 1.00, rules 0.89 |
| 3 | Accuracy after retraining ≥ before | `exception_handler/tests/test_feedback.py` | ✅ monotonic, 0.84 → 1.00 |
| — | Audit trail complete (trigger fires) | `tests/test_integration_db.py` | ✅ 18/18 against real PostgreSQL 16.14 |
| — | End-to-end across four subsystems | `tests/test_end_to_end.py` | ✅ |
| 1 | Load at month-end volume | `matching_engine/tests/test_latency.py` | ✅ 6000 txns, 0.74ms/txn |

Gate numbers are measured on synthetic corpora (build.md §14 sanctions these
for tests). They bound what the code can do on data shaped like the
assumptions; they are not production estimates. The classifier gate is
deliberately measured on a corpus that is **50% ambiguous cases** — on cleanly
separable data both engines score 1.00, which measures the corpus rather than
the classifier.

`tests/test_integration_db.py` skips itself when no database is reachable, so a
developer without Docker still gets a green local run. CI provides PostgreSQL
and **fails the build if those tests skip** — otherwise the audit trigger, on
which §3.3.2's tamper-evidence entirely rests, would go untested everywhere.

### Co-settling nomination: how `SPLIT_SETTLEMENT` became reachable

The classifier reaches `SPLIT_SETTLEMENT` only when `counterpart_count >= 2`
([classifier.py:106](services/exception_handler/app/classifier.py#L106)), and
the engine used to record one best counterpart per unmatched row — so the
count never exceeded 1 and one of Objective 3's four categories could not
occur in the assembled system. Every isolated test passed anyway, because
`exception_corpus.py` builds features from a hand-made counterpart list and
never runs the matcher. Seeded data through the real path exposed it: three
categories appeared and this one never did.

The engine now records every co-settling candidate under
`suggested_resolution.matching_engine.candidate_ids` — the key `_nominated_ids`
already read. Two details make it correct rather than merely non-empty:

- **Candidates come from the pre-resolution scored set.** `resolve_one_to_one`
  keeps a single pair per transaction, which is right for deciding matches and
  destroys exactly what is needed here — a split settlement is the case where
  the pairs it discards are the other legs of the same obligation.
- **Nomination is filtered by arithmetic, not similarity.** Multiplicity *is*
  the definition of a split, so a loose filter relabels ordinary partial
  payments as splits — worse than nominating nothing. Confidence cannot carry
  that weight: the legs of a split and an unrelated invoice from the same
  counterparty are similar in the same way. Legs must each be a fraction of
  the obligation (`LEG_MAX_SHARE`) and together roughly discharge it
  (`SPLIT_COVERAGE_CAP`); anything else falls back to the single best
  candidate.

Measured on the 965-record seed corpus: all four categories present, 12 rows
receive multiple nominations, and no other archetype leaks into
`SPLIT_SETTLEMENT`. An earlier confidence-only filter produced 82 predictions
at roughly 15% precision, which is what motivated the arithmetic one.

Guarded by four tests in `matching_engine/tests/test_layers.py` that span the
matcher-to-classifier seam, including the inverse cases — a lone partial
payment and equal-value lookalikes must **not** nominate multiple candidates.

---

## Running the gates

```bash
pytest                                    # everything (integration auto-skips)
pytest -m integration                     # needs `docker compose up -d postgres`
pytest services/matching_engine/tests/test_latency.py -v    # load + scaling

cd frontend && npm ci && npm run lint && npm run build      # the frontend gate
```

The frontend lint config is correctness-only — no stylistic rules, so it never
reformats. The rule earning its keep is `react-hooks/exhaustive-deps`:
`useWebSocket.js` holds its callback in a ref to avoid a stale-closure
reconnect loop, an invariant invisible to review but visible to the linter.

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs each gate as a named
step, so a red build names the objective it broke rather than reporting one
opaque failure.

Run `pytest` to see the measured margins printed, not just pass/fail — including
the threshold sweep that evidences Sec. 9's false-positive claim.

### Two `Transaction`s

`shared.models.Transaction` is the **Pydantic wire model**;
`shared.models.orm.Transaction` is the **database row**. `orm` is deliberately
not re-exported, so `from shared.models import orm` makes the choice explicit
at every call site.

### Schema is the source of truth

`db/schema.sql` owns the DDL. The ORM maps onto it and never generates it —
enum columns use `create_type=False`. `shared/tests/test_orm_matches_schema.py`
parses the SQL and diffs it against the ORM metadata, so drift fails a test
instead of surfacing at runtime.

`schema.sql` is ASCII-only and re-runnable on purpose: it is piped through
`psql`, Docker's entrypoint and `alembic upgrade --sql`, and more than one of
those paths may apply it to the same database.

---

## Seeding data

A freshly started stack is empty — every corpus in the repo belongs to a gate
and lives under a `tests/` directory. [tools/seed.py](tools/seed.py) generates
a two-sided corpus and keeps the answer key:

```bash
make seed n=2000                 # -> data/seed/{erp_ledger.csv,bank_feed.json,answer_key.json}
make seed-kafka n=2000           # publish to KAFKA_TOPIC_RAW (needs `make infra`)
```

It emits **raw vendor-shaped** payloads — ERP as CSV, the bank side as JSON
with different column names — so seeded data enters through the real front
door and exercises Pandas normalisation, Pydantic, the GE suite and checksum
verification on the way in. Roughly 6% of records are deliberately malformed,
so Subsystem 2 visibly quarantines something instead of reading zero.

Archetypes are mixed to production proportions (62% clean matches, the rest a
tail of exceptions) rather than spread evenly. A flat spread pushes two thirds
of the corpus into the queue and shows a ~25% match rate, which measures the
corpus rather than the engine.

The tool writes to files or Kafka, **never to Postgres** — nothing may reach
the database without passing validation first, and a seeder that INSERTed
directly would fabricate the guarantee the system exists to provide.

### Real obligations

The *pairing* must always be derived — no institution publishes both its
ledger and its bank feed, so no public dataset contains two views of the same
payments, and without an answer key precision cannot be computed at all. The
**obligations need not be**:

```bash
curl -L -o online_retail_II.zip \
  https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip
pip install -r tools/requirements.txt
python tools/seed.py --count 2000 --from-dataset online_retail_II.zip --out data/seed
```

[tools/real_ledger.py](tools/real_ledger.py) draws the internal side from UCI
**Online Retail II** — 1.07M line items from a UK retailer, Dec 2009–Dec 2011,
no account needed. It fits because it has the one thing fraud corpora lack:
**real invoice numbers**. Aggregating line items per invoice yields genuine
per-invoice obligations, which is what an ERP ledger holds.

Amounts, dates, invoice references, counterparties and product descriptions
are then all real; only the counterpart leg is constructed. Two transformations
are applied and both are deliberate:

- **Dates are shifted forward.** The KPI endpoint defaults to a 30-day window,
  so the original 2009–2011 timeline would show zeroes on every panel. Every
  date moves by the same offset, so intervals, day-of-week structure and
  seasonality survive exactly.
- **References are prefixed.** The pipeline enforces `^REF-[0-9]{4,10}$`;
  invoice `489434` becomes `REF-489434`. The identifier is unchanged and still
  real — the prefix maps it into the format this system's feed spec requires,
  which is what an ETL adapter is for. Relaxing the regex instead would weaken
  a documented rule and perturb the detection-rate gate.

75.5% of raw line items are retained. Cancellations (invoices prefixed `C`,
negative amounts) are excluded because the system is *designed* to reject them
and they would otherwise inflate the quarantine rate with items that were never
transactions; rows without a customer are dropped because the counterparty is
what the clustering reads.

### Grading a corpus

[tools/verify_corpus.py](tools/verify_corpus.py) runs a corpus back through
the real validation, matching and triage code and grades it against its own
answer key. It exits non-zero if any exception category proves unreachable —
which is how the `SPLIT_SETTLEMENT` gap above was found.

```bash
python tools/verify_corpus.py data/seed
```

Measured on ~965 records, both corpora, no database:

| | Synthetic | Real (Online Retail II) |
|---|---|---|
| Detection rate | 100.00% | 100.00% |
| False positive rate | 0.00% | 0.00% |
| Match rate | 61.88% | **62.90%** |
| Pair precision | 100.00% | 100.00% |
| Pair recall | 65.88% | 67.14% |
| Categories reachable | 4 of 4 | 4 of 4 |

Real obligations perform marginally better, mostly because genuine product
descriptions are more distinctive than generated ones, so the ML layer clears
a few more pairs. `TIMING_DIFFERENCE` is rarer on the real corpus (1 vs 11) for
the same reason — distinctive text lets the matcher settle timing cases that
would otherwise reach the queue.

## Running the whole system

```bash
docker compose up -d --build
docker compose ps            # every service should read (healthy)
```

Ports follow build.md §15: validation 8001, matching 8002, exceptions 8003,
reporting 8000, frontend 3000. The dashboard is on
[localhost:3000](http://localhost:3000); the reporting gateway serves OpenAPI
docs at [localhost:8000/docs](http://localhost:8000/docs).

`REQUIRE_AUTH=true` is the compose default. Set it to `false` for local
development only — it makes every caller a `SYSTEM_ADMINISTRATOR` and logs a
warning at startup.

### Signing in

Every data endpoint is permission-guarded, so the dashboard needs a token
before it can render anything. It opens on a sign-in card: pick a role, and the
key prefills from `VITE_SERVICE_API_KEY` when set (see
[frontend/.env.example](frontend/.env.example)). That value must match
`SERVICE_API_KEY` on the gateway.

`POST /auth/token` is **not an identity provider** — it verifies one shared
secret and mints a token for whichever role is requested. There is no user
store in this system. A real deployment puts an IdP in front of the gateway and
drops the endpoint.

Switching role **re-issues the token**, because the gateway enforces the role
claim inside the signature and explicitly does not trust the `X-FinanceHub-Role`
header a client sends. Without re-issuing, changing role would restyle the UI
while the server kept applying the old permissions. This is what makes RBAC
demonstrable rather than decorative: sign in as Auditor and the resolve action
is refused by the API, not merely hidden.

On a stack with `REQUIRE_AUTH=false` the card is skipped — the app probes
`/auth/me` first, so it does not put a login wall in front of a gateway that
has no lock.

A demo therefore runs: `docker compose up -d --build` → `make seed-kafka
n=2000` → open [localhost:3000](http://localhost:3000) → sign in.
