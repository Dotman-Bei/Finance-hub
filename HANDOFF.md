# HANDOFF — FinanceHub

**For whoever picks this up next, human or agent.** Read this before changing
anything. It records what exists, what is genuinely verified, what only *looks*
verified, and what to build next in priority order.

Last updated 2026-08-10. Branch `main`, pushed to
`github.com/Dotman-Bei/Finance-hub`.

**What changed this session:** the system was deployed and run for real on a
VPS for the first time, driven in a real browser, put behind TLS, had its
retrain loop exercised end to end, and finally had every endpoint of the
running deployment checked — §3 is substantially rewritten as a result. It is
live at **https://financehub-demo.duckdns.org**. Nine bugs found and fixed,
every one of them invisible to the test suite because they only exist once the
thing runs as services against a real database. Read §7's warning about the
model now on the box before quoting any forest number.

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
6b582f8 Add tools/e2e_check.py: exercise every endpoint of a running deployment
2f72a9d Fix AUDIT_TRAIL against the real audit helper shapes
c5999f7 Make the report type select content, not just the heading
d3751ee HANDOFF: the queue now triages itself
8b9bb26 Schedule triage: the open queue was only ever swept by hand
6c114bc HANDOFF: the retrain loop is proven, the labels are not
d2f64d3 Measure the first retrained model against the bootstrap rules
cd6cd16 HANDOFF: TLS is live; record the two ways it can be undone
7ffdee4 Do not let provision.sh delete TLS on the next deploy
8ac5de8 Add tools/dashboard_check.mjs; record what the browser verified
a3dd1d7 Fix a 500 on any re-submitted record: duplicates crashed the pipeline
25906e1 Fix two bugs a browser found: the dashboard rendered nothing
24e3547 Update HANDOFF and README: the system has now been run for real
78686a9 Add tools/chapter4.py: one command, one results table
002783e Block fractional candidates by counterparty: fixes the P4 scale collapse
d921d64 Add fraction-blocking Channel 3 for split/partial-payment legs
46eba80 Raise max_candidates_per_row: SPLIT_SETTLEMENT was starved of its own legs
439f3be Fold accuracy grading into verify_corpus.py as --accuracy
3ff48c6 Fix TIMING_DIFFERENCE: 0% classification accuracy on genuine timing pairs
260b35d Fix two bugs found running the system live for the first time
```

**307 tests, one failing.** The count rose from 290 because
`tests/test_integration_db.py` no longer skips — with a reachable PostgreSQL
its 18 tests run individually instead of the file skipping as one. The
failure is `test_precision_meets_target`; see §7.

---

## 3. Verified vs. assumed — read this before claiming anything works

This distinction matters more than any other section here. **It changed
substantially on 2026-08-10: the system has now been run for real.**

### Genuinely verified

- **The whole system runs as services on a real VPS** (Ubuntu 24.04.4,
  `169.58.153.9`). `deploy/provision.sh` ran end to end; all six systemd
  units, nginx, PostgreSQL and Redis are active.
- **Data crosses every hop for real**: ~4,750 records seeded through
  `POST /validate` over HTTP, 4,347 rows in PostgreSQL, 60 quarantined,
  reconciliation run via `POST /reconcile`, exceptions opened, one resolved
  through the API.
- **RBAC is enforced**: an `AUDITOR` token is refused with
  `403 Role AUDITOR is not permitted to resolve_exceptions`; a
  `FINANCE_MANAGER` token succeeds. Wrong API key → 401.
- **The audit trigger fires on real UPDATEs** — verified by reading the
  `audittrail` row written by that resolve.
- **All 18 integration tests pass against real PostgreSQL 16.14**, not CI-only.
- nginx serves the built SPA and keeps `/health`, `/stats`, `/audit`
  unreachable from outside (they fall through to the SPA, verified by
  content-type).
- Both corpora graded end-to-end through the **real** validation, matching and
  classifier code via `tools/verify_corpus.py`.
- **The dashboard renders live data in a real browser** — 16/16 checks in
  `tools/dashboard_check.mjs`, zero uncaught page exceptions: sign-in, KPI
  tiles carrying real figures, the match-rate trend chart, the categorised
  exception queue, a live `exception.created` burst arriving over the
  WebSocket during a reconcile, and the Auditor role being offered no Resolve
  control. This found three real bugs on first contact (`25906e1`, `a3dd1d7`).
- **Every endpoint of the running system is exercised** by
  `tools/e2e_check.py` (`make e2e`): 48 assertions over all four services -
  every documented route, all three roles against the permission matrix, all
  four report types downloaded and parsed, and the label rules. Passes both on
  loopback (47/47) and through nginx + TLS (48/48). It found three things
  nothing else had: the matching engine running without persisted models, four
  report types that were one report with four titles, and an AUDIT_TRAIL
  report containing no audit rows.
- **The queue triages itself.** `financehub.triage.triage_open` runs on a
  two-minute beat interval; verified live by seeding, reconciling, then
  *waiting* — 495 untriaged rows went to 1 without intervention, classified
  via `random_forest` across all four categories, and the next sweep
  correctly did nothing in 0.1s. (The 1 is an exception resolved before it
  was ever triaged; `load_untriaged` only picks OPEN rows.)
- **The Sec. 11 feedback loop runs end to end in production.** 250 exceptions
  resolved through the real API (RBAC, audit trail, feature capture) → the
  200-label trigger fired → celery trained a forest → the promotion guard
  scored it → it persisted to `rf_classifier.pkl` → **the API hot-swapped to
  `engine=random_forest` with no restart** (service up since 12:44, model
  written 14:43), and survives a restart.
- **TLS is live** at **https://financehub-demo.duckdns.org** — Let's Encrypt,
  auto-renewing (`certbot.timer`, dry-run passes), HTTP 301s to HTTPS. The
  same 16/16 browser run passes over HTTPS with the WebSocket negotiated as
  `wss://`.

### Still NOT verified

| Thing | Status |
|---|---|
| `docker compose up` | **Never run.** Docker is not installed anywhere in play |
| Kafka ingestion | Never run against a live broker (`CONSUME_KAFKA=false` by design here) |
| A *successful* quarantine replay | The endpoint works and refuses to fake success; no record here can pass on a re-run. See §8 P0 |

The old warning that "the assembled system has never actually run as
services" is now **obsolete**. It found two real bugs on first boot, both
fixed in `260b35d`; see §7.

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
│   ├── verify_corpus.py    # grade a corpus through the real code (--accuracy)
│   ├── chapter4.py         # every gate + corpus grading -> one results table
│   ├── dashboard_check.mjs # drives the deployed dashboard in a real browser
│   ├── e2e_check.py        # every endpoint of a running deployment
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

# results
make chapter4                  # gates + corpus grading + the objectives table
make chapter4 n=5000           # at the sample size worth defending

# the running deployment
make e2e                       # every endpoint, all three roles, all four reports
python tools/e2e_check.py --base https://your.host   # through nginx and TLS too
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
non-zero if any exception category proves unreachable. `--accuracy` adds the
per-archetype classification grading described in §7.

**`tools/chapter4.py`** (`make chapter4`) runs the whole test suite plus the
corpus grading and prints one table mapped to the four objectives. It computes
nothing itself — gate results are read from pytest's JUnit XML and corpus
figures come from `verify_corpus.verify()` — so it cannot report a number the
suite would disagree with. A gate that stops being collected shows as MISSING
rather than disappearing; a skipped gate is reported as skipped, never as
passed.

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

### Accuracy — now a flag, not a throwaway script

`python tools/verify_corpus.py <dir> --accuracy`, or `make chapter4` for the
whole table including gates. Graded **per obligation**, on the internal (ERP)
leg only: a split settlement's external legs each look exactly like a partial
payment viewed alone, so grading them would score an ambiguity that is real
rather than a classifier error.

Classification accuracy, **bootstrap rules**, synthetic corpus, by size:

| Archetype | 800 pairs | 1500 pairs | 5000 pairs |
|---|---|---|---|
| `MISSING_REFERENCE_CODE` | 90% | 79% | 66% |
| `PARTIAL_PAYMENT` | 96% | 92% | 79% |
| `SPLIT_SETTLEMENT` | 79% | 82% | 71% |
| `TIMING_DIFFERENCE` | n/a | n/a | n/a |
| **overall** | **90.00%** | **84.75%** | **72.45%** |

`TIMING_DIFFERENCE` reads `n/a` because ~all of its pairs now **auto-match**
and never reach the queue — that is the P1 fix working, not a gap. Force them
into the queue with a high `--threshold` and they classify 144/144 correctly.

Read the size columns as a **property of the corpus, not only of the engine**.
`tools/seed.py` draws descriptions from 15 counterparties × 5 narratives = 75
combinations, so rows sharing byte-identical text grow linearly with corpus
size (~11 per description at 800, ~140 at 5000). That crowding is what the
counterparty blocking in `002783e` attacks, and it is why the 5000-pair column
improved from 48.72% to 72.45% while the 800-pair column did not move at all.
A real feed with more distinct narratives would sit nearer the left column.

Still the **cold-start bootstrap rules**, not the trained Random Forest
(`verify_corpus.py` loads a nonexistent model path on purpose); the gate in
`test_classifier.py` puts the forest at macro F1 1.00 vs bootstrap 0.89.

### The model currently on the box was trained on SYNTHETIC labels

Read this before quoting any forest number.

`/health` reports `human_labelled = 250`. **No human labelled anything.** The
250 decisions were generated by replaying the corpus answer key through
`POST /exceptions/{id}/resolve` — ACCEPT where the bootstrap category was
right, EDIT with the correct category where it was not.

The system cannot tell the difference, and that is not a bug: `/resolve` *is*
the human path, so anything arriving through it is a human decision as far as
the model is concerned. The only durable record of provenance is the audit
trail, which is why the token subject was set deliberately:

```sql
SELECT actor, count(*) FROM audittrail WHERE entity_type='exceptionqueue' GROUP BY actor;
--  answer-key-replay | 250      <- these
--  dashboard         |   1
```

What this **does** establish: the retrain → promote → persist → hot-swap
mechanism works on real infrastructure. What it does **not** establish: any
accuracy claim about the forest on genuine human feedback.

To reset to a cold start: delete
`services/exception_handler/models/rf_classifier.pkl` and restart
`financehub-exceptions`; the service falls back to the bootstrap rules and
says so on `/health`.

### The forest's numbers here are not comparable to the gate

`macro_f1 = 0.481` on the box vs `1.00` in `test_classifier.py`. Different
data, not a regression. The 250 labels are the subset where answer-key ground
truth existed, and the bootstrap rules were wrong on 140 of them — a hard,
skewed sample. Scored on those same held-out rows the rules manage only
0.377, so **the forest beats them by +0.104 macro F1**; the gate's corpus is
purpose-built at 50% ambiguous and measures something else entirely. Always
compare two engines on the same rows, never against a remembered number.

### The one failing gate

`test_precision_meets_target`: 98.95%, gate 99%, **94 of 95 confirmed pairs
correct**. Facts established rather than assumed:

- **Pre-existing.** Reproduces identically at `e6f02a0`, before any of this
  week's work — verified in a clean `git worktree`, not inferred.
- **Deterministic, not flaky.** The corpus is seeded; three consecutive runs
  give byte-identical output. (Earlier notes in this repo called it flaky.
  That was wrong.)
- **Platform-dependent.** It reportedly passed on the author's Windows
  machine; it fails on Ubuntu 24.04 / Python 3.12.3, most likely a
  scikit-learn tie-break difference in TF-IDF or DBSCAN.
- **The gate demands perfection at this n.** With 95 confirmed pairs the only
  achievable values are 100% (95/95) and 98.95% (94/95) — there is nothing in
  between, so a "99%" gate is a 100% gate here.
- **The offending pair is genuinely ambiguous**: two *different* obligations
  from the same counterparty, identical descriptions (cosine 1.0), amounts
  0.87% apart, 5 days apart, and the bank side carries no reference code.
  Confidence 0.86 against a 0.85 threshold.

A rule of "auto-confirm requires exact amount agreement **or** reference
agreement" would reject it and take precision to 100%, but it would also stop
auto-confirming the ~11 genuine partial payments currently matched, costing
~6 points of recall. That is a **design decision about the engine, not a bug
fix**, so it was left alone. Decide it deliberately before touching it.

---

## 8. What to build next — priority order

**P0–P5 from the previous handoff are done** (2026-08-10). What follows is
what is left, renumbered.

### P0 — Two features work but have never been proven to *do* anything

Both pass their endpoint checks, and neither has been shown to produce its
intended outcome, because the data to do so does not exist here:

* **Quarantine replay.** `POST /quarantine/replay` correctly re-runs stored
  payloads and correctly refuses to mark still-failing records as replayed -
  verified on 25 rows across all three stages, all of which stayed
  quarantined. But every quarantined record here is *permanently* malformed
  (seed.py generates them that way), and replay re-sends the stored bytes
  verbatim, so the success path cannot fire. It needs a record that failed for
  a reason that has since changed - a corrected checksum secret, a relaxed
  business rule - to show `replayed > 0` and `replayed_at` being set.
* **`SYSTEM_ADMINISTRATOR` beyond permissions.** The role authenticates and is
  correctly allowed and denied the right things, but nothing exercises what it
  exists *for* over and above FINANCE_MANAGER.

### P1 — Decide the precision gate deliberately

See §7. Pre-existing, deterministic, one genuinely ambiguous pair out of 95,
and the "fix" is an engine design decision with a real recall cost. It should
be *decided*, not left failing indefinitely — a permanently red gate trains
everyone to ignore the suite.

### P2 — Classification accuracy at scale

72.45% at 5000 pairs against 90.00% at 800. Much of the gap is corpus
vocabulary (see §7), so the two honest options are different in kind:

- **Widen `tools/seed.py`'s pools** (15 counterparties × 5 narratives → far
  more). This makes the corpus resemble a real feed rather than flattering the
  engine, and is the cheaper change. It does not improve the engine.
- **Keep pushing the engine.** The remaining `MISSING_REFERENCE_CODE` losses
  are cases where the true leg is unfindable (mangled text, equal amount) and
  2+ same-counterparty fractions get nominated instead, which the bootstrap
  rule reads as multiplicity → split. Measured and ruled out already: coverage
  does **not** separate the two (all four archetypes sit at median 0.83–1.00),
  and a date-spread cut only separates by reading `seed.py`'s own
  `randint(0, 4)` back off the generator, which is fitting the corpus rather
  than the phenomenon. Do not redo those two experiments.

### P3 — Real human feedback

The loop is proven; the labels are not. Everything the forest knows came from
an answer-key replay (§7). Genuine reviewer decisions through the dashboard
would be the first labels that make an accuracy claim about the forest mean
anything, and they are the only thing that lets it exceed the rules rather
than reproduce them.

### Deployment hardening — the standing note

TLS is configured on this box (see §3). Two things about it that will bite:

* **`provision.sh` will not overwrite a certbot-managed nginx site**, by
  design — it detects the "managed by Certbot" markers and skips the copy, so
  a deploy cannot silently revert the site to plain HTTP. The cost is that
  changes to `deploy/nginx/financehub.conf` are **not** picked up on a TLS
  host; apply them by hand, then `certbot install --cert-name <domain> --nginx`.
* **certbot needs a concrete `server_name`.** The template ships `_` so a
  nameless box still serves, and `certbot --nginx` cannot find a server block
  to install into against a catch-all — it issues the certificate and then
  fails to deploy it. Pass `SERVER_NAME=your.domain` to provision.sh, or set
  it before running certbot.

**HSTS is deliberately not set.** Adding it is a one-line `add_header`, but it
is sticky in browsers for its `max-age` and painful to undo if the domain is
ever served over plain HTTP again — worth a deliberate decision, not a default.

Note `VITE_SERVICE_API_KEY` compiles into the public bundle — fine for a demo,
documented as such, not a way to ship a credential. A real deployment puts an
identity provider in front of the gateway and drops `/auth/token` entirely.

---

## 9. Environment realities

- **The VPS is now the working machine.** Ubuntu 24.04.4 LTS,
  `169.58.153.9`, 7.8 GB RAM, 96 GB disk, root. The repo is checked out
  **twice**: `/root/Finance-hub` (where edits and commits happen) and
  `/opt/financehub` (what systemd runs). They are separate clones — a change
  is not live until you `git pull` in `/opt/financehub` **and** restart the
  unit. There is no venv in `/root/Finance-hub`; use
  `/opt/financehub/.venv/bin/python` for everything.
- **Sourcing `.env` matters for tests.** `tests/test_integration_db.py` falls
  back to a `financehub:changeme@localhost` default and skips when it cannot
  connect. Run `set -a; source /opt/financehub/.env; set +a` first or its 18
  tests silently vanish from the count.
- **Push works over SSH**, via a deploy key generated on the box
  (`~/.ssh/id_ed25519`, fingerprint
  `SHA256:g2JeM0farZgSUxJDIuMFFE38avw/AY/4tdxU3J+Wc+Y`, registered on the
  repo with write access as `financehub-vps`). The remote was switched from
  HTTPS to `git@github.com:` — HTTPS has no credentials here.
- **Dev machine (historical):** Windows 11 Home, 15.8 GB RAM. **Docker not
  installed, WSL2 kernel missing**, `winget` broken. Relevant only because the
  suite was last green there and is not here — see §7's precision gate note.
- **Python:** 3.12.3, which is what Ubuntu 24.04 ships. Containers pin 3.11.
- **`build.md` is untracked** (it appears as `build (1).md` on the Windows
  machine). It is the spec and arguably should be committed, but it has been
  left alone deliberately — do not commit it without asking.
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
- **A cached verdict carries no parsed Transaction.** `to_cache_entry` stores
  the verdict alone on purpose, so a *re-submitted* record reaches
  `persist_batch` as PASSED with `transaction=None`. It used to trip an
  assertion and 500 the whole batch - and the batch commits as a unit, so one
  duplicate lost the other 499 records. Duplicates are now skipped and counted
  as `duplicates_skipped`. Only the deployed service hits this; every test
  constructs a fresh cache, so no fingerprint is ever seen twice in-process.
- **`exceptionqueue.category` is nullable and briefly null.** The matching
  engine opens rows untriaged and Subsystem 3 fills the category in. Beat
  sweeps every two minutes, so a row can legitimately be seen with
  `category = NULL` in the window between a reconcile and the next sweep -
  the dashboard renders those as "Untriaged" rather than crashing on them
  (it used to crash; see `25906e1`). A sweep of 494 rows took 51s against
  that 120s interval, so the headroom is real but not enormous; raise
  `TRIAGE_BATCH_LIMIT` or the interval together, not one alone.
- **Applying `schema.sql` as `postgres` silently breaks re-runnability.** The
  tables come out owned by `postgres`; `GRANT ALL` gives the app role DML but
  not ownership, and DDL needs ownership — so the app's own `DATABASE_URL`
  can never re-apply the schema. Presents as `InsufficientPrivilege: must be
  owner of table transactions` from the integration tests, nowhere else.
  Fixed in `260b35d`; `provision.sh` now applies it as the app role.
- **A misclassification is usually a nomination bug, not a classifier bug.**
  Every classification defect chased this session — `TIMING_DIFFERENCE` at 0%,
  `SPLIT_SETTLEMENT` at 16% — turned out to be the matching engine failing to
  nominate the true counterpart, with the bootstrap rules reasoning correctly
  over the wrong input. Before touching `classifier.py`, print
  `counterpart_count` and check whether the true leg is even in
  `candidate_ids`.
- **Two experiments already run and rejected — do not repeat them.**
  (a) Anchoring `_co_settling_candidates`' relative floor on the best
  *amount-plausible* candidate rather than the top pair: helps
  `SPLIT_SETTLEMENT`, costs `MISSING_REFERENCE_CODE` far more (90% → 54%).
  (b) Requiring a minimum coverage to claim multiplicity: coverage does not
  separate the archetypes at all — all four sit at median 0.83–1.00.
- **`--count` changes conclusions, not just confidence.** A fix verified at
  800 pairs collapsed at 5000 because decoy density per description grows with
  corpus size. Verify anything touching candidate generation at both sizes;
  `make chapter4 n=5000` exists for this.

---

## 11. Before you claim you haven't broken anything

One command now covers the gates and the corpus together:

```bash
set -a; source /opt/financehub/.env; set +a     # or the DB tests skip
make chapter4                                    # gates + corpus + results table
```

It exits non-zero if any gate fails or any exception category becomes
unreachable — the check that catches whole-system regressions no unit test
sees. Expect exactly one failure today (`test_precision_meets_target`, §7); a
**second** failure is yours.

The pieces separately, if you need them:

```bash
/opt/financehub/.venv/bin/python -m pytest       # 307 tests
cd frontend && npm ci && npm run lint && npm run build
/opt/financehub/.venv/bin/python tools/verify_corpus.py data/seed --accuracy
```

And after changing anything the services run, remember the second clone:

```bash
cd /opt/financehub && git pull --ff-only && systemctl restart 'financehub-*'
systemctl --no-pager --plain is-active financehub-{validation,matching,exceptions,reporting}
```
