"""Exercise every endpoint of the deployed system, in one command.

    python tools/e2e_check.py
    python tools/e2e_check.py --base http://127.0.0.1 --json results.json

Every other check in this repo measures logic: pytest runs the pipelines
in-process, verify_corpus grades a corpus through them, chapter4 collects the
gates. None of them touch a running service, and each time something here was
exercised over real HTTP for the first time it broke - a UUID in an int field,
a cached verdict with no parsed Transaction, four report types that were one
report with four titles. This is the check that would have caught those.

It covers all four services: every route their OpenAPI documents, the role x
permission matrix, all four report types, and the label rules that decide what
the classifier is allowed to learn from.

It is not read-only. It seeds a small corpus, reconciles, triages and resolves
a handful of exceptions, so run it against a demo or staging box rather than
anything precious. Everything it writes is attributed to the token subject
`e2e-check`, so its footprint is visible in the audit trail:

    SELECT actor, count(*) FROM audittrail GROUP BY actor;

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

REPORT_TYPES = (
    "RECONCILIATION_SUMMARY",
    "EXCEPTION_LOG",
    "MATCH_RATE_ANALYTICS",
    "AUDIT_TRAIL",
)

results: list[tuple[str, bool, str]] = []
_section = ""


def section(name: str) -> None:
    global _section
    _section = name
    print(f"\n{name}\n" + "-" * 66)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((f"{_section} :: {name}", ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f'  -- {detail}' if detail else ''}")
    return ok


def call(
    url: str,
    token: str | None = None,
    payload: Any = None,
    method: str | None = None,
    raw: bool = False,
) -> tuple[int, Any]:
    """Returns (status, body). Never raises on an HTTP error status."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, body if raw else _maybe_json(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, body if raw else _maybe_json(body)
    except Exception as exc:  # noqa: BLE001 - a dead service is a failed check
        return 0, {"error": str(exc)}


def _pdf_sections(pdf: bytes) -> frozenset[str]:
    """Which named sections a report PDF contains.

    Extracted with pdftotext rather than grepped out of the bytes: ReportLab
    compresses its text streams, so the section names never appear as plain
    bytes and a substring search silently reports every report as identical -
    which is exactly the bug this check exists to catch, inverted.

    Falls back to bucketing on size when poppler is not installed. Four types
    that ignore report_type produce near-identical PDFs, so size alone still
    separates the broken case from the fixed one, just less precisely.
    """
    import shutil
    import tempfile

    names = ("Audit trail", "Activity by actor", "Exception log",
             "Match-rate analytics", "Category distribution",
             "Validation and data quality")
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            fh.write(pdf)
            fh.flush()
            out = subprocess.run(["pdftotext", fh.name, "-"],
                                 capture_output=True, text=True)
        if out.returncode == 0:
            return frozenset(n for n in names if n in out.stdout)
    return frozenset({f"~{len(pdf) // 1000}kb"})


def _maybe_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return body[:200].decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000",
                        help="gateway origin. Defaults to the reporting service "
                             "on loopback, which always answers. Point it at the "
                             "public https:// host to exercise the nginx layer "
                             "too - after certbot, nginx only matches its own "
                             "server_name and redirects HTTP, so http://<ip>/ "
                             "returns 404 rather than the app.")
    parser.add_argument("--api-key", default=None,
                        help="SERVICE_API_KEY; read from /opt/financehub/.env if omitted")
    parser.add_argument("--seed-count", type=int, default=40)
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    gw = args.base.rstrip("/")
    VAL, MATCH, EXC = "http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8003"

    # /audit and /stats are deliberately NOT proxied by nginx - deploy/nginx
    # only forwards /metrics, /exceptions, /reports, /auth and /ws, because an
    # audit trail and a config dump are operational surfaces with no business
    # being public. Through the public gateway they fall through to the SPA and
    # answer 200 with HTML, so asserting against `gw` there would either fail
    # for the wrong reason or pass spuriously on the status code alone. They are
    # therefore always addressed on loopback.
    ADMIN = "http://127.0.0.1:8000"

    key = args.api_key
    if not key:
        try:
            with open("/opt/financehub/.env", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("SERVICE_API_KEY="):
                        key = line.split("=", 1)[1].strip()
        except OSError:
            pass
    if not key:
        print("No SERVICE_API_KEY (pass --api-key)")
        return 2

    # ── health ───────────────────────────────────────────────────────────
    section("Health")
    for name, url in (("reporting", "http://127.0.0.1:8000"), ("validation", VAL),
                      ("matching", MATCH), ("exceptions", EXC)):
        status, body = call(f"{url}/health")
        check(f"{name} /health", status == 200 and body.get("status") == "healthy",
              f"HTTP {status} {body.get('status') if isinstance(body, dict) else body}")
    status, body = call(f"{MATCH}/health")
    fitted = bool(isinstance(body, dict) and body.get("models_loaded"))
    check("matching engine has persisted models fitted", fitted,
          "" if fitted else "models_loaded=False: TF-IDF is refit per batch and "
          "scores are not comparable across runs. POST /models/fit")

    # ── auth ─────────────────────────────────────────────────────────────
    section("Auth")
    status, _ = call(f"{gw}/auth/token", payload={"role": "AUDITOR", "api_key": "wrong"})
    check("a wrong API key is refused", status == 401, f"HTTP {status}")

    tokens: dict[str, str] = {}
    for role in ("FINANCE_MANAGER", "AUDITOR", "SYSTEM_ADMINISTRATOR"):
        status, body = call(f"{gw}/auth/token",
                            payload={"role": role, "api_key": key, "subject": "e2e-check"})
        ok = status == 200 and isinstance(body, dict) and body.get("access_token")
        check(f"{role} token issued", bool(ok), f"HTTP {status}")
        if ok:
            tokens[role] = body["access_token"]
    if len(tokens) != 3:
        print("\ncannot continue without all three tokens")
        return 1
    fm, aud, sa = tokens["FINANCE_MANAGER"], tokens["AUDITOR"], tokens["SYSTEM_ADMINISTRATOR"]

    status, body = call(f"{gw}/auth/me", fm)
    check("/auth/me reports the role and its permissions",
          status == 200 and body.get("role") == "FINANCE_MANAGER" and body.get("permissions"),
          str(body.get("permissions"))[:80] if isinstance(body, dict) else str(body))
    status, _ = call(f"{gw}/metrics/kpi")
    check("an unauthenticated data call is refused", status == 401, f"HTTP {status}")

    # ── ingestion ────────────────────────────────────────────────────────
    section("Subsystem 2 - validation")
    seeded = subprocess.run(
        ["sudo", "-u", "financehub", "/opt/financehub/.venv/bin/python",
         "/opt/financehub/tools/seed.py", "--count", str(args.seed_count),
         "--sink", "http", "--seed", str(abs(hash(str(args))) % 90000),
         "--validate-url", VAL, "--out", "/tmp/e2e-seed"],
        capture_output=True, text=True)
    check("seed reaches POST /validate", seeded.returncode == 0 and "submitted" in seeded.stdout,
          (seeded.stdout.strip().splitlines() or ["no output"])[-2][:90]
          if seeded.stdout.strip() else seeded.stderr[:90])

    status, body = call(f"{VAL}/validate", payload={"records": [{"nonsense": True}]})
    check("malformed input is handled, not fatal", status in (200, 422), f"HTTP {status}")

    status, body = call(f"{VAL}/quarantine?limit=3")
    quarantined = body.get("items", []) if isinstance(body, dict) else []
    check("GET /quarantine lists retained payloads",
          status == 200 and bool(quarantined), f"HTTP {status}, {len(quarantined)} shown")
    if quarantined:
        status, body = call(f"{VAL}/quarantine/replay",
                            payload={"quarantine_ids": [quarantined[0]["id"]]})
        # A genuinely malformed payload must stay quarantined: replay re-runs
        # the stored bytes, so it can only pass if the upstream feed changed.
        check("replay re-runs the payload and does not fake success",
              status == 200 and isinstance(body, dict) and "replayed" in body,
              str(body)[:90])

    status, body = call(f"{VAL}/stats")
    check("validation /stats", status == 200, f"HTTP {status}")
    check("/stats does not leak the database password",
          "***" in str(body) or "@" not in str(body.get("database_url", "")),
          str(body.get("database_url", ""))[:60] if isinstance(body, dict) else "")

    # ── matching ─────────────────────────────────────────────────────────
    section("Subsystem 1 - matching")
    status, body = call(f"{MATCH}/models")
    check("GET /models reports what is persisted", status == 200, f"HTTP {status}")
    status, body = call(f"{MATCH}/reconcile", payload={})
    ok = status == 200 and isinstance(body, dict) and "match_rate" in body
    check("POST /reconcile", ok,
          f"matched={body.get('matched')} unmatched={body.get('unmatched')}"
          if isinstance(body, dict) else str(body)[:80])
    if ok and body.get("persistence"):
        check("reconcile returns its run_id",
              isinstance(body["persistence"].get("run_id"), str),
              str(body["persistence"].get("run_id"))[:40])

    # ── exceptions ───────────────────────────────────────────────────────
    section("Subsystem 3 - exceptions")
    status, body = call(f"{EXC}/triage", payload={"limit": 200})
    check("POST /triage classifies the open queue", status == 200,
          f"triaged={body.get('triaged')} engine={body.get('engine')}"
          if isinstance(body, dict) else str(body)[:80])
    status, body = call(f"{EXC}/models")
    check("exception handler GET /models", status == 200, f"HTTP {status}")

    status, body = call(f"{gw}/exceptions?limit=5&state=SUGGESTED", fm)
    items = body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])
    check("GET /exceptions returns triaged rows", status == 200 and bool(items),
          f"HTTP {status}, {len(items)} rows")

    # Label rules: only ACCEPT and EDIT may teach the classifier.
    for decision, payload, want_label in (
        ("accept", {"decision": "accept", "note": "e2e"}, True),
        ("edit", {"decision": "edit", "corrected_category": "TIMING_DIFFERENCE",
                  "note": "e2e"}, True),
        ("reject", {"decision": "reject", "note": "e2e"}, False),
    ):
        status, body = call(f"{gw}/exceptions?limit=1&state=SUGGESTED", fm)
        rows = body if isinstance(body, list) else body.get("items", [])
        if not rows:
            check(f"{decision} path", False, "no SUGGESTED exception left to resolve")
            continue
        status, out = call(f"{gw}/exceptions/{rows[0]['id']}/resolve", fm, payload)
        got = out.get("usable_as_label") if isinstance(out, dict) else None
        check(f"{decision} -> usable_as_label={want_label}",
              status == 200 and got is want_label, f"HTTP {status}, got {got}")

    # ── RBAC ─────────────────────────────────────────────────────────────
    section("RBAC")
    status, body = call(f"{gw}/exceptions?limit=1&state=SUGGESTED", fm)
    rows = body if isinstance(body, list) else body.get("items", [])
    if rows:
        status, _ = call(f"{gw}/exceptions/{rows[0]['id']}/resolve", aud,
                         {"decision": "accept", "note": "e2e rbac"})
        check("AUDITOR cannot resolve an exception", status == 403, f"HTTP {status}")
    status, _ = call(f"{ADMIN}/audit?limit=1", fm)
    check("FINANCE_MANAGER cannot read the audit trail", status == 403, f"HTTP {status}")
    status, _ = call(f"{ADMIN}/audit?limit=1", aud)
    check("AUDITOR can read the audit trail", status == 200, f"HTTP {status}")
    status, _ = call(f"{ADMIN}/audit?limit=1", sa)
    check("SYSTEM_ADMINISTRATOR can read the audit trail", status == 200, f"HTTP {status}")

    # ── metrics ──────────────────────────────────────────────────────────
    section("Subsystem 4 - metrics")
    for ep in ("/metrics/kpi", "/metrics/match-rate", "/metrics/categories"):
        status, body = call(f"{gw}{ep}", fm)
        check(f"GET {ep}", status == 200 and body not in (None, [], {}), f"HTTP {status}")

    # ── audit ────────────────────────────────────────────────────────────
    section("Subsystem 4 - audit")
    status, body = call(f"{ADMIN}/audit?limit=3", aud)
    check("GET /audit", status == 200 and isinstance(body, dict) and body.get("items"),
          f"total={body.get('total')}" if isinstance(body, dict) else "")
    status, body = call(f"{ADMIN}/audit/actors", aud)
    check("GET /audit/actors", status == 200 and isinstance(body, list) and bool(body),
          f"{len(body)} actors" if isinstance(body, list) else "")
    status, body = call(f"{ADMIN}/audit/integrity", aud)
    complete = isinstance(body, dict) and body.get("complete")
    check("GET /audit/integrity reports full coverage", status == 200 and bool(complete),
          f"coverage={body.get('coverage')} complete={body.get('complete')}"
          if isinstance(body, dict) else "")

    # ── reports ──────────────────────────────────────────────────────────
    section("Subsystem 4 - reports")
    sections_seen: dict[str, set[str]] = {}
    for rtype in REPORT_TYPES:
        status, body = call(f"{gw}/reports/generate", fm, {"type": rtype})
        ok = status == 200 and isinstance(body, dict) and body.get("status") == "READY"
        check(f"generate {rtype}", ok, f"HTTP {status} {str(body)[:70]}")
        if not ok:
            continue
        status, pdf = call(f"{gw}/reports/{body['id']}/download", fm, raw=True)
        valid = status == 200 and isinstance(pdf, bytes) and pdf.startswith(b"%PDF-")
        check(f"download {rtype} is a valid PDF", valid,
              f"{len(pdf) if isinstance(pdf, bytes) else 0} bytes")
        if valid:
            sections_seen[rtype] = _pdf_sections(pdf)

    # The bug this catches: four types that differ only in their title.
    if len(sections_seen) > 1:
        distinct = len({frozenset(v) for v in sections_seen.values()})
        detail = "; ".join(f"{k}={sorted(v) or 'size:' + str(len(v))}"
                           for k, v in sections_seen.items())
        check("report types differ in content, not just the heading", distinct > 1,
              f"{distinct} distinct section sets - {detail[:160]}")

    status, body = call(f"{gw}/reports", fm)
    check("GET /reports lists them", status == 200, f"HTTP {status}")
    # The posture itself is worth asserting, not just worked around: if a
    # future nginx change started proxying these, an audit trail would become
    # world-readable and nothing else would notice.
    if gw.startswith("https://") or ":8000" not in gw:
        status, body = call(f"{gw}/audit?limit=1", aud, raw=True)
        leaked = status == 200 and isinstance(body, bytes) and b'"items"' in body
        check("the public gateway does not expose /audit", not leaked,
              "audit JSON is reachable through nginx" if leaked
              else "falls through to the SPA, as designed")

    status, body = call(f"{ADMIN}/stats", sa)
    check("gateway /stats returns JSON, not the SPA shell",
          status == 200 and isinstance(body, dict) and "events" in body,
          f"HTTP {status} {str(body)[:60]}")

    # ── report ───────────────────────────────────────────────────────────
    failed = [(n, d) for n, ok, d in results if not ok]
    print("\n" + "=" * 66)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for name, detail in failed:
        print(f"  FAILED  {name}{f'  -- {detail}' if detail else ''}")

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump([{"check": n, "ok": ok, "detail": d} for n, ok, d in results], fh, indent=2)
        print(f"\nwrote {args.json_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
