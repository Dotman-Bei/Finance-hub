"""Load a real transaction corpus as the internal ledger.

Companion to tools/seed.py. `seed.py` generates both sides of the
reconciliation; this replaces the *internal* side with real transactions, so
amounts, dates, reference codes, counterparties and descriptions all come from
observed data rather than from a random number generator. The external side is
still derived, because no institution publishes both halves - see the module
docstring in seed.py for why that is a property of the problem rather than a
shortcut.

The source
----------
UCI Machine Learning Repository, "Online Retail II" (dataset 502): ~1.07M line
items from a UK online retailer, Dec 2009 - Dec 2011. Freely downloadable, no
account required.

    https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip

It fits because it has the one thing fraud-detection corpora lack: **real
invoice numbers**. A reconciliation system matches on references, and a
PCA-anonymised feature matrix has none. Aggregating line items per invoice
also yields genuine per-invoice obligations, which is what an ERP ledger
actually holds.

What is filtered, and why
-------------------------
Measured on the full file:

    raw line items      1,067,371
    cancellations          19,494   invoice prefixed 'C', negative quantities
    null Customer ID      243,007   no identifiable counterparty
    non-positive qty       22,950
    non-positive price      6,207
    -> clean lines        805,549   (75.5%)
    -> invoices            36,969

Cancellations cannot survive ingestion regardless: they carry negative
amounts, and both the Pydantic validator and the Great Expectations suite
require a positive one. Excluding them is not tuning the data to pass - it is
removing records the system is designed to reject, which would otherwise
inflate the quarantine rate with items that were never transactions.

Dropping null Customer ID is the largest cut and the most debatable, since it
may skew toward retail over wholesale. It is taken because a counterparty is
what the description similarity and clustering work on. 36,969 invoices remain
against the few thousand any run needs, so the sample is not scarce.

Two transformations, both deliberate
------------------------------------
**Dates are shifted forward.** The data is from 2009-2011 and the KPI endpoint
defaults to a 30-day window, so a dashboard fed the original timeline would
show zeroes on every panel. The selected block is translated so its newest
invoice lands shortly before today. Only the offset changes: inter-invoice
intervals, day-of-week structure and seasonality are all preserved exactly,
because every date moves by the same amount.

**Reference codes are prefixed.** The pipeline enforces
`^REF-[0-9]{4,10}$` on `reference_code`, and real invoice numbers are bare
digits. `489434` becomes `REF-489434`: the identifier is unchanged and still
real, and the prefix maps it into the format this system's approved feed spec
requires - which is what an ETL adapter is for. The alternative, relaxing the
regex, would weaken a documented business rule and perturb the detection-rate
gate that depends on it.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger("real_ledger")

#: Sheets in the source workbook. Both are read and concatenated.
SOURCE_FILENAME = "online_retail_II.xlsx"

#: A UK retailer bills in sterling. The GE suite's allowed set includes GBP.
CURRENCY = "GBP"

#: Parsing 1.07M rows out of xlsx takes minutes; the invoice-level extract is
#: a few megabytes of CSV. Written next to the source, reused on every later run.
CACHE_FILENAME = "invoices.cache.csv"


def _resolve_source(path: Path) -> Path:
    """Accept the .zip, the .xlsx, or the directory holding either."""
    if path.is_dir():
        for candidate in (path / SOURCE_FILENAME, path / "online+retail+ii.zip"):
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"No {SOURCE_FILENAME} or zip found in {path}")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download it with:\n"
            "  curl -L -o online_retail_II.zip "
            "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
        )
    return path


def load_invoices(path: str | Path, refresh: bool = False) -> pd.DataFrame:
    """Invoice-level obligations from the raw workbook, cached after first read.

    Returns one row per invoice: reference, amount, date, counterparty, and the
    first line item's description.
    """
    source = _resolve_source(Path(path))
    cache = source.parent / CACHE_FILENAME

    if cache.exists() and not refresh:
        logger.info("Reading cached extract %s", cache)
        frame = pd.read_csv(cache, parse_dates=["txn_date"])
        return frame

    if source.suffix == ".zip":
        logger.info("Extracting %s", source)
        with zipfile.ZipFile(source) as archive:
            archive.extract(SOURCE_FILENAME, source.parent)
        source = source.parent / SOURCE_FILENAME

    logger.info("Parsing %s (minutes, once)", source)
    workbook = pd.ExcelFile(source)
    raw = pd.concat(
        [workbook.parse(sheet) for sheet in workbook.sheet_names], ignore_index=True
    )
    raw.columns = [c.strip() for c in raw.columns]
    raw["Invoice"] = raw["Invoice"].astype(str).str.strip()

    clean = raw[
        ~raw["Invoice"].str.startswith("C")
        & (raw["Quantity"] > 0)
        & (raw["Price"] > 0)
        & raw["Customer ID"].notna()
    ].copy()
    clean["line_total"] = clean["Quantity"] * clean["Price"]

    invoices = (
        clean.groupby("Invoice")
        .agg(
            amount=("line_total", "sum"),
            txn_date=("InvoiceDate", "min"),
            lines=("line_total", "size"),
            customer=("Customer ID", "first"),
            country=("Country", "first"),
            top_item=("Description", "first"),
        )
        .reset_index()
    )
    invoices["amount"] = invoices["amount"].round(2)
    # NUMERIC(18,2) and the GE ceiling both cap the upper end; the lower bound
    # is the suite's 0.01 minimum.
    invoices = invoices[(invoices["amount"] >= 0.01) & (invoices["amount"] < 1e9)]

    logger.info(
        "%d line items -> %d invoices (%.1f%% of raw lines retained)",
        len(raw), len(invoices), 100 * len(clean) / len(raw),
    )

    invoices.to_csv(cache, index=False)
    logger.info("Cached extract -> %s", cache)
    return invoices


def to_ledger(
    invoices: pd.DataFrame,
    count: int = 400,
    window_days: int = 120,
    headroom_days: int = 21,
    seed: int = 20260810,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Canonical internal-ledger rows drawn from the real corpus.

    `window_days` selects the most recent slice of the *original* timeline
    before shifting, so the result is dense rather than scattered over two
    years - a 30-day dashboard window on a 738-day spread would show almost
    nothing.

    `headroom_days` is the gap left between the newest shifted invoice and
    today. The derived external side applies settlement lags of up to 20 days
    (seed.py's MAX_FORWARD_SHIFT), and a lag applied to an invoice dated
    yesterday lands in the future, where the business-rule stage correctly
    quarantines it. Reserving the headroom is what stops the loader from
    manufacturing records it has labelled as good.
    """
    today = today or dt.date.today()
    frame = invoices.copy()
    frame["txn_date"] = pd.to_datetime(frame["txn_date"])

    newest = frame["txn_date"].max()
    cutoff = newest - pd.Timedelta(days=window_days)
    frame = frame[frame["txn_date"] > cutoff]

    if frame.empty:
        raise ValueError(f"No invoices within {window_days} days of {newest.date()}")

    if len(frame) > count:
        frame = frame.sample(n=count, random_state=seed)
    else:
        logger.warning(
            "Only %d invoices in the last %d days; asked for %d",
            len(frame), window_days, count,
        )

    # One offset for every row, so relative spacing survives untouched.
    target_newest = today - dt.timedelta(days=headroom_days)
    offset = pd.Timestamp(target_newest) - newest

    rng = random.Random(seed)
    ledger: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        invoice = str(row["Invoice"]).strip()
        shifted = (row["txn_date"] + offset).date()
        item = str(row["top_item"]).strip().title()
        customer = int(row["customer"])

        ledger.append(
            {
                "external_id": f"ERP-{invoice}",
                "amount": float(row["amount"]),
                "currency": CURRENCY,
                "txn_date": shifted,
                # Real product text and a real counterparty: this is what the
                # TF-IDF layer clusters on, so inventing it would hollow out
                # the one thing the ML layer actually reads.
                "description": f"{item} - Customer {customer} ({row['country']})",
                "reference_code": f"REF-{invoice}",
                "_source_invoice": invoice,
                "_lines": int(row["lines"]),
            }
        )

    rng.shuffle(ledger)
    return ledger


__all__ = ["load_invoices", "to_ledger", "CURRENCY"]
