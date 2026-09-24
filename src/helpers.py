"""Shared query helpers for the Finance Genie Virtual Graph demos.

Connection-agnostic utilities used across the demos and the support scripts: the
``$since`` window math, the explicit-transaction query runner, the table printer, and
the error formatters.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Mapping, Sequence

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError
from neo4j.time import Date, DateTime

from queries import Query, Row


def data_max_dates(driver: Driver) -> tuple[DateTime, Date]:
    """Return the dataset's max transfer timestamp and max account opened_date.

    These anchor the "recent" windows so they land inside the synthetic dataset
    (which ends 2024-03-30) instead of around an empty present day. Exits with a clear
    message when the graph has no transfers or no accounts.
    """
    rec, _, _ = driver.execute_query(
        "MATCH ()-[t:TRANSFERRED_TO]->() RETURN max(t.transfer_timestamp) AS mx"
    )
    max_transfer = first_row(rec, "max(transfer_timestamp)")["mx"]
    rec, _, _ = driver.execute_query(
        "MATCH (a:Account) RETURN max(a.opened_date) AS mx"
    )
    max_opened = first_row(rec, "max(opened_date)")["mx"]
    if max_transfer is None or max_opened is None:
        sys.exit("The graph has no transfers or no accounts, so the recent windows "
                 "cannot be anchored. Check the Virtual Graph mapping and tables.")
    return max_transfer, max_opened


def first_row(records: Sequence[Mapping[str, object]],
              what: str) -> Mapping[str, object]:
    """Return the first record, or exit with a clear message when there is none."""
    if not records:
        sys.exit(f"No rows came back for {what}. Check the Virtual Graph mapping "
                 "and tables.")
    return records[0]


def since_param(query: Query, max_transfer: DateTime,
                max_opened: Date) -> dt.datetime | dt.date:
    """Compute the ``$since`` cutoff for a windowed query."""
    window = dt.timedelta(days=query.since_window_days)
    if query.since_source == "opened":
        anchor = max_opened.to_native()  # datetime.date
        return anchor - window
    anchor = max_transfer.to_native()  # datetime.datetime
    return anchor - window


def run_cypher(driver: Driver, cypher: str, params: dict[str, object],
               timeout: float) -> list[Row]:
    """Run one statement in an explicit transaction (no managed-transaction retry)."""
    with (
        driver.session() as session,
        session.begin_transaction(timeout=timeout) as tx,
    ):
        result = tx.run(cypher, **params)
        return [record.data() for record in result]


def print_table(rows: list[Row], max_rows: int, total_matched: int) -> None:
    """Print result rows as a simple aligned table.

    ``rows`` is the slice to print and ``total_matched`` the full match count, so
    ``--rows 0`` still reports how many rows matched.
    """
    if total_matched == 0:
        print("  (no rows)")
        return
    if not rows:
        print(f"  ... {total_matched} matching row(s) not shown (--rows {max_rows})")
        return

    columns = list(rows[0].keys())
    widths = {
        col: max(len(col), *(len(_fmt(row.get(col))) for row in rows))
        for col in columns
    }
    print(("  " + "  ".join(col.ljust(widths[col]) for col in columns)).rstrip())
    print("  " + "  ".join("-" * widths[col] for col in columns))
    for row in rows:
        cells = (_fmt(row.get(col)).ljust(widths[col]) for col in columns)
        print(("  " + "  ".join(cells)).rstrip())
    if total_matched > max_rows:
        print(f"  ... {total_matched - max_rows} more matching row(s)")


def _fmt(value: object) -> str:
    """Compact display of a cell value (truncate long account lists)."""
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value)
        return text if len(text) <= 60 else text[:57] + "..."
    return str(value)


def driver_error(exc: DriverError) -> str:
    """One-line description of a client-side driver failure (timeout, lost connection).

    These are not server ``Neo4jError`` codes. The useful detail is the exception type
    and its underlying cause, often ``TimeoutError`` from the server's 60s Bolt read
    timeout. The query usually keeps running server-side, so the run continues with the
    next query rather than aborting. The timeout hint is added only when the cause is a
    ``TimeoutError``, since a DNS failure or bad credentials has nothing to do with it.
    """
    cause = exc.__cause__
    suffix = f" (cause: {type(cause).__name__})" if cause is not None else ""
    hint = (". Likely the 60s Bolt read timeout, or the warehouse is too small"
            if isinstance(cause, TimeoutError) else "")
    return f"{type(exc).__name__}: {exc}{suffix}{hint}"


def query_error(exc: Neo4jError | DriverError, indent: str = "  ") -> str:
    """Describe a failed statement: the server code and message, or the driver failure.

    A ``Neo4jError`` prints its code, then its message on the next line after
    ``indent``. A ``DriverError`` prints the one-line ``driver_error`` summary.
    """
    if isinstance(exc, Neo4jError):
        return f"{exc.code}\n{indent}{exc.message}"
    return driver_error(exc)
