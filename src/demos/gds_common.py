"""Shared GDS helpers used by the fast-gds and gds-probe demos.

Holds the single-statement runner, the graph-drop Cypher, and the projection runner that
cleans up after a failed projection. Both GDS paths need them.
"""

from __future__ import annotations

import time
from uuid import uuid4

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError

from helpers import query_error

Rows = list[dict[str, object]]

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
    except (Neo4jError, DriverError) as exc:
        elapsed = time.perf_counter() - t0
        print(f"  FAILED after {elapsed:.1f}s: {query_error(exc)}")
        if raise_on_error:
            raise
        return None
    elapsed = time.perf_counter() - t0
    print(f"  OK {elapsed:.1f}s, {len(rows)} row(s)")
    return rows


def new_graph_name(prefix: str) -> str:
    """Return a unique graph name that a failed Aura session cannot block."""
    return f"{prefix}_{uuid4().hex[:12]}"


def retryable_projection_error(exc: Neo4jError) -> bool:
    """Recognize Aura session startup failures that need a fresh graph name."""
    message = exc.message or ""
    return ("SessionId[" in message and "not found" in message) or (
        "status code 409" in message and "graph mapping" in message
        and "already exists" in message
    )


def project_with_cleanup(driver: Driver, label: str, cypher: str,
                         params: dict[str, object], graph: str, *,
                         retry_prefix: str | None) -> tuple[Rows | None, str]:
    """Run a projection statement and drop the graph after any failure.

    A failed projection can still leave a session or graph mapping behind, so every
    failure path attempts a best-effort drop of the name it used. When ``retry_prefix``
    is set, one session startup failure retries under a fresh name built from it.
    Returns the projection rows, or ``None`` on failure, and the graph name last used.
    """
    for attempt in range(2):
        try:
            rows = run_statement(driver, f"{label} as '{graph}'", cypher,
                                 {**params, "graph": graph}, raise_on_error=True)
        except (Neo4jError, DriverError) as exc:
            run_statement(driver, f"clean up failed projection '{graph}'",
                          DROP_GRAPH, {"graph": graph})
            if (attempt == 0 and retry_prefix is not None
                    and isinstance(exc, Neo4jError)
                    and retryable_projection_error(exc)):
                graph = new_graph_name(retry_prefix)
                print(f"  Retrying projection once as '{graph}'.")
                continue
            return None, graph
        return rows, graph
    return None, graph
