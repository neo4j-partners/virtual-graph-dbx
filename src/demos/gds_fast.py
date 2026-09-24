"""Fast-gds demo (``--demo fast-gds``).

The working GDS Session + PageRank path over a small, recent window of the Account
transfer network, provisioned via the Cypher-projection form of
``gds.graph.project(...)``. See ``gds-guide.md``.
"""

from __future__ import annotations

import argparse
import datetime as dt

from neo4j import GraphDatabase

from connection import load_connection
from demos.gds_common import (
    DROP_GRAPH,
    new_graph_name,
    project_with_cleanup,
    run_statement,
)
from helpers import data_max_dates

COUNT_WINDOW = """
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
RETURN count(t) AS edges
"""

PROJECT_WINDOW = """
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
RETURN gds.graph.project(
  $graph,
  src,
  dst,
  {
    sourceNodeLabels: labels(src),
    targetNodeLabels: labels(dst),
    relationshipType: type(t)
  },
  { memory: $memory }
) AS result
"""

PAGERANK_STREAM = """
CALL gds.pageRank.stream($graph)
YIELD nodeId, score
RETURN nodeId, score
ORDER BY score DESC
LIMIT $limit
"""

# A streamed GDS ``nodeId`` packs the source ``account_id`` into its low 50 bits,
# shifted left by one.
_NODE_ID_LOW_BITS = (1 << 50) - 1
_DEFAULT_GRAPH = "account_transfers_recent"


def decode_account_id(node_id: int) -> int:
    """Decode a streamed GDS ``nodeId`` back to the Virtual Graph ``account_id``."""
    return (node_id & _NODE_ID_LOW_BITS) >> 1


def run_gds(args: argparse.Namespace) -> bool:
    """Provision a GDS Session over a windowed transfer subgraph and stream PageRank."""
    uri, auth = load_connection()

    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()

        # Step 0: find the window cutoff from the dataset's max transfer timestamp
        # (a cheap max() scan). The data ends in the past, so anchor to its max, not
        # now. --since-hours, when set, overrides --since-days for a thin sub-day
        # slice. The cutoff is computed here and passed as $since, so the WHERE stays a
        # plain `>= $since` comparison (the Virtual Graph cannot do temporal
        # arithmetic).
        if args.since_hours is not None:
            window = dt.timedelta(hours=args.since_hours)
            window_label = f"last {args.since_hours}h"
        else:
            window = dt.timedelta(days=args.since_days)
            window_label = f"last {args.since_days}d"
        max_transfer, _ = data_max_dates(driver)
        since = max_transfer.to_native() - window
        graph = args.graph if args.graph is not None else new_graph_name(_DEFAULT_GRAPH)
        print(f"Connected. Window: {window_label}, "
              f"transfer_timestamp >= {since} (data max {max_transfer.to_native()}).")
        print(f"Target graph '{graph}', memory={args.memory}.")

        # Step 1: cheap sizing query, no session. See how big the projection will be.
        sized = run_statement(driver, f"size window (count edges, {window_label})",
                              COUNT_WINDOW, {"since": since})
        if sized is None:
            return False
        edges = sized[0]["edges"]
        print(f"  -> {edges} TRANSFERRED_TO edge(s) in the window will be projected.")
        if edges == 0:
            print("\nWindow is empty; widen --since-hours/--since-days. "
                  "Not provisioning a session.")
            return True
        if args.count_only:
            print("\n--count-only set; sized the window without provisioning "
                  "a session.")
            return True

        # An explicitly named graph retains the old replace-on-run behavior. Default
        # runs use distinct names so a failed Aura session cannot block the next run.
        if args.graph is not None:
            dropped = run_statement(driver, f"drop stale projection '{graph}'",
                                    DROP_GRAPH, {"graph": graph})
            if dropped is None:
                return False

        # Projection provisions the GDS session. A session startup race can leave a
        # graph mapping behind even when the projection failed, so every failure drops
        # the name it used. Default runs retry once under a new name on the observed
        # session/mapping failures.
        projected, graph = project_with_cleanup(
            driver,
            "project from the window (provisions the session)",
            PROJECT_WINDOW,
            {"memory": args.memory, "since": since},
            graph,
            retry_prefix=_DEFAULT_GRAPH if args.graph is None else None,
        )
        if projected is None:
            print("\nProjection failed; cannot run PageRank. See the error above.")
            return False
        for row in projected:
            print(f"  {row['result']}")

        # Step 3: stream PageRank. No write-back exists on a Virtual Graph, so results
        #         come back to the app rather than being written to nodes.
        ranked = None
        dropped = None
        try:
            ranked = run_statement(
                driver,
                f"PageRank stream (top {args.limit})",
                PAGERANK_STREAM,
                {"graph": graph, "limit": args.limit},
            )
            if ranked:
                print(f"\n  Top {len(ranked)} accounts by PageRank (recent window):")
                print("  nodeId                account_id  score")
                print("  --------------------  ----------  ----------")
                for row in ranked:
                    account_id = decode_account_id(row["nodeId"])
                    print(f"  {row['nodeId']!s:<20}  {account_id!s:<10}  "
                          f"{row['score']:.6f}")
        finally:
            # Keep a successful graph only when requested. Clean up failed streams.
            if args.keep and ranked is not None:
                print(f"\n--keep set; leaving '{graph}' in place.")
            else:
                dropped = run_statement(driver, f"drop projection '{graph}'",
                                        DROP_GRAPH, {"graph": graph})
        return ranked is not None and (args.keep or dropped is not None)
