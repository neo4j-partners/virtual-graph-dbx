"""GDS property-projection demo (``--demo gds-probe``).

GDS accepts numeric graph properties, so the sweep projects the numeric amount and
converts transfer timestamps to epoch milliseconds. It inspects string properties but
skips their projection because a string identifier has no useful numeric equivalent.

Each projection scenario provisions its own session under a unique graph name, so the
sweep takes a few minutes. A failed projection drops the graph it tried to create.
The default 7-day window works (roughly 35-45s per scenario); ``--since-hours`` only
narrows the window.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass

from neo4j import Driver, GraphDatabase
from neo4j.time import Date, DateTime, Duration, Time

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

# Sample one windowed relationship and its endpoints to introspect the property keys
# and types actually present, so the sweep projects real properties and the summary can
# explain a failure in terms of the value type GDS saw.
SAMPLE_ROW = """
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
RETURN properties(t) AS rel, properties(src) AS node
LIMIT 1
"""

# Build the projection around a data-config body assembled per scenario. The body is a
# Cypher map literal whose expressions reference src / dst / t (and any variable the
# scenario's WITH clause adds); both are internal strings, not user input.
PROJECT_TEMPLATE = """
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
{with_clause}
RETURN gds.graph.project(
  $graph,
  src,
  dst,
  {{ {data_config} }},
  {{ memory: $memory }}
) AS result
"""

# After a projection that carries a numeric relationship property, confirm the property
# is real and usable by running PageRank weighted by it.
PAGERANK_WEIGHTED = """
CALL gds.pageRank.stream($graph, {{ relationshipWeightProperty: '{prop}' }})
YIELD nodeId, score
RETURN nodeId, score
ORDER BY score DESC
LIMIT 5
"""

# Epoch milliseconds for the timestamp scenarios. `.epochMillis` on the relationship
# property returns the right values but raises 01N52 (unknown property key
# `epochMillis`). toInteger() on the timestamp pushes down to SQL as epoch seconds with
# no warning, but only as a plain column: inside the config map it is evaluated
# engine-side and rejected (22N38), so it is bound in a WITH first. Every
# account_links timestamp is a whole second, so `* 1000` matches epochMillis exactly.
TIMESTAMP_MS_WITH = (
    "WITH src, dst, t, toInteger(t.transfer_timestamp) * 1000 AS transfer_timestamp_ms"
)

# The label/type-only base every scenario starts from (the known-good fast-gds shape).
BASE_CONFIG = (
    "sourceNodeLabels: labels(src), "
    "targetNodeLabels: labels(dst), "
    "relationshipType: type(t)"
)


@dataclass(frozen=True)
class Scenario:
    """One projection variant in the sweep."""

    key: str
    description: str
    data_config: str | None
    weight_prop: str | None = None  # if set, run a weighted PageRank after projecting
    skip_reason: str | None = None
    with_clause: str = ""  # optional WITH between the WHERE and the projection


def _type_name(value: object) -> str:
    """Readable type label for a sampled property value (numeric vs not)."""
    if isinstance(value, bool):
        return "boolean (non-numeric)"
    if isinstance(value, int):
        return "integer (numeric)"
    if isinstance(value, float):
        return "float (numeric)"
    if isinstance(value, (Date, DateTime, Time, Duration)):
        return f"{type(value).__name__} (temporal, non-numeric)"
    if isinstance(value, str):
        return "string (non-numeric)"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _pick_props(node_props: dict[str, object]) -> tuple[str | None, str | None]:
    """Pick one numeric and one non-numeric node property to probe, if available.

    The numeric pick skips identifier keys such as ``account_id``, because an ID
    projects as a number but carries no measurement.
    """
    numeric = next(
        (k for k, v in node_props.items()
         if isinstance(v, (int, float)) and not isinstance(v, bool)
         and k != "id" and not k.endswith("_id")),
        None,
    )
    non_numeric = next(
        (k for k, v in node_props.items()
         if isinstance(v, (str, Date, DateTime, Time, Duration))),
        None,
    )
    return numeric, non_numeric


def _build_scenarios(node_num: str | None, node_str: str | None) -> list[Scenario]:
    """Assemble supported projections and identify unsupported string properties."""
    scenarios = [
        Scenario("A_control",
                 "labels + relationshipType only (the working fast-gds shape)",
                 BASE_CONFIG),
        Scenario("B_rel_amount", "relationshipProperties { amount } (numeric)",
                 BASE_CONFIG + ", relationshipProperties: { amount: t.amount }",
                 weight_prop="amount"),
        Scenario("C_rel_timestamp",
                 "relationshipProperties { transfer_timestamp_ms } "
                 "(numeric epoch milliseconds)",
                 BASE_CONFIG + ", relationshipProperties: "
                 "{ transfer_timestamp_ms: transfer_timestamp_ms }",
                 with_clause=TIMESTAMP_MS_WITH),
        Scenario("D_rel_both",
                 "relationshipProperties { amount, transfer_timestamp_ms }",
                 BASE_CONFIG
                 + ", relationshipProperties: "
                 "{ amount: t.amount, transfer_timestamp_ms: transfer_timestamp_ms }",
                 weight_prop="amount", with_clause=TIMESTAMP_MS_WITH),
    ]
    if node_num is not None:
        scenarios.append(Scenario(
            f"E_node_numeric ({node_num})",
            f"sourceNodeProperties / targetNodeProperties {{ {node_num} }} (numeric)",
            BASE_CONFIG
            + f", sourceNodeProperties: {{ {node_num}: src.{node_num} }}"
            + f", targetNodeProperties: {{ {node_num}: dst.{node_num} }}"))
    if node_str is not None:
        scenarios.append(Scenario(
            f"F_node_nonnumeric ({node_str})",
            f"sourceNodeProperties / targetNodeProperties {{ {node_str} }} "
            "(non-numeric)",
            None,
            skip_reason=(f"{node_str} is non-numeric; keep it outside the GDS "
                         "projection")))
    return scenarios


def run_probe(args: argparse.Namespace) -> bool:
    """Sweep usable GDS property configs on the recent transfer window.

    Returns ``False`` when sizing fails or any scenario ends in FAIL or PARTIAL.
    """
    uri, auth = load_connection()

    print(f"Connecting to {uri} ...")
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()

        # Window cutoff from the dataset max (data ends in the past), passed as $since.
        if args.since_hours is not None:
            window = dt.timedelta(hours=args.since_hours)
            window_label = f"last {args.since_hours}h"
        else:
            window = dt.timedelta(days=args.since_days)
            window_label = f"last {args.since_days}d"
        max_transfer, _ = data_max_dates(driver)
        since = max_transfer.to_native() - window
        print(f"Connected. Window: {window_label}, transfer_timestamp >= {since}.")

        sized = run_statement(driver, f"size window (count edges, {window_label})",
                              COUNT_WINDOW, {"since": since})
        if sized is None:
            return False
        edges = sized[0]["edges"]
        print(f"  -> {edges} edge(s) in the window.")
        if edges == 0:
            print("\nWindow is empty; widen --since-hours/--since-days.")
            return True

        node_num, node_str = _introspect(driver, since)
        if args.count_only:
            print("\n--count-only set; introspected the schema without provisioning.")
            return True

        scenarios = _build_scenarios(node_num, node_str)
        print(f"\nRunning {len(scenarios)} projection scenario(s) as "
              f"'{args.graph}_<id>', memory={args.memory}. Each provisions its own "
              "session.")

        results: list[tuple[str, str, str]] = []
        for scenario in scenarios:
            results.append(_run_scenario(driver, args, since, scenario))

        _print_summary(results)
        return all(status in ("OK", "SKIP") for _, status, _ in results)


def _introspect(driver: Driver, since: dt.datetime) -> tuple[str | None, str | None]:
    """Print the sampled relationship/node property types; pick node props to probe."""
    sample = run_statement(driver, "introspect one windowed relationship + endpoint",
                           SAMPLE_ROW, {"since": since})
    if not sample:
        print("  Could not sample a row; node-property scenarios will be skipped.")
        return None, None
    rel_props = sample[0]["rel"] or {}
    node_props = sample[0]["node"] or {}
    print("  Relationship (TRANSFERRED_TO) property types:")
    for key, value in rel_props.items():
        print(f"    {key}: {_type_name(value)}")
    print("  Node (:Account) property types:")
    for key, value in node_props.items():
        print(f"    {key}: {_type_name(value)}")
    node_num, node_str = _pick_props(node_props)
    print(f"  Probing node properties: numeric={node_num!r}, non-numeric={node_str!r}.")
    return node_num, node_str


def _run_scenario(driver: Driver, args: argparse.Namespace, since: dt.datetime,
                  scenario: Scenario) -> tuple[str, str, str]:
    """Run one projection scenario under a unique name, optionally check it, drop."""
    print(f"\n{'=' * 78}\nScenario {scenario.key}: {scenario.description}")
    if scenario.skip_reason is not None:
        print(f"  SKIPPED: {scenario.skip_reason}")
        return (scenario.key, "SKIP", scenario.skip_reason)

    if scenario.data_config is None:
        raise ValueError(f"scenario {scenario.key} has no data config to project")
    cypher = PROJECT_TEMPLATE.format(data_config=scenario.data_config,
                                     with_clause=scenario.with_clause)
    # Every scenario gets its own name, so a failed session or leftover mapping from
    # one scenario cannot block the next. A failed projection drops the name it used.
    projected, graph = project_with_cleanup(
        driver, f"project ({scenario.key})", cypher,
        {"memory": args.memory, "since": since}, new_graph_name(args.graph),
        retry_prefix=args.graph)

    if projected is None:
        return (scenario.key, "FAIL", "projection rejected (see error above)")

    detail = "projected"
    for row in projected:
        result = row["result"]
        if isinstance(result, dict):
            detail = (f"{result.get('nodeCount')} nodes / "
                      f"{result.get('relationshipCount')} rels, "
                      f"projectMillis={result.get('projectMillis')}")

    # If the scenario carried a numeric relationship weight, confirm it is usable.
    if scenario.weight_prop is not None:
        weighted = run_statement(
            driver, f"weighted PageRank on '{scenario.weight_prop}'",
            PAGERANK_WEIGHTED.format(prop=scenario.weight_prop), {"graph": graph})
        if weighted is None:
            run_statement(driver, f"drop '{graph}'", DROP_GRAPH, {"graph": graph})
            return (scenario.key, "PARTIAL",
                    (f"{detail}; but weighted PageRank on '{scenario.weight_prop}' "
                     "failed"))
        detail += f"; weighted PageRank OK ({len(weighted)} rows)"

    dropped = run_statement(driver, f"drop '{graph}'", DROP_GRAPH, {"graph": graph})
    if dropped is None:
        return (scenario.key, "PARTIAL", f"{detail}; but dropping '{graph}' failed")
    return (scenario.key, "OK", detail)


def _print_summary(results: list[tuple[str, str, str]]) -> None:
    """Print the scenario / status / detail summary table."""
    print(f"\n{'=' * 78}\nSweep summary\n")
    key_w = max(len("scenario"), *(len(r[0]) for r in results))
    status_w = max(len("status"), *(len(r[1]) for r in results))
    print(f"  {'scenario'.ljust(key_w)}  {'status'.ljust(status_w)}  detail")
    print(f"  {'-' * key_w}  {'-' * status_w}  {'-' * 6}")
    for key, status, detail in results:
        print(f"  {key.ljust(key_w)}  {status.ljust(status_w)}  {detail}")
