"""GDS examples demo (``--demo gds-examples``).

Projects the whole Account transfer network once, then runs PageRank, WCC and Louvain
on the same GDS Session. Session provisioning is the fixed cost, so each algorithm
after the projection adds only seconds. Sized for the larger graph built by
``vg-generate`` (``src/generate_large.py``), and it also runs on the original data.
"""

from __future__ import annotations

import argparse

from neo4j import GraphDatabase

from connection import load_connection
from demos.gds_common import (
    DROP_GRAPH,
    new_graph_name,
    project_with_cleanup,
    run_statement,
)
from demos.gds_fast import decode_account_id
from helpers import verify_or_exit

COUNT_ALL = """
MATCH (:Account)-[t:TRANSFERRED_TO]->(:Account)
RETURN count(t) AS edges
"""

PROJECT_ALL = """
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
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

WCC_STATS = """
CALL gds.wcc.stats($graph)
YIELD componentCount, componentDistribution
RETURN componentCount, componentDistribution
"""

LOUVAIN_STATS = """
CALL gds.louvain.stats($graph)
YIELD communityCount, modularity, ranLevels, communityDistribution
RETURN communityCount, modularity, ranLevels, communityDistribution
"""

LOUVAIN_TOP = """
CALL gds.louvain.stream($graph)
YIELD nodeId, communityId
RETURN communityId, count(*) AS size
ORDER BY size DESC
LIMIT $limit
"""

_DEFAULT_GRAPH = "account_transfers_all"


def _print_distribution(dist: dict[str, object]) -> None:
    keys = ("min", "p50", "p90", "p99", "max", "mean")
    print("  size " + ", ".join(f"{k}={dist[k]}" for k in keys if k in dist))


def run_algorithms(driver, graph: str, limit: int) -> bool:
    """Run PageRank, WCC and Louvain on ``graph`` and print each result."""
    ok = True

    ranked = run_statement(driver, f"PageRank stream (top {limit})",
                           PAGERANK_STREAM, {"graph": graph, "limit": limit})
    ok &= ranked is not None
    if ranked:
        print("  accountId   score")
        print("  ----------  ----------")
        for row in ranked:
            print(f"  {decode_account_id(row['nodeId'])!s:<10}  {row['score']:.6f}")

    wcc = run_statement(driver, "WCC stats", WCC_STATS, {"graph": graph})
    ok &= wcc is not None
    if wcc:
        print(f"  {wcc[0]['componentCount']:,} connected components")
        _print_distribution(wcc[0]["componentDistribution"])

    louvain = run_statement(driver, "Louvain stats", LOUVAIN_STATS, {"graph": graph})
    ok &= louvain is not None
    if louvain:
        row = louvain[0]
        print(f"  {row['communityCount']:,} communities, "
              f"modularity {row['modularity']:.4f}, {row['ranLevels']} level(s)")
        _print_distribution(row["communityDistribution"])

    top = run_statement(driver, f"Louvain largest {limit} communities",
                        LOUVAIN_TOP, {"graph": graph, "limit": limit})
    ok &= top is not None
    if top:
        print("  communityId           size")
        print("  --------------------  ------")
        for row in top:
            print(f"  {row['communityId']!s:<20}  {row['size']}")
    return ok


def run_examples(args: argparse.Namespace) -> bool:
    """Project every transfer once and run PageRank, WCC and Louvain on it."""
    uri, auth = load_connection()

    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        verify_or_exit(driver, uri)
        graph = args.graph if args.graph is not None else new_graph_name(_DEFAULT_GRAPH)
        print(f"Connected. Target graph '{graph}', memory={args.memory}.")

        sized = run_statement(driver, "size graph (count all transfers)",
                              COUNT_ALL, {})
        if sized is None:
            return False
        edges = sized[0]["edges"]
        print(f"  -> {edges:,} TRANSFERRED_TO edge(s) will be projected.")
        if edges == 0:
            print("\nNo transfers found. Not provisioning a session.")
            return True
        if args.count_only:
            print("\n--count-only set; sized the graph without provisioning "
                  "a session.")
            return True

        if args.graph is not None:
            dropped = run_statement(driver, f"drop stale projection '{graph}'",
                                    DROP_GRAPH, {"graph": graph})
            if dropped is None:
                return False

        projected, graph = project_with_cleanup(
            driver,
            "project all transfers (provisions the session)",
            PROJECT_ALL,
            {"memory": args.memory},
            graph,
            retry_prefix=_DEFAULT_GRAPH if args.graph is None else None,
        )
        if projected is None:
            print("\nProjection failed; cannot run the algorithms. "
                  "See the error above.")
            return False
        for row in projected:
            print(f"  {row['result']}")

        ok = False
        dropped = None
        try:
            ok = run_algorithms(driver, graph, args.limit)
        finally:
            if args.keep and ok:
                print(f"\n--keep set; leaving '{graph}' in place.")
            else:
                dropped = run_statement(driver, f"drop projection '{graph}'",
                                        DROP_GRAPH, {"graph": graph})
        return ok and (args.keep or dropped is not None)
