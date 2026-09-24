"""Basic demo (``--demo basic``).

The warm-up exploration / visualization queries from ``basic-graph-examples.md``:
simple counts and small, anchored traversals that show the value of the relationships
without any fraud logic.
"""

from __future__ import annotations

import time

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError

from helpers import first_row, print_table, query_error, run_cypher
from queries import BASIC_QUERIES, BasicQuery

DEFAULT_ACCOUNT_ID = 17813
DEFAULT_MERCHANT_ID = 1


def pick_anchors(driver: Driver) -> tuple[int, int]:
    """Pick an anchor account and a merchant for the basic graph queries.

    The anchored traversals need a node to start from. The defaults are account 17813
    and merchant 1, which the numbers in ``basic-graph-examples.md`` are measured
    against. If a default is missing, the fallback is the lowest account_id with an
    outgoing transfer, or the lowest merchant_id. The chosen ids are printed so the
    same query can be pasted into the Aura Workspace.
    """
    rec, _, _ = driver.execute_query(
        "MATCH (a:Account {account_id: $id}) RETURN a.account_id AS id",
        id=DEFAULT_ACCOUNT_ID,
    )
    if not rec:
        rec, _, _ = driver.execute_query(
            "MATCH (a:Account)-[:TRANSFERRED_TO]->(:Account) "
            "RETURN a.account_id AS id ORDER BY id LIMIT 1"
        )
    account_id = first_row(rec, "an anchor account")["id"]
    rec, _, _ = driver.execute_query(
        "MATCH (m:Merchant {merchant_id: $id}) RETURN m.merchant_id AS id",
        id=DEFAULT_MERCHANT_ID,
    )
    if not rec:
        rec, _, _ = driver.execute_query(
            "MATCH (m:Merchant) RETURN m.merchant_id AS id ORDER BY id LIMIT 1"
        )
    merchant_id = first_row(rec, "an anchor merchant")["id"]
    return account_id, merchant_id


def run_basic_query(driver: Driver, query: BasicQuery, max_rows: int, timeout: float,
                    params: dict[str, object]) -> bool:
    """Execute one basic query and print rows (table) or a count (graph).

    Returns False when the query errored, so the CLI can exit non-zero.
    """
    print(f"\n{'=' * 78}")
    print(f"[B{query.number}] {query.title}  ({query.kind})")
    if query.note:
        print(f"  note: {query.note}")
    print("=" * 78)

    t0 = time.perf_counter()
    try:
        rows = run_cypher(driver, query.cypher, params, timeout)
    except (Neo4jError, DriverError) as exc:
        print(f"  ERROR after {time.perf_counter() - t0:.1f}s: {query_error(exc)}")
        return False
    elapsed = time.perf_counter() - t0

    if query.kind == "graph":
        print(f"  OK {elapsed:.1f}s, {len(rows)} path/row(s) returned.")
        print("  Run this in the Aura Workspace Query tab to see the visualization.")
    else:
        print(f"  OK {elapsed:.1f}s, {len(rows)} row(s).")
        print_table(rows[:max_rows], max_rows, total_matched=len(rows))
    return True


def run_basic(driver: Driver, max_rows: int, timeout: float) -> bool:
    """Run all basic exploration and visualization queries.

    Returns True only when every query succeeded.
    """
    account_id, merchant_id = pick_anchors(driver)
    params = {"account_id": account_id, "merchant_id": merchant_id}
    print(f"Anchors: account_id={account_id}, merchant_id={merchant_id} "
          f"(used by the graph queries).")
    print(f"Running {len(BASIC_QUERIES)} basic quer"
          f"{'y' if len(BASIC_QUERIES) == 1 else 'ies'} (timeout {timeout:g}s each).")
    failed = [query.number for query in BASIC_QUERIES
              if not run_basic_query(driver, query, max_rows, timeout, params)]
    if failed:
        print(f"\n{len(failed)} basic quer{'y' if len(failed) == 1 else 'ies'} "
              f"failed: {', '.join(f'B{n}' for n in failed)}")
    return not failed
