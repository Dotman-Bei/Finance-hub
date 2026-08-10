# Deploying FinanceHub to a VPS — without Docker

Bare-metal deployment on Ubuntu 24.04 LTS. No containers, no Kafka, no JVM.

The compose stack in the repo root remains the reference deployment; this is
the alternative for a plain rented Linux box.

---

## Why there is no Kafka here

`CONSUME_KAFKA=false` runs the validation pipeline REST-only
([main.py:41](../services/validation_pipeline/app/main.py#L41)), accepting
batches on `POST /validate` instead of consuming a topic. `/validate` puts the
`raw` document through the same Pandas normalisation a Kafka message takes, so
records still cross all four stages — this is a different door into the same
front door, not a bypass.

Dropping the broker removes roughly a gigabyte of JVM. That is the difference
between a comfortable 4 GB box and one that starts killing processes.

To run the broker anyway: install one, set `KAFKA_BROKER` and flip
`CONSUME_KAFKA=true` in `.env`. Nothing else changes.

---

## Sizing

| | |
|---|---|
| **Recommended** | 2 vCPU · 4 GB RAM · 40 GB SSD |
| **Minimum** | 2 GB — tight, and six Python processes each carrying pandas, numpy and scikit-learn will run you out |
| **OS** | Ubuntu 24.04 LTS (ships Python 3.12, which the suite is verified on) |

Hetzner CX22 is about €4/month at that size. DigitalOcean and Vultr equivalents
run $20–24.

---

## Install

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/Dotman-Bei/Finance-hub.git /opt/financehub
sudo bash /opt/financehub/deploy/provision.sh
```

The script is idempotent — safe to re-run after a failure or a `git pull`.

It installs PostgreSQL, Redis, Python, Node 20 and nginx; creates the
`financehub` system account; generates `.env` with **random secrets**; creates
the database and applies `db/schema.sql`; builds one virtualenv and the SPA;
installs six systemd units and the nginx site; and enables `ufw`.

### Secrets

`.env` is generated once, with `openssl rand` values for `POSTGRES_PASSWORD`,
`JWT_SECRET` and `SERVICE_API_KEY`. **None of `.env.example`'s defaults reach a
deployed machine.** Re-running the script leaves an existing `.env` alone —
regenerating `JWT_SECRET` would invalidate every issued token, and a new DB
password would lock the services out of their own database.

To rotate: delete `.env` and re-run.

---

## What listens where

| Process | Bind | Public? |
|---|---|---|
| nginx | `0.0.0.0:80` | **yes — the only one** |
| reporting_api | `127.0.0.1:8000` | no |
| exception_handler | `127.0.0.1:8003` | no |
| matching_engine | `127.0.0.1:8002` | no |
| validation_pipeline | `127.0.0.1:8001` | no |
| PostgreSQL | `127.0.0.1:5432` | no |
| Redis | `127.0.0.1:6379` | no |

This matters more without Docker than with it. There is no network namespace
between these processes and the internet, so a service bound to `0.0.0.0` *is*
on the internet. An unauthenticated Redis on a public IP is compromised within
hours — usually for cryptomining, and your provider will suspend the box.

`ufw` allows only OpenSSH and nginx. The loopback binding is the first lock;
the firewall is the second.

nginx proxies only `/metrics`, `/exceptions`, `/reports`, `/auth` and `/ws/`.
`/health`, `/stats` and `/audit` are operational surfaces and stay unreachable
from outside.

---

## Seeding it

A freshly provisioned box has an empty database, so the dashboard renders
zeroes. On a host with no broker, use the HTTP sink:

```bash
sudo -u financehub /opt/financehub/.venv/bin/python \
  /opt/financehub/tools/seed.py --count 2000 --sink http \
  --validate-url http://127.0.0.1:8001 --out /tmp/seed
```

It POSTs the ERP side as CSV and the bank side as JSON — different column
vocabularies on each side, so normalisation is genuinely exercised — and prints
how many records the pipeline quarantined. The answer key lands in
`/tmp/seed/answer_key.json`.

It exits non-zero if any batch failed to reach the pipeline, so it will not
report success against a database it never populated.

Then run a reconciliation pass:

```bash
curl -s -X POST http://127.0.0.1:8002/reconcile -H 'Content-Type: application/json' -d '{}'
```

---

## Signing in

Every data endpoint is permission-guarded and `REQUIRE_AUTH=true` is the
deployed default. The dashboard opens on a sign-in card; the key is baked into
the bundle at build time from `SERVICE_API_KEY`, so it prefills.

```bash
grep SERVICE_API_KEY /opt/financehub/.env
```

Note that `VITE_` variables are compiled into the public JS bundle and are
readable with devtools. Acceptable for a demo secret; not a way to ship a
credential. A real deployment puts an identity provider in front of the gateway
and drops `/auth/token` entirely.

---

## TLS

`provision.sh` does **not** configure TLS, because it cannot know your domain.
Before anyone else uses the box:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain
```

Until then the sign-in key and every JWT cross the network in clear text.

---

## Operating it

```bash
# status
systemctl status 'financehub-*'

# logs, live
journalctl -u financehub-reporting -f
journalctl -u financehub-validation -n 200

# restart everything
sudo systemctl restart financehub-validation financehub-matching \
  financehub-exceptions financehub-reporting \
  financehub-celery-worker financehub-celery-beat
```

### Deploying a change

```bash
cd /opt/financehub && sudo git pull
sudo bash deploy/provision.sh     # reinstalls deps, rebuilds the SPA, restarts units
```

---

## Troubleshooting

**A unit will not start.** `journalctl -u <unit> -n 50`. The usual cause is
`.env` — `EnvironmentFile` is strict about syntax, and a value containing an
unquoted `#` truncates.

**`ModuleNotFoundError: shared`.** `WorkingDirectory` must be `/opt/financehub`,
the repo root. The services import `shared.*` and `services.*`, which resolve
from nowhere else.

**Model file is not written.** `ProtectSystem=strict` makes the filesystem
read-only except for the paths each unit declares. The matching and exception
units carry `ReadWritePaths` for their `models/` directories; a new writable
path needs adding there too.

**Dashboard shows 401 on every panel.** No token. The sign-in card should
appear first — if it does not, `SERVICE_API_KEY` was empty when the SPA was
built. Re-run `provision.sh` after confirming it is set in `.env`.

**Dashboard shows zeroes.** Not an error — an empty database. Seed it.

**WebSocket keeps reconnecting.** Check nginx passed the `Upgrade` headers and
that `proxy_read_timeout` is high; a quiet exception queue on the default 60s
would drop the socket all day.
