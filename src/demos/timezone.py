"""Timezone round-trip demo (``--demo timezone``).

Reproduces a slow path on the Virtual Graph: the engine fires one
``SELECT current_timezone()`` warehouse round trip per TIMESTAMP value it materializes
into a Cypher datetime, run serially at about 5.5 per second with no caching. DATE values
and plain scalars cost nothing.

The demo runs five discriminating queries, each capped at ``LIMIT 25``:

* A  ``RETURN t``                    relationship carrying a TIMESTAMP  -> 25 calls
* B  ``RETURN t.amount, t.link_id``  non-temporal scalars               ->  0 calls
* C  ``RETURN t.transfer_timestamp`` a bare TIMESTAMP scalar            -> 25 calls
* D  ``RETURN a`` (Account)          a node whose only temporal is DATE ->  0 calls
* E  rerun A twice back to back      no session cache                   -> 25 + 25

Two signals are reported. The always-available one is wall-clock: the TIMESTAMP-bearing
queries take seconds for 25 rows while the scalar / DATE queries are sub-second. When
the Databricks SDK is installed (``uv sync --extra history``) and a warehouse is
configured (``DATABRICKS_WAREHOUSE_ID``, ``DATABRICKS_CONFIG_PROFILE`` in ``.env``), the
demo also pulls the warehouse query history and counts the actual ``current_timezone()``
statements in each run's time window, which is the direct proof.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from neo4j import Driver
from neo4j.exceptions import DriverError, Neo4jError

from helpers import driver_error, run_cypher

LIMIT = 25  # one current_timezone() call per returned row, or none

POLL_INTERVAL_S = 180  # poll query history every 3 minutes (ingestion lags ~11 minutes)
MAX_POLLS = 10  # give up after ~30 minutes and report wall-clock only


@dataclass(frozen=True)
class TzProbe:
    """One discriminating query, its prediction, and how many times to run it."""

    key: str
    title: str
    cypher: str
    predicted_per_run: int
    repeat: int = 1


@dataclass
class TzRun:
    """A single execution: its wall-clock window (epoch ms) and what came back."""

    label: str
    predicted: int
    start_ms: int
    end_ms: int
    elapsed: float
    rows: int
    actual: int | None = None  # current_timezone() calls counted from history, if available


PROBES: tuple[TzProbe, ...] = (
    TzProbe(
        "A",
        "RETURN t  (relationship carrying transfer_timestamp, a TIMESTAMP)",
        f"MATCH ()-[t:TRANSFERRED_TO]->() RETURN t LIMIT {LIMIT}",
        predicted_per_run=LIMIT,
    ),
    TzProbe(
        "B",
        "RETURN t.amount, t.link_id  (non-temporal scalars)",
        f"MATCH ()-[t:TRANSFERRED_TO]->() "
        f"RETURN t.amount AS amount, t.link_id AS link_id LIMIT {LIMIT}",
        predicted_per_run=0,
    ),
    TzProbe(
        "C",
        "RETURN t.transfer_timestamp  (a bare TIMESTAMP scalar)",
        f"MATCH ()-[t:TRANSFERRED_TO]->() RETURN t.transfer_timestamp AS ts LIMIT {LIMIT}",
        predicted_per_run=LIMIT,
    ),
    TzProbe(
        "D",
        "RETURN a  (Account node whose only temporal property is a DATE)",
        f"MATCH (a:Account) RETURN a LIMIT {LIMIT}",
        predicted_per_run=0,
    ),
    TzProbe(
        "E",
        "rerun A twice back to back  (no session cache: each run pays in full)",
        f"MATCH ()-[t:TRANSFERRED_TO]->() RETURN t LIMIT {LIMIT}",
        predicted_per_run=LIMIT,
        repeat=2,
    ),
)


def _run_once(driver: Driver, label: str, cypher: str, predicted: int,
              timeout: float) -> TzRun | None:
    """Execute one query, forcing materialization, and record its wall-clock window."""
    start_ms = int(time.time() * 1000)
    t0 = time.perf_counter()
    try:
        rows = run_cypher(driver, cypher, {}, timeout)
    except Neo4jError as exc:
        print(f"  [{label}] ERROR after {time.perf_counter() - t0:.1f}s: "
              f"{exc.code}\n    {exc.message}")
        return None
    except DriverError as exc:
        print(f"  [{label}] ERROR after {time.perf_counter() - t0:.1f}s: {driver_error(exc)}")
        return None
    elapsed = time.perf_counter() - t0
    end_ms = int(time.time() * 1000)
    print(f"  [{label}] {elapsed:6.1f}s  {len(rows):>3} rows  "
          f"(predicted {predicted} current_timezone call(s))")
    return TzRun(label, predicted, start_ms, end_ms, elapsed, len(rows))


def _fetch_tz_starts(client, warehouse_id: str, window_start: int,  # noqa: ANN001
                     window_end: int) -> list[int]:
    """Return sorted start times (epoch ms) of ``current_timezone()`` statements.

    ``query_history.list`` returns a ``ListQueriesResponse`` (not an iterable), so the
    page lives on ``.res``; follow ``next_page_token`` to drain every page.
    """
    from databricks.sdk.service.sql import QueryFilter, TimeRange

    starts: list[int] = []
    page_token: str | None = None
    while True:
        if page_token:
            resp = client.query_history.list(page_token=page_token)
        else:
            resp = client.query_history.list(filter_by=QueryFilter(
                warehouse_ids=[warehouse_id],
                query_start_time_range=TimeRange(start_time_ms=window_start,
                                                 end_time_ms=window_end),
            ))
        starts.extend(
            info.query_start_time_ms
            for info in resp.res or []
            if info.query_text and "current_timezone" in info.query_text.lower()
            and info.query_start_time_ms is not None
        )
        if not resp.has_next_page or not resp.next_page_token:
            break
        page_token = resp.next_page_token
    return sorted(starts)


def _count_timezone_calls(runs: list[TzRun], warehouse_id: str, profile: str | None,
                          wait: float) -> bool:
    """Pull warehouse query history and fill each run's ``actual`` call count.

    Returns ``True`` if history was reached and counts were assigned, ``False`` if the
    SDK is missing or the workspace was unreachable (the demo then reports timing only).
    Query history is read through the REST API, which does not itself land a statement in
    warehouse history, so the count is not polluted by this call.
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("\n  databricks-sdk not installed; reporting wall-clock only.")
        print("  Install the history extra to get the direct call count: "
              "uv sync --extra history")
        return False

    try:
        client = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    except (ValueError, OSError) as exc:  # missing/!invalid profile or config
        print(f"\n  Could not build a Databricks client ({exc}); reporting wall-clock only.")
        return False

    window_start = min(run.start_ms for run in runs) - 2_000
    window_end = max(run.end_ms for run in runs) + 2_000
    total_pred = sum(run.predicted for run in runs)

    print(f"\n  Counting current_timezone() statements on warehouse {warehouse_id} "
          f"(expecting {total_pred}).")
    print(f"  Query history lags ~11 minutes, so polling every "
          f"{POLL_INTERVAL_S // 60} min up to {MAX_POLLS} times "
          f"(~{MAX_POLLS * POLL_INTERVAL_S // 60} min) until all have ingested ...")
    if wait > 0:
        time.sleep(wait)

    for poll in range(1, MAX_POLLS + 1):
        try:
            tz_starts = _fetch_tz_starts(client, warehouse_id, window_start, window_end)
        except Exception as exc:  # noqa: BLE001 - SDK raises a broad set; degrade gracefully
            print(f"  Query-history pull failed ({type(exc).__name__}: {exc}); "
                  "reporting wall-clock only.")
            return False
        print(f"  poll {poll}/{MAX_POLLS}: {len(tz_starts)}/{total_pred} "
              "current_timezone() statements ingested.")
        if len(tz_starts) >= total_pred:
            # Assign each call to the single run whose window contains its start time.
            for run in runs:
                run.actual = sum(1 for ms in tz_starts
                                 if run.start_ms <= ms <= run.end_ms)
            return True
        if poll < MAX_POLLS:
            time.sleep(POLL_INTERVAL_S)

    print(f"  Gave up after {MAX_POLLS} polls (~{MAX_POLLS * POLL_INTERVAL_S // 60} min); "
          "reporting wall-clock only.")
    return False


def _summary(runs: list[TzRun], counted: bool) -> None:
    print(f"\n{'=' * 78}")
    print("Summary")
    print("=" * 78)
    header = f"  {'run':<10}{'elapsed':>9}{'rows':>6}{'predicted':>11}"
    if counted:
        header += f"{'actual':>8}{'verdict':>9}"
    print(header)
    for run in runs:
        line = (f"  {run.label:<10}{run.elapsed:>8.1f}s{run.rows:>6}"
                f"{run.predicted:>11}")
        if counted:
            verdict = "match" if run.actual == run.predicted else "MISMATCH"
            line += f"{run.actual:>8}{verdict:>9}"
        print(line)
    if counted:
        total_actual = sum(run.actual or 0 for run in runs)
        total_pred = sum(run.predicted for run in runs)
        print(f"\n  Total current_timezone() calls: {total_actual} "
              f"(predicted {total_pred}).")
        print("  One round trip per TIMESTAMP value materialized; DATE and scalars are free.")
    else:
        print("\n  Wall-clock contrast is the signal: the TIMESTAMP-bearing runs (A, C, E)")
        print("  spend seconds on 25 rows; the scalar / DATE runs (B, D) are sub-second.")


def run_timezone(driver: Driver, args) -> None:  # noqa: ANN001 - argparse.Namespace
    """Run the timezone round-trip discriminating set and report the two signals."""
    print(f"Running {len(PROBES)} discriminating queries (LIMIT {LIMIT} each, "
          f"timeout {args.timeout:g}s).")
    print("TIMESTAMP-bearing results should be slow; scalar / DATE results fast.\n")

    runs: list[TzRun] = []
    for probe in PROBES:
        print(f"{'-' * 78}\n[{probe.key}] {probe.title}")
        for attempt in range(probe.repeat):
            label = f"{probe.key} ({attempt + 1})" if probe.repeat > 1 else probe.key
            run = _run_once(driver, label, probe.cypher, probe.predicted_per_run,
                            args.timeout)
            if run is not None:
                runs.append(run)

    if not runs:
        print("\nNo queries completed; nothing to summarize.")
        return

    counted = False
    if not args.no_history:
        warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
        profile = os.environ.get("DATABRICKS_CONFIG_PROFILE") or os.environ.get(
            "DATABRICKS_PROFILE")
        if not warehouse_id:
            print("\n  DATABRICKS_WAREHOUSE_ID not set; reporting wall-clock only.")
        else:
            counted = _count_timezone_calls(runs, warehouse_id, profile, args.history_wait)

    _summary(runs, counted)
