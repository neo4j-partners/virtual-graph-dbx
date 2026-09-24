"""Run the Finance Genie Virtual Graph demos.

A single entry point with five demos, selected with ``--demo``:

* ``--demo fraud`` (default): fraud-signal queries 1-10 from ``finding-fraud.md``.
  Databricks runs each GROUP BY, and the Neo4j engine applies each threshold, orders
  and limits. "Recent" windows are passed as a precomputed ``$since`` parameter.
  Query 11 (layering cycles) is documented but unsupported on the Virtual Graph, so
  it is never run. See ``queries.py``.
* ``--demo basic``: the warm-up exploration and visualization queries from
  ``basic-graph-examples.md``. Simple counts and small, anchored traversals show the
  value of the relationships without any fraud logic.
* ``--demo fast-gds``: the working GDS Session + PageRank path over a small, recent
  window of the Account transfer network, provisioned via the Cypher-projection form
  of ``gds.graph.project(...)``. See ``gds-guide.md``.
* ``--demo gds-probe``: sweep projections that add node and relationship properties
  one at a time on the recent window (default 7 days), to find which property configs
  the projection accepts. See ``src/demos/gds_probe.py``.
* ``--demo 100m``: the SQL-side "Zero spill from 100K to 100M rows" spike.
  Talks SQL straight to the backing warehouse via the Databricks SDK (not Bolt). It
  runs the C1/C2/C3 aggregation SQL the Virtual Graph pushes down to, then polls
  warehouse query history to confirm ``spill_to_disk_bytes = 0``. History can lag up
  to a few minutes, and a countdown shows progress. Needs the history extra
  (``uv sync --extra history``). See ``src/demos/sql_spike.py``.

Connection details come from the project ``.env`` at the repository root (NEO4J_URI,
NEO4J_USERNAME, NEO4J_PASSWORD), which points at the Aura Virtual Graph engine.

Every demo exits 1 when a query, projection or statement fails, and argparse exits 2
on a bad flag.

Usage:
    uv run vg-demo                    # fraud demo: queries 1-10
    uv run vg-demo --query 5          # run a single fraud query by number
    uv run vg-demo --only 5 6         # run a subset, in this order
    uv run vg-demo --rows 5           # cap printed rows per query
    uv run vg-demo --timeout 60       # per-query server timeout in s (default 300)

    uv run vg-demo --demo basic       # basic exploration and visualization queries

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
    # run the C-queries only and skip the history wait (up to a few minutes)
    uv run vg-demo --demo 100m --skip-history
    # rebuild the full 100K-to-100M ramp first (destructive)
    uv run vg-demo --demo 100m --build
    # build only these ramp sizes
    uv run vg-demo --demo 100m --build --sizes 1000000 100000000
"""

from __future__ import annotations

import argparse
import sys

from neo4j import GraphDatabase

from connection import load_connection
from demos.basic import run_basic
from demos.fraud import run_fraud, select_queries
from demos.gds_fast import run_gds
from demos.gds_probe import run_probe
from demos.sql_spike import run_spike


def _parse(text: str, kind: type[int | float]) -> int | float:
    """Parse ``text`` as ``kind``, raising the argparse error type on failure."""
    try:
        return kind(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid {kind.__name__} value: {text!r}") from None


def positive_int(text: str) -> int:
    """argparse type: an integer >= 1."""
    value = _parse(text, int)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text}")
    return value


def non_negative_int(text: str) -> int:
    """argparse type: an integer >= 0."""
    value = _parse(text, int)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text}")
    return value


def positive_float(text: str) -> float:
    """argparse type: a finite float > 0."""
    value = _parse(text, float)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"must be a positive number, got {text}")
    return value


_GDS = ("fast-gds", "gds-probe")
_SPIKE = ("100m",)
# The demos that read each flag. Setting a flag for any other demo has no effect.
FLAG_DEMOS: dict[str, tuple[str, ...]] = {
    "query": ("fraud",), "only": ("fraud",),
    "rows": ("fraud", "basic"), "timeout": ("fraud", "basic"),
    "graph": _GDS, "since_days": _GDS, "since_hours": _GDS, "memory": _GDS,
    "count_only": _GDS, "limit": ("fast-gds",), "keep": ("fast-gds",),
    "profile": _SPIKE, "warehouse": _SPIKE, "build": _SPIKE, "sizes": _SPIKE,
    "history_lag_minutes": _SPIKE, "poll_minutes": _SPIKE,
    "max_wait_minutes": _SPIKE, "skip_history": _SPIKE,
}


def ignored_flags(parser: argparse.ArgumentParser,
                  args: argparse.Namespace) -> list[str]:
    """Return a warning for each flag that was set but the chosen demo never reads."""
    warnings = []
    for dest, demos in FLAG_DEMOS.items():
        if args.demo not in demos and getattr(args, dest) != parser.get_default(dest):
            flag = "--" + dest.replace("_", "-")
            warnings.append(f"{flag} has no effect with --demo {args.demo}")
    if args.demo == "100m" and args.sizes is not None and not args.build:
        warnings.append("--sizes has no effect without --build")
    return warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo",
                        choices=("fraud", "basic", "fast-gds", "gds-probe", "100m"),
                        default="fraud", help="which demo to run (default: fraud)")

    fraud = parser.add_argument_group("fraud demo")
    pick = fraud.add_mutually_exclusive_group()
    pick.add_argument("--query", type=positive_int, metavar="N",
                      help="run only the query with this number")
    pick.add_argument("--only", type=positive_int, nargs="+", metavar="N",
                      help="run only these query numbers, in this order")

    shared = parser.add_argument_group("fraud + basic demos")
    shared.add_argument("--rows", type=non_negative_int, default=10, metavar="N",
                        help="maximum rows to print per query (default: 10). With 0, "
                             "only the match count is printed")
    shared.add_argument("--timeout", type=positive_float, default=300.0,
                        metavar="SECONDS",
                        help="per-query server-side timeout (default: 300)")

    gds = parser.add_argument_group("gds demos (fast-gds / gds-probe)")
    gds.add_argument("--graph", default=None, metavar="NAME",
                     help="projection name (fast-gds uses a unique name by default, "
                          "gds-probe defaults to account_transfers_recent)")
    gds.add_argument("--since-days", type=positive_int, default=7, metavar="N",
                     help="window size: project transfers from the last N days of data "
                          "(default: 7)")
    gds.add_argument("--since-hours", type=positive_float, default=None, metavar="H",
                     help="window size in hours. Overrides --since-days for a thin "
                          "recent slice, for example --since-hours 2 projects ~300 "
                          "edges")
    gds.add_argument("--memory", default="2GB", metavar="SIZE",
                     help="session instance size that provisions the GDS Session "
                          "(default: 2GB)")
    gds.add_argument("--limit", type=positive_int, default=10, metavar="N",
                     help="fast-gds only: number of top-ranked nodes to stream "
                          "(default: 10)")
    gds.add_argument("--count-only", action="store_true",
                     help="only run the sizing count for the window and never "
                          "provision a session")
    gds.add_argument("--keep", action="store_true",
                     help="fast-gds only: skip the final drop so the projection and "
                          "session can be reused")

    spike = parser.add_argument_group("100m demo")
    spike.add_argument("--profile", default=None, metavar="NAME",
                       help="Databricks config profile (default: "
                            "DATABRICKS_CONFIG_PROFILE, then DATABRICKS_PROFILE, from "
                            ".env, else DEFAULT)")
    spike.add_argument("--warehouse", default=None, metavar="ID",
                       help="SQL warehouse id to run on (default: "
                            "VG_BACKING_WAREHOUSE_ID from .env, else the backing VG "
                            "warehouse)")
    spike.add_argument("--build", action="store_true",
                       help="rebuild account_links_large at each ramp size first "
                            "(destructive CREATE OR REPLACE). Without it, query the "
                            "existing table")
    spike.add_argument("--sizes", type=positive_int, nargs="+", default=None,
                       metavar="N",
                       help="ramp row counts for --build (default: 100K, 250K, 500K, "
                            "1M, 10M, 50M, 100M)")
    spike.add_argument("--history-lag-minutes", type=positive_float, default=3.0,
                       metavar="M",
                       help="estimated query-history lag, used for the countdown "
                            "(default: 3)")
    spike.add_argument("--poll-minutes", type=positive_float, default=3.0, metavar="M",
                       help="how often to poll history while waiting for spill "
                            "(default: 3)")
    spike.add_argument("--max-wait-minutes", type=positive_float, default=40.0,
                       metavar="M",
                       help="give up polling history after this long (default: 40)")
    spike.add_argument("--skip-history", action="store_true",
                       help="run the C-queries but do not wait for the history lag. "
                            "Spill is left unconfirmed")
    return parser


def parse_args() -> argparse.Namespace:
    """Parse the command line and warn about flags the chosen demo ignores."""
    parser = build_parser()
    args = parser.parse_args()
    for warning in ignored_flags(parser, args):
        print(f"warning: {warning}", file=sys.stderr)
    return args


def run_demo(args: argparse.Namespace) -> bool:
    """Dispatch to the selected demo and return whether it succeeded."""
    # The GDS demos manage their own connection and stream rather than running the
    # shared query loop.
    if args.demo == "fast-gds":
        return run_gds(args)
    if args.demo == "gds-probe":
        args.graph = args.graph or "account_transfers_recent"
        return run_probe(args)
    # The 100m demo talks SQL to the warehouse via the Databricks SDK, not Bolt, so it
    # manages its own connection and never opens the Neo4j driver.
    if args.demo == "100m":
        return run_spike(args)

    # Resolve the fraud query numbers before connecting, so a bad number fails fast.
    selected = select_queries(args) if args.demo == "fraud" else None
    uri, auth = load_connection()
    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        if args.demo == "basic":
            print("Connected. Running basic exploration and visualization queries.")
            return run_basic(driver, args.rows, args.timeout)
        return run_fraud(driver, args, selected)


def main() -> None:
    args = parse_args()
    succeeded = run_demo(args)
    print(f"\n{'=' * 78}\n{'Done.' if succeeded else 'Done, with failures.'}")
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
