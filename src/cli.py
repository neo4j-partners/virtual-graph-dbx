"""Run the Finance Genie Virtual Graph demos.

A single entry point with four demos, selected with ``--demo``:

* ``--demo fraud`` (default) — the fast, pushdown-friendly fraud-signal queries from
  ``finding-fraud.md``. The server aggregates and orders; threshold (HAVING)
  filters run here in Python; "recent" windows are passed as a precomputed ``$since``
  parameter; fan-in/fan-out reshape a ``count(DISTINCT)`` into pair-grouping plus a
  client-side rollup. ``--all`` also attempts the heavier signals that have no
  pushdown-friendly form (included to show where those patterns reach the engine's
  current limits). See ``queries.py``.
* ``--demo basic`` — the warm-up exploration / visualization queries from
  ``basic-graph-examples.md``: simple counts and small, anchored traversals that
  show the value of the relationships without any fraud logic.
* ``--demo fast-gds`` — the working GDS Session + PageRank path over a small, recent
  window of the Account transfer network, provisioned via the Cypher-projection form
  of ``gds.graph.project(...)``. See ``gds-guide.md``.
* ``--demo slow-gds`` — the GDS forms to steer clear of, shown deliberately: the classic
  ``CALL gds.graph.project('g', 'Account', ...)`` form (returns ``42NG0``) and a
  large-window projection (exceeds the 60s Bolt read timeout).
* ``--demo gds-probe`` — sweep projections that add node / relationship properties one
  at a time on a thin window, to isolate which property configs the projection rejects.
  See ``src/demos/gds_probe.py``.
* ``--demo timezone`` — reproduce the per-row ``current_timezone()`` round trip: run the
  five discriminating queries (``LIMIT 25`` each) and show TIMESTAMP-bearing
  results are slow while scalar / DATE results are fast. With the Databricks SDK
  installed and a warehouse configured, also pull query history and count the actual
  ``current_timezone()`` statements per run. See ``src/demos/timezone.py``.
* ``--demo 100m`` — the SQL-side "Zero spill from 100K to 100M rows" spike.
  Talks SQL straight to the backing warehouse via the Databricks
  SDK (not Bolt): runs the C1/C2/C3 aggregation SQL the Virtual Graph pushes down to,
  then polls warehouse query history (after its 11-25 min lag, with a countdown) to
  confirm ``spill_to_disk_bytes = 0``. Needs the history extra (``uv sync --extra
  history``). See ``src/demos/sql_spike.py``.

Connection details come from the project ``.env`` at the repository root (NEO4J_URI,
NEO4J_USERNAME, NEO4J_PASSWORD), which points at the Aura Virtual Graph engine.

Usage:
    uv run vg-demo                          # fraud demo: every fast query
    uv run vg-demo --all                    # also attempt the slow / unsupported queries
    uv run vg-demo --query 5                # run a single fraud query by number
    uv run vg-demo --only 5 6               # run a subset, in this order
    uv run vg-demo --rows 5                 # cap printed rows per query
    uv run vg-demo --timeout 60             # per-query server timeout in seconds (default 120)

    uv run vg-demo --demo basic             # basic exploration / visualization queries

    uv run vg-demo --demo fast-gds          # 7-day window: size, project, stream top 10, drop
    uv run vg-demo --demo fast-gds --since-hours 2   # thin sub-day slice (~a couple hundred edges)
    uv run vg-demo --demo fast-gds --count-only      # only size the window; no session
    uv run vg-demo --demo fast-gds --limit 25        # stream the top 25
    uv run vg-demo --demo fast-gds --keep            # leave the projection/session in place

    uv run vg-demo --demo slow-gds          # demonstrate the GDS forms that fail

    uv run vg-demo --demo gds-probe --since-hours 2   # sweep property projections to isolate the edge-case bug

    uv run vg-demo --demo timezone          # per-row current_timezone() round trip: timing + history count
    uv run vg-demo --demo timezone --no-history      # wall-clock contrast only (no Databricks SDK needed)
    uv run vg-demo --demo timezone --history-wait 45 # wait longer for query history to ingest before counting

    uv run vg-demo --demo 100m              # query the existing table, then confirm zero spill from history
    uv run vg-demo --demo 100m --skip-history        # run the C-queries only; skip the ~15min history wait
    uv run vg-demo --demo 100m --build               # rebuild the full 100K-to-100M ramp first (destructive)
    uv run vg-demo --demo 100m --build --sizes 1000000 100000000   # build only these ramp sizes
"""

from __future__ import annotations

import argparse

from neo4j import GraphDatabase

from connection import load_connection
from demos.basic import run_basic
from demos.fraud import run_fraud
from demos.gds_common import override_bolt_read_timeout
from demos.gds_fast import run_gds
from demos.gds_probe import run_probe
from demos.gds_slow import run_slow_gds
from demos.sql_spike import run_spike
from demos.timezone import run_timezone


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo",
                        choices=("fraud", "basic", "fast-gds", "slow-gds", "gds-probe",
                                 "timezone", "100m"),
                        default="fraud", help="which demo to run (default: fraud)")

    fraud = parser.add_argument_group("fraud demo")
    fraud.add_argument("--all", action="store_true",
                       help="also attempt the slow / unsupported fraud queries (guarded)")
    fraud.add_argument("--query", type=int, metavar="N",
                       help="run only the query with this number")
    fraud.add_argument("--only", type=int, nargs="+", metavar="N",
                       help="run only these query numbers, in this order")

    shared = parser.add_argument_group("fraud + basic demos")
    shared.add_argument("--rows", type=int, default=10, metavar="N",
                        help="maximum rows to print per query (default: 10)")
    shared.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS",
                        help="per-query server-side timeout (default: 120)")

    gds = parser.add_argument_group("gds demos (fast-gds / slow-gds)")
    gds.add_argument("--graph", default="account_transfers_recent", metavar="NAME",
                     help="projection / in-memory graph name (default: account_transfers_recent)")
    gds.add_argument("--since-days", type=int, default=7, metavar="N",
                     help="window size: project transfers from the last N days of data (default: 7)")
    gds.add_argument("--since-hours", type=float, default=None, metavar="H",
                     help="window size in hours; overrides --since-days for a thin recent "
                          "slice (e.g. --since-hours 2 projects ~a couple hundred edges)")
    gds.add_argument("--memory", default="2GB", metavar="SIZE",
                     help="session instance size that provisions the GDS Session (default: 2GB)")
    gds.add_argument("--limit", type=int, default=10, metavar="N",
                     help="number of top-ranked nodes to stream (default: 10)")
    gds.add_argument("--count-only", action="store_true",
                     help="only run the sizing count for the window; never provision a session")
    gds.add_argument("--keep", action="store_true",
                     help="skip the final drop so the projection/session can be reused")
    gds.add_argument("--read-timeout", type=float, default=None, metavar="SECONDS",
                     help="override the 60s Bolt read timeout Aura pins, to get past the "
                          "client-side trip on a long, silent provisioning. 0 disables it "
                          "entirely. Necessary but not sufficient: the server can still reset "
                          "the connection on long provisions. Unsupported; unset keeps 60s")
    gds.add_argument("--probe-read-timeout", type=float, default=300.0, metavar="SECONDS",
                     help="gds-probe only: default Bolt read-timeout clamp so a known-good "
                          "~130s provisioning is not aborted by the 60s client trip while the "
                          "sweep measures property failures (default: 300; --read-timeout wins)")

    tz = parser.add_argument_group("timezone demo")
    tz.add_argument("--no-history", action="store_true",
                    help="skip the Databricks query-history count; report wall-clock only")
    tz.add_argument("--history-wait", type=float, default=30.0, metavar="SECONDS",
                    help="seconds to wait for query history to ingest before counting "
                         "the current_timezone() statements (default: 30)")

    spike = parser.add_argument_group("100m demo")
    spike.add_argument("--profile", default=None, metavar="NAME",
                       help="Databricks CLI profile (default: DATABRICKS_CONFIG_PROFILE "
                            "from .env)")
    spike.add_argument("--warehouse", default=None, metavar="ID",
                       help="SQL warehouse id to run on (default: the backing VG warehouse)")
    spike.add_argument("--build", action="store_true",
                       help="rebuild account_links_large at each ramp size first "
                            "(destructive CREATE OR REPLACE); without it, query the existing table")
    spike.add_argument("--sizes", type=int, nargs="+", default=None, metavar="N",
                       help="ramp row counts for --build (default: 100K, 250K, 500K, 1M, "
                            "10M, 50M, 100M)")
    spike.add_argument("--history-lag-minutes", type=float, default=15.0, metavar="M",
                       help="estimated query-history lag, used for the countdown (default: 15)")
    spike.add_argument("--poll-minutes", type=float, default=3.0, metavar="M",
                       help="how often to poll history while waiting for spill (default: 3)")
    spike.add_argument("--max-wait-minutes", type=float, default=40.0, metavar="M",
                       help="give up polling history after this long (default: 40)")
    spike.add_argument("--skip-history", action="store_true",
                       help="run the C-queries but do not wait for the history lag; "
                            "spill is left unconfirmed")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # The GDS demos manage their own connection: the Bolt read-timeout override must be
    # installed before the driver opens, and they stream rather than running the shared
    # query loop.
    if args.demo == "fast-gds":
        run_gds(args)
        print(f"\n{'=' * 78}\nDone.")
        return
    if args.demo == "slow-gds":
        run_slow_gds(args)
        print(f"\n{'=' * 78}\nDone.")
        return
    if args.demo == "gds-probe":
        run_probe(args)
        print(f"\n{'=' * 78}\nDone.")
        return
    # The 100m demo talks SQL to the warehouse via the Databricks SDK, not Bolt, so it
    # manages its own connection and never opens the Neo4j driver.
    if args.demo == "100m":
        run_spike(args)
        print(f"\n{'=' * 78}\nDone.")
        return

    # The fraud --all set includes slow / unsupported queries. The server's 60s Bolt
    # read timeout (left in place) is what trips a silent slow query; --read-timeout can
    # raise or disable it. Install before the driver opens. The fast queries finish well
    # under 60s, so this only affects the slow ones.
    if args.demo == "fraud" and args.all and args.read_timeout is not None:
        seconds = None if args.read_timeout == 0 else args.read_timeout
        override_bolt_read_timeout(seconds)
        shown = "disabled (no timeout)" if seconds is None else f"{seconds:g}s"
        print(f"--all: Bolt read timeout overridden to {shown} for the slow queries.")

    uri, auth = load_connection()
    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        if args.demo == "basic":
            print("Connected. Running basic exploration / visualization queries.")
            run_basic(driver, args.rows, args.timeout)
        elif args.demo == "timezone":
            print("Connected. Running the timezone round-trip demo.")
            run_timezone(driver, args)
        else:
            run_fraud(driver, args)
    print(f"\n{'=' * 78}\nDone.")


if __name__ == "__main__":
    main()
