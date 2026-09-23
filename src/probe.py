"""Single-query probe for the Virtual Graph.

Runs one Cypher statement with ``driver.execute_query``, measures wall-clock time,
and prints the result. Pass the Cypher as the single positional argument.

There is no client timeout of its own, but ``execute_query`` retries a read that hits
the 60s Bolt read timeout (the server's ``connection.recv_timeout_seconds`` hint), so a
query that runs longer than a minute is resubmitted and piles up on the warehouse.
Keep this for quick checks and run heavy queries (for example the unanchored Query
10) through ``vg-demo``.

    uv run vg-probe "RETURN 1 AS ok"
    uv run vg-probe --help

Reads the project ``.env`` at the repository root by default; set ``PROBE_ENV`` to
point at another dotenv.
"""

from __future__ import annotations

import argparse
import time

from neo4j import GraphDatabase

from connection import load_connection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vg-probe", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cypher", help="the Cypher statement to run")
    return parser.parse_args()


def main() -> None:
    cypher = parse_args().cypher
    uri, auth = load_connection()

    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        print(f"connected: {uri}", flush=True)
        t0 = time.perf_counter()
        recs, _, _ = driver.execute_query(cypher)
        elapsed = time.perf_counter() - t0
        sample = recs[0].data() if recs else None
        print(f"OK {elapsed:.1f}s rows={len(recs)} sample={sample}", flush=True)


if __name__ == "__main__":
    main()
