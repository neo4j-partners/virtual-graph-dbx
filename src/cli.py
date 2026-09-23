"""Run the Finance Genie Virtual Graph demos.

A single entry point with five demos, selected with ``--demo``:

* ``--demo fraud`` (default) — fraud-signal queries 1-10 from ``finding-fraud.md``.
  The server aggregates, applies each threshold, orders and limits; "recent" windows
  are passed as a precomputed ``$since`` parameter. Query 11 (layering cycles) is
  documented but unsupported on the Virtual Graph, so it is never run. See
  ``queries.py``.
* ``--demo basic`` — the warm-up exploration / visualization queries from
  ``basic-graph-examples.md``: simple counts and small, anchored traversals that
  show the value of the relationships without any fraud logic.
* ``--demo fast-gds`` — the working GDS Session + PageRank path over a small, recent
  window of the Account transfer network, provisioned via the Cypher-projection form
  of ``gds.graph.project(...)``. See ``gds-guide.md``.
* ``--demo gds-probe`` — sweep projections that add node / relationship properties one
  at a time on the recent window (default 7 days), to find which property configs the
  projection accepts.
  See ``src/demos/gds_probe.py``.
* ``--demo 100m`` — the SQL-side "Zero spill from 100K to 100M rows" spike.
  Talks SQL straight to the backing warehouse via the Databricks SDK (not Bolt): runs
  the C1/C2/C3 aggregation SQL the Virtual Graph pushes down to, then polls warehouse
  query history (which can lag up to a few minutes; a countdown shows progress) to
  confirm ``spill_to_disk_bytes = 0``. Needs the history extra (``uv sync --extra
  history``). See ``src/demos/sql_spike.py``.

Connection details come from the project ``.env`` at the repository root (NEO4J_URI,
NEO4J_USERNAME, NEO4J_PASSWORD), which points at the Aura Virtual Graph engine.

Usage:
    uv run vg-demo                    # fraud demo: queries 1-10
    uv run vg-demo --query 5          # run a single fraud query by number
    uv run vg-demo --only 5 6         # run a subset, in this order
    uv run vg-demo --rows 5           # cap printed rows per query
    uv run vg-demo --timeout 60       # per-query server timeout in s (default 300)

    uv run vg-demo --demo basic       # basic exploration / visualization queries

    # 7-day window: size, project, stream top 10, drop
    uv run vg-demo --demo fast-gds
    uv run vg-demo --demo fast-gds --since-hours 2  # sub-day slice (~300 edges)
    uv run vg-demo --demo fast-gds --count-only     # only size the window
    uv run vg-demo --demo fast-gds --limit 25       # stream the top 25
    uv run vg-demo --demo fast-gds --keep           # leave the projection in place

    # sweep property projections one at a time (7-day window, a few minutes)
    uv run vg-demo --demo gds-probe

    # query the existing table, then confirm zero spill from history
    uv run vg-demo --demo 100m
    # run the C-queries only; skip the history wait (up to a few minutes)
    uv run vg-demo --demo 100m --skip-history
    # rebuild the full 100K-to-100M ramp first (destructive)
    uv run vg-demo --demo 100m --build
    # build only these ramp sizes
    uv run vg-demo --demo 100m --build --sizes 1000000 100000000
"""

from __future__ import annotations

import argparse

from neo4j import GraphDatabase

from connection import load_connection
from demos.basic import run_basic
from demos.fraud import run_fraud
from demos.gds_fast import run_gds
from demos.gds_probe import run_probe
from demos.sql_spike import run_spike


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo",
                        choices=("fraud", "basic", "fast-gds", "gds-probe", "100m"),
                        default="fraud", help="which demo to run (default: fraud)")

    fraud = parser.add_argument_group("fraud demo")
    fraud.add_argument("--query", type=int, metavar="N",
                       help="run only the query with this number")
    fraud.add_argument("--only", type=int, nargs="+", metavar="N",
                       help="run only these query numbers, in this order")

    shared = parser.add_argument_group("fraud + basic demos")
    shared.add_argument("--rows", type=int, default=10, metavar="N",
                        help="maximum rows to print per query (default: 10)")
    shared.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS",
                        help="per-query server-side timeout (default: 300)")

    gds = parser.add_argument_group("gds demos (fast-gds / gds-probe)")
    gds.add_argument("--graph", default=None, metavar="NAME",
                     help="projection name (fast-gds uses a unique name by default; "
                          "gds-probe defaults to account_transfers_recent)")
    gds.add_argument("--since-days", type=int, default=7, metavar="N",
                     help="window size: project transfers from the last N days of data "
                          "(default: 7)")
    gds.add_argument("--since-hours", type=float, default=None, metavar="H",
                     help="window size in hours; overrides --since-days for a thin "
                          "recent slice (e.g. --since-hours 2 projects ~300 edges)")
    gds.add_argument("--memory", default="2GB", metavar="SIZE",
                     help="session instance size that provisions the GDS Session "
                          "(default: 2GB)")
    gds.add_argument("--limit", type=int, default=10, metavar="N",
                     help="number of top-ranked nodes to stream (default: 10)")
    gds.add_argument("--count-only", action="store_true",
                     help="only run the sizing count for the window; never provision "
                          "a session")
    gds.add_argument("--keep", action="store_true",
                     help="skip the final drop so the projection/session can be reused")

    spike = parser.add_argument_group("100m demo")
    spike.add_argument("--profile", default=None, metavar="NAME",
                       help="Databricks config profile (default: "
                            "DATABRICKS_CONFIG_PROFILE, then DATABRICKS_PROFILE, from "
                            ".env; else DEFAULT)")
    spike.add_argument("--warehouse", default=None, metavar="ID",
                       help="SQL warehouse id to run on (default: the backing VG "
                            "warehouse)")
    spike.add_argument("--build", action="store_true",
                       help="rebuild account_links_large at each ramp size first "
                            "(destructive CREATE OR REPLACE); without it, query the "
                            "existing table")
    spike.add_argument("--sizes", type=int, nargs="+", default=None, metavar="N",
                       help="ramp row counts for --build (default: 100K, 250K, 500K, "
                            "1M, 10M, 50M, 100M)")
    spike.add_argument("--history-lag-minutes", type=float, default=3.0, metavar="M",
                       help="estimated query-history lag, used for the countdown "
                            "(default: 3)")
    spike.add_argument("--poll-minutes", type=float, default=3.0, metavar="M",
                       help="how often to poll history while waiting for spill "
                            "(default: 3)")
    spike.add_argument("--max-wait-minutes", type=float, default=40.0, metavar="M",
                       help="give up polling history after this long (default: 40)")
    spike.add_argument("--skip-history", action="store_true",
                       help="run the C-queries but do not wait for the history lag; "
                            "spill is left unconfirmed")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # The GDS demos manage their own connection and stream rather than running the
    # shared query loop.
    if args.demo == "fast-gds":
        succeeded = run_gds(args)
        print(f"\n{'=' * 78}\nDone.")
        if not succeeded:
            raise SystemExit(1)
        return
    if args.demo == "gds-probe":
        args.graph = args.graph or "account_transfers_recent"
        run_probe(args)
        print(f"\n{'=' * 78}\nDone.")
        return
    # The 100m demo talks SQL to the warehouse via the Databricks SDK, not Bolt, so it
    # manages its own connection and never opens the Neo4j driver.
    if args.demo == "100m":
        run_spike(args)
        print(f"\n{'=' * 78}\nDone.")
        return

    uri, auth = load_connection()
    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        if args.demo == "basic":
            print("Connected. Running basic exploration / visualization queries.")
            run_basic(driver, args.rows, args.timeout)
        else:
            run_fraud(driver, args)
    print(f"\n{'=' * 78}\nDone.")


if __name__ == "__main__":
    main()
