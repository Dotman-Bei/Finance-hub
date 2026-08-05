"""Stage 2 of 4 - business rules via Great Expectations (build.md Sec. 8).

"logical checks beyond structure: amount positive, txn_date not in the future,
reference_code matches an approved format, currency in an allowed set. Define a
GE suite in expectations/ and run it per record batch."

Two implementation notes worth knowing before changing anything here:

* GE builds its context and resolves metrics lazily and slowly, so the context,
  data source and validation definition are constructed once per process and
  reused. Rebuilding them per batch costs roughly a second each time.
* GE reports failures per *column expectation*, listing the row offsets that
  failed (`unexpected_index_list`). We invert that into row -> violations so a
  single bad record is quarantined on its own instead of failing its batch.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import great_expectations as gx
import pandas as pd
from great_expectations.core import ExpectationSuite
from great_expectations.expectations.registry import get_expectation_impl

logger = logging.getLogger(__name__)

SUITE_DIR = Path(__file__).resolve().parent.parent / "expectations"
DEFAULT_SUITE = SUITE_DIR / "transaction_suite.json"

#: Columns the suite reasons about. A batch missing one of these still
#: validates - GE reports the absent column rather than raising.
SUITE_COLUMNS = (
    "amount",
    "txn_date",
    "currency",
    "source_type",
    "reference_code",
    "external_id",
)


class RuleProcessor:
    """Runs the Great Expectations suite over a batch of candidate records."""

    def __init__(self, suite_path: Path | str = DEFAULT_SUITE) -> None:
        self.suite_path = Path(suite_path)
        if not self.suite_path.exists():
            raise FileNotFoundError(f"GE suite not found: {self.suite_path}")

        self._suite_json = self._load_suite_json()
        self._context = gx.get_context(mode="ephemeral")
        self._batch_definition = self._build_batch_definition()
        self._validation_definition = None  # built on first use, see _ensure_vd

    # ── setup ────────────────────────────────────────────────────────────

    def _load_suite_json(self) -> dict[str, Any]:
        import json

        with self.suite_path.open(encoding="utf-8") as fh:
            payload = json.load(fh)

        if not payload.get("expectations"):
            raise ValueError(f"GE suite {self.suite_path} declares no expectations")
        return payload

    def _build_batch_definition(self):
        source = self._context.data_sources.add_pandas("validation_pipeline")
        asset = source.add_dataframe_asset(name="candidate_transactions")
        return asset.add_batch_definition_whole_dataframe("batch")

    def _build_suite(self, as_of: dt.date) -> ExpectationSuite:
        """Rebuild the suite from JSON, injecting the runtime date bound.

        "txn_date not in the future" is relative to now, so it cannot be
        frozen into the file. Everything else comes straight from the JSON.
        """
        suite = self._context.suites.add_or_update(
            ExpectationSuite(name=self._suite_json["name"])
        )

        for spec in self._suite_json["expectations"]:
            impl = get_expectation_impl(spec["type"])
            # meta carries the human-readable rule text that ends up in the
            # violation message and in validationlogs, so it must be passed
            # through - not just the kwargs.
            suite.add_expectation(impl(**spec["kwargs"], meta=spec.get("meta") or {}))

        # The dynamic rule, added last so the JSON stays declarative.
        suite.add_expectation(
            get_expectation_impl("expect_column_values_to_be_between")(
                column="txn_date",
                max_value=as_of,
                meta={"rule": "build.md Sec. 8 - txn_date not in the future"},
            )
        )
        return suite

    def _ensure_validation_definition(self, as_of: dt.date):
        suite = self._build_suite(as_of)
        self._validation_definition = self._context.validation_definitions.add_or_update(
            gx.ValidationDefinition(
                data=self._batch_definition,
                suite=suite,
                name="transaction_rules",
            )
        )
        return self._validation_definition

    # ── validation ───────────────────────────────────────────────────────

    def validate_frame(
        self, frame: pd.DataFrame, as_of: dt.date | None = None
    ) -> dict[int, list[str]]:
        """Return {positional row index -> violations} for a candidate batch.

        Rows absent from the mapping satisfied every business rule. The index
        is positional, not the DataFrame's label, so callers can align results
        with the batch they submitted.
        """
        if frame.empty:
            return {}

        as_of = as_of or dt.date.today()
        validation_definition = self._ensure_validation_definition(as_of)

        prepared = self._prepare(frame)

        result = validation_definition.run(
            batch_parameters={"dataframe": prepared},
            result_format="COMPLETE",
        )

        violations: dict[int, list[str]] = {}

        for outcome in result.results:
            if outcome.success:
                continue

            config = outcome.expectation_config
            column = config.kwargs.get("column", "?")
            rule = (config.meta or {}).get("rule", config.type)

            failed_rows = outcome.result.get("unexpected_index_list") or []
            failed_values = outcome.result.get("partial_unexpected_list") or []

            if not failed_rows:
                # A column-level failure with no row offsets - e.g. the column
                # is missing entirely. That invalidates the whole batch.
                message = f"{column}: {rule} (column-level failure)"
                for index in range(len(prepared)):
                    violations.setdefault(index, []).append(message)
                continue

            for position, row_index in enumerate(failed_rows):
                index = self._as_position(row_index)
                if index is None:
                    continue
                value = failed_values[position] if position < len(failed_values) else None
                violations.setdefault(index, []).append(
                    f"{column}: {rule} (got {value!r})"
                )

        return violations

    @staticmethod
    def _as_position(row_index: Any) -> int | None:
        """GE returns either an int offset or a dict of index columns."""
        if isinstance(row_index, int):
            return row_index
        if isinstance(row_index, dict):
            for value in row_index.values():
                if isinstance(value, int):
                    return value
        return None

    @staticmethod
    def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
        """Reset to a clean 0..n-1 index and coerce the typed columns.

        Values that will not coerce become NaT/NaN, which the not-null
        expectations then catch - a parse failure must surface as a violation,
        never as a silently dropped row.
        """
        prepared = frame.reset_index(drop=True).copy()

        for column in SUITE_COLUMNS:
            if column not in prepared.columns:
                prepared[column] = None

        prepared["amount"] = pd.to_numeric(prepared["amount"], errors="coerce")
        prepared["txn_date"] = pd.to_datetime(
            prepared["txn_date"], errors="coerce"
        ).dt.date

        for column in ("currency", "source_type", "reference_code", "external_id"):
            prepared[column] = prepared[column].where(prepared[column].notna(), None)

        return prepared


__all__ = ["RuleProcessor", "DEFAULT_SUITE", "SUITE_DIR", "SUITE_COLUMNS"]
