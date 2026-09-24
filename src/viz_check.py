"""Verify the anchored fraud-visualization queries against the Virtual Graph.

For each laundering shape (collection account, spray account, round-trip pair) this
finds a real flagged account, then runs the matching anchored ego-network query and
reports how many rows come back, confirming each visualization renders small and fast.

The 7-day cutoff is derived from the dataset's max transfer timestamp, the same way
the fraud demo computes ``$since`` for Queries 5 and 6.

Reads the project ``.env`` at the repository root by default. Set ``PROBE_ENV`` to
point at another dotenv.

    uv run vg-viz
    uv run vg-viz --help
"""

from __future__ import annotations

import argparse
import sys
import time

from neo4j import Driver, GraphDatabase, Record
from neo4j.exceptions import DriverError, Neo4jError

from connection import load_connection
from helpers import data_max_dates, first_row, query_error, since_param
from queries import QUERIES

# Query 5 (fan-in) carries the 7-day transfer window the finders and stars share.
_WINDOW_QUERY = next(q for q in QUERIES if q.number == 5)


def timed(driver: Driver, label: str, cypher: str, **params: object) -> list[Record]:
    """Run one query, print its wall-clock time and row count, return the records."""
    t0 = time.perf_counter()
    recs, _, _ = driver.execute_query(cypher, **params)
    print(f"  {label}: {time.perf_counter() - t0:.1f}s, {len(recs)} rows")
    return recs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vg-viz", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    return parser.parse_args()


def cutoff(driver: Driver) -> str:
    """Return the 7-day cutoff as an ISO-8601 UTC string for ``datetime($since)``."""
    max_transfer, max_opened = data_max_dates(driver)
    since = since_param(_WINDOW_QUERY, max_transfer, max_opened)
    return since.isoformat().replace("+00:00", "Z")


def main() -> None:
    parse_args()
    uri, auth = load_connection()

    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        print(f"connected: {uri}")
        try:
            run_checks(driver)
        except (Neo4jError, DriverError) as exc:
            sys.exit(f"ERROR: {query_error(exc, indent='')}")


def run_checks(driver: Driver) -> None:
    """Find one anchor per laundering shape, then run its anchored visualization."""
    since = cutoff(driver)
    print(f"  7-day window: transfer_timestamp >= {since}")

    # Anchor 1: recipient with the most distinct senders in the 7-day window.
    recs = timed(
        driver,
        "find collection account",
        "MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account) "
        "WHERE t.transfer_timestamp >= datetime($since) "
        "WITH dst.account_id AS recipient, "
        "count(DISTINCT src.account_id) AS senders "
        "RETURN recipient, senders ORDER BY senders DESC, recipient ASC LIMIT 5",
        since=since,
    )
    top = first_row(recs, "the collection-account finder")
    coll_id = top["recipient"]
    print(f"    -> account {coll_id} with {top['senders']} distinct senders")

    # Anchor 2: sender with the most distinct recipients in the 7-day window.
    recs = timed(
        driver,
        "find spray account",
        "MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account) "
        "WHERE t.transfer_timestamp >= datetime($since) "
        "WITH src.account_id AS sender, "
        "count(DISTINCT dst.account_id) AS recipients "
        "RETURN sender, recipients ORDER BY recipients DESC, sender ASC LIMIT 5",
        since=since,
    )
    top = first_row(recs, "the spray-account finder")
    spray_id = top["sender"]
    print(f"    -> account {spray_id} with {top['recipients']} "
          "distinct recipients")

    # Anchor 3: highest-volume reciprocal round-trip pair. Same form as Query 3: the
    # pattern binds one row per (f, g) combination, so each direction's sum is
    # divided by the other direction's leg count. The legs are counted on the scalar
    # link_id, since count(DISTINCT <relationship>) keeps the GROUP BY out of the SQL.
    recs = timed(
        driver,
        "find round-trip pair",
        "MATCH (a:Account)-[f:TRANSFERRED_TO]->(b:Account)-[g:TRANSFERRED_TO]->(a) "
        "WHERE a.account_id < b.account_id "
        "WITH a.account_id AS a_id, b.account_id AS b_id, "
        "count(DISTINCT f.link_id) AS n_ab, count(DISTINCT g.link_id) AS n_ba, "
        "sum(f.amount) AS sf, sum(g.amount) AS sg "
        "RETURN a_id, b_id, round(sf / n_ba + sg / n_ab, 2) AS vol, "
        "n_ab + n_ba AS legs "
        "ORDER BY vol DESC, a_id ASC, b_id ASC LIMIT 5",
    )
    top = first_row(recs, "the round-trip finder")
    a_id, b_id = top["a_id"], top["b_id"]
    print(f"    -> pair {a_id} <-> {b_id}, round-trip volume {top['vol']}")

    print("--- visualizations ---")

    # The two star pictures use the same 7-day window as the finders (and as
    # Queries 5 and 6), so they draw the transfers that flagged the anchor.
    timed(
        driver,
        f"VIZ fan-in star @ {coll_id}",
        "MATCH (sender:Account)-[t:TRANSFERRED_TO]->(a:Account {account_id: $id}) "
        "WHERE t.transfer_timestamp >= datetime($since) "
        "RETURN sender, t, a LIMIT 50",
        id=coll_id,
        since=since,
    )
    timed(
        driver,
        f"VIZ fan-out star @ {spray_id}",
        "MATCH (a:Account {account_id: $id})"
        "-[t:TRANSFERRED_TO]->(recipient:Account) "
        "WHERE t.transfer_timestamp >= datetime($since) "
        "RETURN a, t, recipient LIMIT 50",
        id=spray_id,
        since=since,
    )
    timed(
        driver,
        f"VIZ round-trip @ {a_id} <-> {b_id}",
        "MATCH (a:Account {account_id: $a})"
        "-[t:TRANSFERRED_TO]-(b:Account {account_id: $b}) "
        "RETURN a, t, b",
        a=a_id,
        b=b_id,
    )


if __name__ == "__main__":
    main()
