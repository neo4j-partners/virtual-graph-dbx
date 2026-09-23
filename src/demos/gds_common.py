"""Shared GDS helpers used by the fast-gds and gds-probe demos.

Holds the single-statement runner and the graph-drop Cypher that both GDS paths need.
"""

from __future__ import annotations

import time

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError

from helpers import driver_error

DROP_GRAPH = """
CALL gds.graph.drop($graph, false)
YIELD graphName
RETURN graphName
"""


def run_statement(driver: Driver, label: str, cypher: str,
                  params: dict[str, object], *,
                  raise_on_error: bool = False) -> list[dict[str, object]] | None:
    """Run one statement to completion, timing it and reporting any Neo4j error.

    Uses an explicit ``session.run`` (no managed-transaction retry) so a slow statement
    is never silently re-run, then returns its rows. On a Neo4jError the code and
    message are printed and ``None`` is returned, since learning *why* a statement is
    rejected is the point of this probe. ``raise_on_error`` lets the fast demo inspect
    projection failures after they have been reported.
    """
    print(f"\n--- {label}")
    t0 = time.perf_counter()
    try:
        with driver.session() as session:
            rows = [record.data() for record in session.run(cypher, **params)]
    except Neo4jError as exc:
        elapsed = time.perf_counter() - t0
        print(f"  FAILED after {elapsed:.1f}s: {exc.code}\n  {exc.message}")
        if raise_on_error:
            raise
        return None
    except DriverError as exc:
        elapsed = time.perf_counter() - t0
        print(f"  FAILED after {elapsed:.1f}s: {driver_error(exc)}")
        if raise_on_error:
            raise
        return None
    elapsed = time.perf_counter() - t0
    print(f"  OK {elapsed:.1f}s, {len(rows)} row(s)")
    return rows
