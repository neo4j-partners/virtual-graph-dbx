"""100m demo (``--demo 100m``).

Reproduces the "Zero spill from 100K to 100M rows" finding. Unlike the other demos, this
one talks SQL straight to the Databricks warehouse rather than Cypher over Bolt, because
the finding is the SQL-side spike: it
runs the aggregation SQL that the Virtual Graph pushes down to, directly against
``account_links_large``, and reads ``spill_to_disk_bytes`` from the warehouse query
history to show the warehouse never spills.

The SQL path is the Databricks SDK (``WorkspaceClient``): ``statement_execution`` runs
the queries and ``query_history`` pulls the confirmatory metrics. The SDK ships as the
``history`` extra (``uv sync --extra history``) and is required here, since this demo
has no Bolt fallback.

Flow:

1. Run the C1/C2/C3 aggregation SQL (each wrapped in an outer aggregate so the
   scan-and-aggregate cost is paid without shipping result rows), recording client
   wall-clock. ``--build`` first rebuilds ``account_links_large`` at each ramp size
   (destructive ``CREATE OR REPLACE``); without it the demo queries the existing table.
2. Wait out the warehouse query-history lag (up to a few minutes), polling on an
   interval and printing a countdown each check, until every statement's row has
   finalized.
3. Print the confirmatory metrics and the zero-spill headline.

Connection details (profile, catalog, schema) come from the project ``.env`` via
``load_databricks_config``; the warehouse defaults to the backing VG warehouse.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from connection import VG_BACKING_WAREHOUSE, load_databricks_config

# The ramp from the spike: build at each size and confirm zero spill all the way up.
RAMP_SIZES = [100_000, 250_000, 500_000, 1_000_000, 10_000_000, 50_000_000, 100_000_000]

# A tag carried in every statement's SQL text. Correlation back to history is by
# statement id; the tag just makes this demo's runs easy to spot in query history.
TAG = "vg-demo:100m"


class SqlError(RuntimeError):
    """A Databricks statement failed or ended in a non-SUCCEEDED state."""


@dataclass
class SqlResult:
    statement_id: str
    columns: list[str]
    rows: list[list[str]]
    wall_clock_s: float


@dataclass
class RunRecord:
    """One submitted C-query: its label, ramp size, and statement id to correlate."""

    label: str
    size: int
    statement_id: str
    wall_clock_s: float
    inner_rows: str


@dataclass
class HistRow:
    """The confirmatory metrics for one statement, from warehouse query history."""

    execution_ms: int | None
    read_rows: int | None
    spill_bytes: int
    # Served from the result cache: nothing ran, so spill is not measured.
    from_cache: bool = False


def run_sql(client: object, warehouse_id: str, statement: str, *,
            poll_interval: float = 2.0, timeout: float = 900.0) -> SqlResult:
    """Execute one statement, polling until it finishes, and return its rows + timing.

    Submits with a 50s server-side wait (the API maximum) and
    ``on_wait_timeout=CONTINUE``, so short statements return on the first call and only
    a genuinely long one (a large build) falls through to polling. ``wall_clock_s`` is
    the client submit-to-finish time; the trustworthy execution time and spill come
    later from query history.
    """
    from databricks.sdk.service.sql import (
        ExecuteStatementRequestOnWaitTimeout,
        StatementState,
    )

    t0 = time.perf_counter()
    resp = client.statement_execution.execute_statement(
        statement=statement, warehouse_id=warehouse_id, wait_timeout="50s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE)
    statement_id = resp.statement_id
    state = resp.status.state
    while state in (StatementState.PENDING, StatementState.RUNNING):
        if time.perf_counter() - t0 > timeout:
            raise SqlError(f"timed out after {timeout:g}s in state {state.value} "
                           f"(statement {statement_id}, still running server-side)")
        time.sleep(poll_interval)
        resp = client.statement_execution.get_statement(statement_id)
        state = resp.status.state
    wall = time.perf_counter() - t0
    if state != StatementState.SUCCEEDED:
        message = resp.status.error.message if resp.status.error else "(no message)"
        raise SqlError(f"statement {statement_id} ended {state.value}: {message}")
    schema = resp.manifest.schema if resp.manifest else None
    columns = [col.name for col in schema.columns] if schema and schema.columns else []
    rows = (resp.result.data_array if resp.result else None) or []
    return SqlResult(statement_id, columns, rows, wall)


def build_sql(table: str, accounts: str, n: int) -> str:
    """The CTAS that builds ``account_links_large`` at ``n`` rows."""
    return f"""CREATE OR REPLACE TABLE {table}
USING DELTA
PARTITIONED BY (transfer_date)
AS
WITH ids AS (
  SELECT account_id, (row_number() OVER (ORDER BY account_id)) - 1 AS idx
  FROM {accounts}
),
n AS (SELECT count(*) AS c FROM ids),
gen AS (
  SELECT
    id AS link_id,
    CAST(rand(1) * (SELECT c FROM n) AS BIGINT) AS src_idx,
    CAST(rand(2) * (SELECT c FROM n) AS BIGINT) AS dst_idx,
    ROUND(rand(3) * 5000 + 1, 2) AS amount,
    TIMESTAMP('2024-01-01 00:00:00')
      + make_interval(0, 0, 0, CAST(rand(4) * 90 AS INT),
                      CAST(rand(5) * 24 AS INT), CAST(rand(6) * 60 AS INT), 0)
        AS transfer_timestamp
  FROM range(0, {n})
)
SELECT g.link_id, s.account_id AS src_account_id, d.account_id AS dst_account_id,
       g.amount, g.transfer_timestamp,
       CAST(g.transfer_timestamp AS DATE) AS transfer_date
FROM gen g
JOIN ids s ON s.idx = g.src_idx
JOIN ids d ON d.idx = g.dst_idx"""


def c_queries(table: str) -> list[tuple[str, str]]:
    """The C1/C2/C3 pushdown-equivalent SQL, each wrapped in an outer aggregate.

    The wrapper avoids shipping the result rows; its first column is the inner group
    count. The optimizer drops inner aggregates the wrapper never reads, so the C1 and
    C2 wrappers also sum or max every inner aggregate. That keeps ``sum(amount)``,
    ``avg(amount)`` and ``max(amount)`` in the plan, and the scan reads ``amount`` as
    well as ``src_account_id``. C3 orders by ``pair_outflow``, so its ``sum(amount)``
    survives with a plain ``count(*)`` wrapper.

    The wrapper also selects ``current_timestamp()`` to bypass the warehouse result
    cache. The cache matches on the normalized plan, so a re-run is served from cache
    (``read_rows`` 0, spill 0 because nothing ran) even with a new comment or alias, and
    the Statement Execution API runs each statement in its own session, so a separate
    ``SET use_cached_result = false`` does not carry over. A non-deterministic
    expression opts the statement out of the cache without touching the aggregation.
    """
    c1 = f"""SELECT count(*), sum(transfers), sum(outflow), avg(avg_amount),
       max(max_amount), current_timestamp() AS run_at FROM (
  SELECT src_account_id AS account_id,
         count(*) AS transfers, sum(amount) AS outflow,
         avg(amount) AS avg_amount, max(amount) AS max_amount
  FROM {table}
  GROUP BY src_account_id
)"""
    c2 = f"""SELECT count(*), sum(transfers), sum(outflow),
       current_timestamp() AS run_at FROM (
  SELECT src_account_id AS account_id, count(*) AS transfers, sum(amount) AS outflow
  FROM {table}
  WHERE transfer_timestamp >= TIMESTAMP('2024-03-23T23:58:00Z')
  GROUP BY src_account_id
)"""
    c3 = f"""SELECT count(*), current_timestamp() AS run_at FROM (
  SELECT src_account_id AS sender, dst_account_id AS recipient,
         count(*) AS pair_transfers, sum(amount) AS pair_outflow
  FROM {table}
  GROUP BY src_account_id, dst_account_id
  ORDER BY pair_outflow DESC
  LIMIT 100
)"""
    return [("C1 full-table group-by", c1),
            ("C2 windowed group-by", c2),
            ("C3 high-card pair group-by", c3)]


def tagged(label: str, size: int, sql: str) -> str:
    """Prepend the demo's tag comment so this run is easy to find in query history."""
    return f"/* {TAG} | {label} | size={size} */\n{sql}"


def run_c_queries(client: object, warehouse: str, table: str,
                  size: int) -> list[RunRecord]:
    """Run C1/C2/C3 against ``table`` at ramp ``size``; return their records."""
    records: list[RunRecord] = []
    for label, sql in c_queries(table):
        try:
            res = run_sql(client, warehouse, tagged(label, size, sql))
        except SqlError as exc:
            print(f"    {label}: ERROR {exc}")
            continue
        inner = res.rows[0][0] if res.rows else "?"
        print(f"    {label}: OK {res.wall_clock_s:.1f}s client; "
              f"inner group count {inner}")
        records.append(
            RunRecord(label, size, res.statement_id, res.wall_clock_s, inner))
    return records


def table_row_count(client: object, warehouse: str, table: str) -> int:
    """Current row count of ``account_links_large`` (read-only mode reports it)."""
    res = run_sql(client, warehouse, f"SELECT count(*) FROM {table}")
    return int(res.rows[0][0])


def pull_history(client: object, warehouse: str, start_ms: int, end_ms: int,
                 want_ids: set[str]) -> dict[str, HistRow]:
    """Pull this run's finalized statements from warehouse query history, keyed by id.

    Scoped by warehouse, statement id, and the run's time window so the result fits one
    page. ``include_metrics`` is required for the spill / read-rows fields to populate.
    Only rows that have finalized (``is_final``) are returned, so a row counts as landed
    exactly when its metrics are stable.
    """
    from databricks.sdk.service.sql import QueryFilter, TimeRange

    response = client.query_history.list(
        filter_by=QueryFilter(
            warehouse_ids=[warehouse],
            statement_ids=list(want_ids),
            query_start_time_range=TimeRange(start_time_ms=start_ms,
                                             end_time_ms=end_ms),
        ),
        include_metrics=True,
    )
    found: dict[str, HistRow] = {}
    for info in response.res or []:
        if info.query_id not in want_ids or not info.is_final:
            continue
        metrics = info.metrics
        found[info.query_id] = HistRow(
            execution_ms=metrics.execution_time_ms if metrics else None,
            read_rows=metrics.rows_read_count if metrics else None,
            spill_bytes=(metrics.spill_to_disk_bytes or 0) if metrics else 0,
            from_cache=bool(metrics.result_from_cache) if metrics else False,
        )
    return found


def _mmss(seconds: float) -> str:
    """Format a duration as ``Xm Ys``."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def await_history(client: object, warehouse: str, start_ms: int, want_ids: set[str], *,
                  poll_interval: float, lag_estimate: float,
                  max_wait: float) -> dict[str, HistRow]:
    """Poll query history until every statement has finalized, with a countdown.

    Warehouse query history can lag by up to a few minutes, so the metrics may not be
    readable right after the queries run. Each check prints elapsed time, how many
    statements have landed, and a countdown toward the estimated lag; polling
    continues past the estimate until all rows arrive or ``max_wait`` is hit.

    A failed pull is reported and polling continues. The SDK raises an API error as a
    ``DatabricksError`` subclass, a network failure as a ``requests`` exception, and
    ``TimeoutError`` once its own transient-error retries run out.
    """
    from databricks.sdk.errors import DatabricksError
    from requests.exceptions import RequestException

    print(f"\n  Query history can lag up to a few minutes; polling every "
          f"{poll_interval / 60:g} min until all {len(want_ids)} statements land "
          f"(est. lag ~{lag_estimate / 60:g} min, "
          f"giving up after {max_wait / 60:g} min).")
    t0 = time.perf_counter()
    check = 0
    while True:
        check += 1
        end_ms = int(time.time() * 1000) + 2_000
        try:
            found = pull_history(client, warehouse, start_ms, end_ms, want_ids)
        except (DatabricksError, RequestException, TimeoutError) as exc:
            print(f"  [check {check}] history pull failed "
                  f"({type(exc).__name__}: {exc})")
            found = {}
        landed = want_ids & set(found)
        elapsed = time.perf_counter() - t0
        if want_ids <= set(found):
            print(f"  [check {check}] all {len(want_ids)} statements landed "
                  f"after {_mmss(elapsed)}.")
            return found
        if elapsed >= max_wait:
            print(f"  [check {check}] giving up after {_mmss(elapsed)}: "
                  f"{len(landed)}/{len(want_ids)} landed "
                  "(lag exceeded --max-wait-minutes).")
            return found
        remaining = lag_estimate - elapsed
        eta = (f"~{_mmss(remaining)} to est. lag" if remaining > 0
               else f"est. lag passed by {_mmss(-remaining)}")
        print(f"  [check {check}] {len(landed)}/{len(want_ids)} landed | "
              f"elapsed {_mmss(elapsed)} | {eta} | "
              f"next check in {poll_interval / 60:g} min")
        time.sleep(min(poll_interval, max_wait - elapsed))


def print_spill_report(records: list[RunRecord], found: dict[str, HistRow]) -> None:
    """Print the confirmatory metrics and the zero-spill headline."""
    print(f"\n{'=' * 78}")
    print("Databricks confirmatory metrics (query history)")
    print("=" * 78)
    print(f"  {'label':<28}{'size':>14}{'exec_ms':>10}{'read_rows':>16}"
          f"{'groups':>9}{'spill_bytes':>14}")
    print(f"  {'-' * 28}{'-' * 14:>14}{'-' * 10:>10}{'-' * 16:>16}"
          f"{'-' * 9:>9}{'-' * 14:>14}")
    total_spill = 0
    pending = 0
    cached = 0
    for rec in records:
        hist = found.get(rec.statement_id)
        if hist is None:
            pending += 1
            print(f"  {rec.label:<28}{rec.size:>14,}{'(pending)':>10}"
                  f"{'':>16}{rec.inner_rows:>9}{'':>14}")
            continue
        if hist.from_cache:
            cached += 1
            print(f"  {rec.label:<28}{rec.size:>14,}{'(cached)':>10}"
                  f"{'':>16}{rec.inner_rows:>9}{'not measured':>14}")
            continue
        total_spill += hist.spill_bytes
        exec_ms = f"{hist.execution_ms:,}" if hist.execution_ms is not None else "?"
        read_rows = f"{hist.read_rows:,}" if hist.read_rows is not None else "?"
        print(f"  {rec.label:<28}{rec.size:>14,}{exec_ms:>10}{read_rows:>16}"
              f"{rec.inner_rows:>9}{hist.spill_bytes:>14,}")
    print("=" * 78)
    if pending:
        print(f"  {pending} statement(s) still pending in history; "
              "rerun later to confirm.")
    if cached:
        print(f"  {cached} statement(s) were served from the result cache: cached, not "
              "measured. Their spill says nothing about the aggregation.")
    if total_spill == 0 and pending == 0 and cached == 0:
        sizes = sorted({rec.size for rec in records})
        span = (f"{sizes[0]:,} to {sizes[-1]:,} rows" if len(sizes) > 1
                else f"{sizes[0]:,} rows")
        print(f"  Zero spill: spill_to_disk_bytes = 0 across all {len(records)} "
              f"statements ({span}). The 2X-Small builds its hash aggregate in memory.")
    elif total_spill > 0:
        print(f"  Spill detected: {total_spill:,} total spill_to_disk_bytes "
              "(unexpected for this query shape on this data).")


def run_spike(args: argparse.Namespace) -> None:
    """Run the C-query workload over the ramp and confirm zero spill from history."""
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("The 100m demo needs the Databricks SDK. Install the history extra:\n"
              "  uv sync --extra history")
        return

    cfg = load_databricks_config()
    profile = args.profile or cfg.profile
    warehouse = args.warehouse or VG_BACKING_WAREHOUSE
    table = f"`{cfg.catalog}`.`{cfg.schema}`.`account_links_large`"
    accounts = f"`{cfg.catalog}`.`{cfg.schema}`.`accounts`"

    try:
        client = WorkspaceClient(profile=profile)
    except (ValueError, OSError) as exc:
        print(f"Could not build a Databricks client ({exc}). Check the "
              "DATABRICKS_CONFIG_PROFILE in .env or pass --profile.")
        return

    print(f"100m demo: SQL-side zero-spill spike on warehouse {warehouse} "
          f"(profile {profile}).")
    print(f"Table: {table}")

    # Lower-bound the history window at the run's start (minus a buffer for clock skew).
    start_ms = int(time.time() * 1000) - 60_000

    records: list[RunRecord] = []
    if args.build:
        sizes = args.sizes or RAMP_SIZES
        print(f"\n--build: rebuilding {table} (destructive CREATE OR REPLACE) at "
              f"{len(sizes)} ramp size(s): {', '.join(f'{n:,}' for n in sizes)}.")
        for size in sizes:
            print(f"\n  ramp {size:,} rows:")
            try:
                built = run_sql(client, warehouse,
                                tagged("build", size, build_sql(table, accounts, size)))
            except SqlError as exc:
                print(f"    build {size:,}: ERROR {exc}; skipping its queries.")
                continue
            print(f"    build: OK {built.wall_clock_s:.1f}s client wall-clock.")
            records.extend(run_c_queries(client, warehouse, table, size))
    else:
        try:
            size = table_row_count(client, warehouse, table)
        except SqlError as exc:
            print(f"\nCould not read {table} ({exc}). "
                  "Use --build to create it, or check --warehouse / catalog / schema.")
            return
        print(f"\nRead-only: querying the existing table ({size:,} rows). "
              "Use --build to rebuild the full 100K-to-100M ramp.")
        records.extend(run_c_queries(client, warehouse, table, size))

    if not records:
        print("\nNo queries ran successfully; nothing to confirm in history.")
        return

    if args.skip_history:
        print(f"\n--skip-history: ran {len(records)} statement(s) but not waiting for "
              "the history lag (up to a few minutes). Spill not confirmed; rerun "
              "without --skip-history.")
        return

    want_ids = {rec.statement_id for rec in records}
    found = await_history(client, warehouse, start_ms, want_ids,
                          poll_interval=args.poll_minutes * 60.0,
                          lag_estimate=args.history_lag_minutes * 60.0,
                          max_wait=args.max_wait_minutes * 60.0)
    print_spill_report(records, found)
