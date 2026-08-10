# HANDOFF — FinanceHub

**For whoever picks this up next, human or agent.** Read this before changing
anything. It records what exists, what is genuinely verified, what only *looks*
verified, and what to build next in priority order.

Last updated at commit `6268934`. Branch `main`, pushed to
`github.com/Dotman-Bei/Finance-hub`.

---

## 1. What this is

FinanceHub — an Automated Financial Transaction Reconciliation and Reporting
System. It is the software artefact for a thesis whose Chapters 1–3 specify it;
[build.md](build%20%281%29.md) is the engineering translation of those chapters
and is the closest thing to a spec. Four subsystems over REST, one PostgreSQL
database as the single source of truth.

| # | Subsystem | Objective | Does |
|---|---|---|---|
| 1 | Hybrid Matching Engine | Obj. 1 | Rule layer then unsupervised ML layer, confidence-scored |
| 2 | Real-Time Validation Pipeline | Obj. 2 | Quarantine ≥98% of malformed records *before* the DB |
| 3 | Smart Exception Handling | Obj. 3 | Classify unmatched into 4 categories, suggest fixes, learn from humans |
| 4 | Reporting Dashboard | Obj. 4 | KPIs, charts, exception triage, audit PDFs |

Flow: `sources → validation → matching → (matched → ledger) / (unmatched →
exception queue) → dashboard`.

---

## 2. State: all seven phases are built

build.md's phases 0–6 are complete. There is no Phase 7. Everything since has
been closing gaps found by actually exercising the thing.

```
6268934  Draw obligations from a real corpus; add corpus verification
1202bef  Add deploy/: bare-metal VPS deployment, no Docker
956b5de  Add the sign-in flow: the dashboard could not authenticate at all
6d2a6a3  Make SPLIT_SETTLEMENT reachable: nominate co-settling candidates
3383ac0  Add tools/seed.py: two-sided corpus generator with a retained answer key
407a0fe  Phase 6 follow-up: complete the frontend lint gate, refresh stale docs
9d3df3d  Phase 6: hardening — audit gate, end-to-end tests, load test, CI
3271be7  FinanceHub: automated reconciliation system, phases 0-5
```

**290 tests, suite green.** One integration test auto-skips locally (no
Postgres) and CI fails the build if it skips.

---

## 3. Verified vs. assumed — read this before claiming anything works

This distinction matters more than any other section here.

### Genuinely verified

- 290 tests pass on Python 3.12.3 (Windows)
- Frontend: `npm ci`, `npm run lint`, `npm run build` all clean
- Both corpora graded end-to-end through the **real** validation, matching and
  classifier code via `tools/verify_corpus.py`
- `provision.sh` passes `bash -n`; every `ExecStart` path and module it names exists
- Docker installer downloaded and signature-verified (Docker Inc, valid)

### NOT verified — do not assume these work

| Thing | Status |
|---|---|
| `docker compose up` | **Never run.** Docker is not installed on the dev machine |
| `deploy/provision.sh` | **Never run on Linux.** Syntax-checked only |
| The dashboard rendering live data | **Never seen.** No browser has ever loaded it against a running gateway |
| WebSocket `/ws/exceptions` | Unit-tested only; never exercised service-to-service |
| Service-to-service HTTP hops | Never run; everything so far is in-process function calls |
| Kafka ingestion | Never run against a live broker |
| The audit trigger | CI-only (needs real PostgreSQL) |

**The single largest risk to this project is that the assembled system has
never actually run as services.** Everything measured so far comes from calling
the pipelines directly in Python. Expect real bugs on first boot.

---

## 4. Invariants — breaking these breaks the thesis, not just the code

1. **No mock data anywhere.** Services read real data or fail visibly. CI greps
   `frontend/src/` for `demoData|startDemoStream|placeholderPdf|synthetic corpus`
   and fails the build. Synthetic corpora live only under `tests/` and `tools/`.
2. **Nothing reaches the database without passing validation.** `tools/seed.py`
   writes to files, Kafka or `POST /validate` — **never** `INSERT`. A seeder that
   wrote directly would fabricate the exact guarantee Subsystem 2 exists to provide.
3. **`db/schema.sql` owns the DDL.** The ORM maps onto it and never generates it
   (`create_type=False`). `shared/tests/test_orm_matches_schema.py` parses the SQL
   and diffs it against ORM metadata, so drift fails a test.
4. **`schema.sql` stays ASCII-only and re-runnable.** It is piped through `psql`,
   Docker's entrypoint and `alembic upgrade --sql`; more than one path may apply it.
5. **Two `Transaction`s, deliberately.** `shared.models.Transaction` is the
   Pydantic wire model; `shared.models.orm.Transaction` is the DB row. `orm` is
   *not* re-exported — `from shared.models import orm` forces the choice.
6. **The answer key is keyed on `external_id`, never UUID.** Postgres assigns
   primary keys at insert; a generator cannot know them and must not pretend to.
7. **`FEATURE_NAMES` order is the model's input contract.** Appending is safe;
   reordering or inserting silently invalidates every persisted `.pkl`. Paired
   with `FEATURE_VERSION` and checked at load.
8. **Rejections are never training labels.** A rejection says the suggestion was
   wrong, not what was right.
9. **Every suggestion records its engine** (`bootstrap` | `random_forest`) so no
   consumer mistakes a cold-start rule for a learned prediction.
10. **In `deploy/`, only nginx is publicly bound.** Without Docker there is no
    network namespace; a service on `0.0.0.0` is on the internet.

---

## 5. Repo map

```
FINALS/
├── build (1).md            # the spec (UNTRACKED — see §9)
├── HANDOFF.md              # this file
├── README.md               # status, gates, quickstart
├── docker-compose.yml      # 10 services, all active
├── db/schema.sql           # 6 tables, 5 enums, audit trigger
├── shared/                 # config, db, Pydantic + SQLAlchemy models
├── services/
│   ├── validation_pipeline/    # Subsystem 2, port 8001
│   ├── matching_engine/        # Subsystem 1, port 8002
│   ├── exception_handler/      # Subsystem 3, port 8003 (+ celery)
│   └── reporting_api/          # Subsystem 4 backend, port 8000
├── frontend/               # React SPA, port 3000
├── tools/
│   ├── seed.py             # two-sided corpus + answer key
│   ├── real_ledger.py      # real obligations from UCI Online Retail II
│   ├── verify_corpus.py    # grade a corpus through the real code
│   └── requirements.txt    # openpyxl — tools only, not services
├── deploy/                 # bare-metal VPS: provision.sh, systemd, nginx
└── tests/                  # cross-subsystem e2e + integration
```

---

## 6. Tooling you will need

```bash
make help                      # all targets

# corpora
make seed n=2000               # synthetic, to files
make seed-real d=online_retail_II.zip n=2000   # real obligations
make seed-http n=2000          # POST to a running validation pipeline
make verify                    # grade data/seed against its answer key
```

**`tools/seed.py`** emits *raw vendor-shaped* payloads — ERP as CSV, bank as
JSON, with **different column names on each side**. That is the point: seeded
data enters through the real front door and exercises Pandas normalisation,
Pydantic, Great Expectations and checksum verification. It is not the test
corpora, which feed the matcher directly and bypass ingestion.

**`tools/real_ledger.py`** draws obligations from UCI Online Retail II (1.07M
line items, UK retailer, 2009–2011, no account needed):

```bash
curl -L -o online_retail_II.zip \
  https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip
pip install -r tools/requirements.txt
```

**`tools/verify_corpus.py`** runs a corpus back through the real code and exits
non-zero if any exception category proves unreachable.

---

## 7. Measured results, with the caveats that matter

From `tools/verify_corpus.py` on ~965 records, no database:

| | Synthetic | Real (Online Retail II) |
|---|---|---|
| Detection rate | 100.00% | 100.00% |
| False positive rate | 0.00% | 0.00% |
| Match rate | 61.88% | 62.90% |
| Pair precision | 100.00% | 100.00% |
| Pair recall | 65.88% | 67.14% |
| Categories reachable | 4/4 | 4/4 |

### Accuracy — computed ad hoc, NOT yet in any tool

| Objective | Measure | Real | Synthetic |
|---|---|---|---|
| 2 — validation | accuracy (963/963) | 100.00% | 100.00% |
| 1 — matching | F1 | 80.34% | 79.43% |
| 3 — classification | accuracy vs. key | 100.00% ⚠️ | **81.25%** |

**The real 100% is flattered by an easy sample** — it grades 60 obligations over
only *three* categories, because all 36 `TIMING_DIFFERENCE` pairs got matched by
the ML layer and never reached the queue. The synthetic 81.25% grades all four
and is the honest number:

| Category | In key | Reached queue | Correct |
|---|---|---|---|
| `MISSING_REFERENCE_CODE` | 24 | 24 | 96% |
| `PARTIAL_PAYMENT` | 24 | 24 | 92% |
| `SPLIT_SETTLEMENT` | 12 | 12 | **58%** |
| `TIMING_DIFFERENCE` | 36 | 4 | **0%** |

Two further qualifiers: these are the **cold-start bootstrap rules**, not the
trained Random Forest (`verify_corpus.py` loads a nonexistent model path on
purpose); the gate in `test_classifier.py` puts the forest at macro F1 1.00 vs
bootstrap 0.89. And n=60/64 is thin for a headline claim.

---

## 8. What to build next — priority order

### P0 — Run the system for real

Nothing else is worth much until this happens, because every "it works" claim
above is about in-process calls.

Either path:

- **Docker:** installer is at `~/Downloads/DockerDesktopInstaller.exe` (596 MB,
  signature verified). Needs admin PowerShell: `wsl --update`, then the
  installer, then **reboot**. WSL2 kernel is currently missing.
- **VPS (user's stated preference):** `deploy/` is ready. Ubuntu 24.04,
  2 vCPU / 4 GB. See [deploy/DEPLOY.md](deploy/DEPLOY.md). No Kafka needed —
  `CONSUME_KAFKA=false` runs REST-only.

Then, in order: `up` → seed → confirm rows in Postgres → load the dashboard →
sign in → switch to Auditor and confirm the API *refuses* the resolve. Expect
bugs at every hop.

### P1 — Fix `TIMING_DIFFERENCE` classification (0%)

Of 36 timing pairs, 32 were matched (fine) and the 4 that reached the queue were
**all misclassified** — 3 as `MISSING_REFERENCE_CODE`, 1 as `PARTIAL_PAYMENT`.
The pattern suggests the nominated counterpart was not the true leg, so features
were computed against the wrong row. Start in
`services/exception_handler/app/features.py` (`extract`, the `nearest` selection)
and `classifier.py`'s bootstrap ordering.

### P2 — Fold accuracy grading into `verify_corpus.py`

The §7 accuracy table was computed by a throwaway script. It should be a flag on
the tool, graded on internal obligations (a split *leg* viewed alone is
legitimately ambiguous with a partial payment — grade the obligation, not the leg).

### P3 — `SPLIT_SETTLEMENT` at 58%

Reachable now, but a third still land as `PARTIAL_PAYMENT`. The co-settling
arithmetic in the bootstrap rules needs tightening.

### P4 — Larger samples

Rerun at `--count 5000` for numbers tight enough to defend.

### P5 — Chapter 4 harness

A script that runs every gate plus `verify_corpus` and emits one results table.
The numbers currently only exist in scrolling pytest output.

### P6 — Deployment hardening

TLS via certbot (`provision.sh` deliberately does not, since it cannot know the
domain). Note `VITE_SERVICE_API_KEY` compiles into the public bundle — fine for a
demo, documented as such, not a way to ship a credential.

---

## 9. Environment realities

- **Dev machine:** Windows 11 Home, build 21996, 15.8 GB RAM. Virtualization
  enabled in firmware. **Docker not installed. WSL2 kernel missing.**
  `winget` is present but broken (`file cannot be accessed by the system`).
- **Python:** 3.12.3 in `.venv` — the suite is verified on it, which is also what
  Ubuntu 24.04 ships. Containers pin 3.11.
- **`build (1).md` is untracked.** It is the spec and arguably should be
  committed, but it has been left alone deliberately — do not commit it without
  asking.
- **User preference:** VPS over Docker, stated explicitly. Don't re-litigate it.
- **On datasets:** the user pushed hard on using real data. The settled position
  is in §6 — real obligations, derived counterpart side. Two *unrelated* public
  datasets cannot work: no true pair exists between them, so there is no answer
  key and precision is not computable. Don't reopen unless asked.

---

## 10. Gotchas that already cost real time

- **`reference_code` must match `^REF-[0-9]{4,10}$`.** Real invoice numbers are
  bare digits and get quarantined 100% of the time. `real_ledger.py` prefixes
  them. Do not relax the regex — the detection-rate gate depends on it.
- **Dates need headroom.** An archetype applying a 5–20 day settlement lag to a
  transaction dated yesterday lands in the future, where stage 2 correctly
  quarantines it — silently destroying records the answer key calls good. See
  `MAX_FORWARD_SHIFT` and `real_ledger.to_ledger(headroom_days=21)`.
- **`canonical_json` strips `checksum` but keeps `checksum_algorithm`.**
  Attaching the algorithm *after* hashing fails every signed record at stage 3,
  and looks exactly like the checksum stage catching a corrupt feed.
- **Never draw IDs with `randint`.** 500+ draws from a 900k range collide with
  ~14% probability; a duplicate `external_id` makes the answer key ambiguous.
  `build_corpus` now asserts uniqueness.
- **The Windows console is cp1252.** Box-drawing characters and em-dashes in tool
  output raise `UnicodeEncodeError`. Keep printed strings ASCII.
- **systemd `EnvironmentFile` is strict** — an unquoted `#` in a value truncates it.
- **`WorkingDirectory` must be the repo root.** Services import `shared.*` and
  `services.*`, which resolve from nowhere else.
- **`ProtectSystem=strict`** means a unit needs an explicit `ReadWritePaths` for
  any model file it writes, or the write silently fails and the service falls
  back to a per-batch vocabulary.
- **The 62% clean archetype mix is deliberate.** A flat 1-in-6 spread puts two
  thirds of the corpus in the exception queue and shows a ~25% match rate, which
  measures the corpus rather than the engine.

---

## 11. Before you claim you haven't broken anything

```bash
.venv/Scripts/python -m pytest            # 290 tests, must be green
cd frontend && npm ci && npm run lint && npm run build
python tools/seed.py --count 400 --out data/seed && python tools/verify_corpus.py data/seed
```

`verify_corpus.py` exits non-zero if an exception category becomes unreachable —
that is the check that catches whole-system regressions no unit test sees.
