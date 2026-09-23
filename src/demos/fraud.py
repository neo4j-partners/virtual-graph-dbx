"""Fraud demo (``--demo fraud``).

The fraud-signal queries 1-10 from ``finding-fraud.md``. The server aggregates,
applies each threshold (a ``WHERE`` after the aggregating ``WITH``), orders and limits;
"recent" windows are passed as a precomputed ``$since`` parameter. Only the courier
query still merges two halves and filters here in Python. Query 11 is documented but
unsupported on the Virtual Graph, so it is never run. See ``queries.py``.
"""

from __future__ import annotations

import argparse
import sys
import time

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError
from neo4j.time import Date, DateTime

from helpers import data_max_dates, driver_error, print_table, run_cypher, since_param
from queries import QUERIES, Query, Row


def merge_enrichment(rows: list[Row], enrich_rows: list[Row], key: str,
                     columns: dict[str, object]) -> None:
    """Copy ``columns`` from the enrich rows onto ``rows`` by ``key``, in place.

    Replaces an ``OPTIONAL MATCH``: an account absent from the enrich result gets the
    given default for each column, so rows with no optional match are preserved.
    """
    lookup = {row[key]: row for row in enrich_rows}
    for row in rows:
        match = lookup.get(row[key])
        for col, default in columns.items():
            row[col] = match[col] if match is not None else default


def run_query(driver: Driver, query: Query, max_rows: int, timeout: float,
              max_transfer: DateTime, max_opened: Date) -> None:
    """Execute one query, merge any enrichment, apply a client filter, and print."""
    marker = "OK" if query.vg_supported else "unsupported"
    print(f"\n{'=' * 78}")
    print(f"[{query.number}] {query.title}  (Virtual Graph: {marker})")
    if not query.vg_supported:
        print(f"  Not run. {query.note}")
        return
    if query.note:
        print(f"  note: {query.note}")
    print("=" * 78)

    params: dict[str, object] = {}
    if query.since_window_days is not None:
        params["since"] = since_param(query, max_transfer, max_opened)
        print(f"  $since = {params['since']}")

    t0 = time.perf_counter()
    try:
        rows = run_cypher(driver, query.cypher, params, timeout)
        if query.enrich_cypher is not None:
            enrich_rows = run_cypher(driver, query.enrich_cypher, {}, timeout)
            merge_enrichment(rows, enrich_rows, query.enrich_key, query.enrich_columns)
    except Neo4jError as exc:
        print(f"  ERROR after {time.perf_counter() - t0:.1f}s: {exc.code}\n"
              f"  {exc.message}")
        return
    except DriverError as exc:
        print(f"  ERROR after {time.perf_counter() - t0:.1f}s: {driver_error(exc)}")
        return
    elapsed = time.perf_counter() - t0
    if query.row_transform is not None:
        for row in rows:
            query.row_transform(row)

    if query.client_filter is None:
        matched = rows
        print(f"  OK {elapsed:.1f}s; {len(matched)} row(s)")
    else:
        matched = [r for r in rows if query.client_filter(r)]
        print(f"  OK {elapsed:.1f}s; {len(rows)} server row(s); "
              f"{len(matched)} pass the client-side threshold")
    print_table(matched[: max_rows], max_rows, total_matched=len(matched))


def select_queries(args: argparse.Namespace) -> list[Query]:
    """Resolve which fraud queries to run from the CLI flags."""
    by_number = {q.number: q for q in QUERIES}
    if args.only:
        missing = [n for n in args.only if n not in by_number]
        if missing:
            sys.exit(f"No quer{'y' if len(missing) == 1 else 'ies'} numbered "
                     f"{', '.join(map(str, missing))}")
        return [by_number[n] for n in args.only]
    if args.query is not None:
        if args.query not in by_number:
            sys.exit(f"No query numbered {args.query} (valid: 1-{len(QUERIES)})")
        return [by_number[args.query]]
    return [q for q in QUERIES if q.vg_supported]


def run_fraud(driver: Driver, args: argparse.Namespace) -> None:
    """Run the selected fraud-signal queries."""
    selected = select_queries(args)
    max_transfer, max_opened = data_max_dates(driver)
    print(f"Connected. Data max transfer={max_transfer}, max opened={max_opened}.")
    print(f"Running {len(selected)} quer{'y' if len(selected) == 1 else 'ies'} "
          f"(timeout {args.timeout:g}s each).")
    for query in selected:
        run_query(driver, query, args.rows, args.timeout, max_transfer, max_opened)
