# Reproducing the warehouse performance tests

A full worked example of re-running one performance phase end to end, expanding the tight
summary in the [README](../README.md#reproducing-the-warehouse-performance-tests). The plan
and the recorded results live alongside this file:

- [`perf-test.md`](perf-test.md): the seven-phase plan (what to run and why).
- [`perf-tests-results.md`](perf-tests-results.md): the recorded timings and work log.
- [`perf-tests-results-v2.md`](perf-tests-results-v2.md): the Test set C staged-ramp spike.
- [`verify-best.md`](verify-best.md): the best-practices verification, which is where the
  Statements-API history-pull recipe was first written down.

## The three roles, and which tool owns each

Three different things get measured, and each has one tool that owns it. Keeping them
separate is what makes the runs reproducible.

| Role | Tool | Notes |
|---|---|---|
| **Reported timings** (the min/median/max seconds in the results tables) | `vg-probe`, client wall-clock on the Aura side | The load-bearing metric. This is not a Databricks measurement at all; it is the time the client waits for a single Cypher statement. |
| **Warehouse lifecycle** (cache-clearing restart, resize between phases) | `databricks warehouses` CLI, or the `manage_sql_warehouse` MCP tool | Restart is the only reliable cache clear; the warehouse local disk cache has no SQL clear command. |
| **Databricks confirmatory metrics** (execution time, spill, rows read) | `system.query.history`, pulled with the Databricks SQL Statements API via the CLI (primary), or the MCP `execute_sql` tool (optional) | Confirmatory only. It lags about 11 minutes, sometimes closer to 25, so it cannot be read right after a test. |

## Why this order

- **The metrics pull leads with the Statements API, not the MCP `execute_sql` tool.** The
  MCP tool has a hard 60-second cap that ignores its own timeout parameter, and the
  `system.query.history` scans routinely outrun it.
- **The async Statements API has no such cap.** It is scriptable and needs only the
  `databricks` CLI plus a profile, which is already required for the lifecycle commands.
- **The MCP tool stays as an optional convenience** for quick interactive pulls, when the
  scan is kept cheap.
- **`vg-probe` wall-clock is the primary reported metric** because of the history lag. It is
  available the instant a query returns, while the Databricks-side detail lands minutes
  later and only confirms what the wall-clock already showed.

## Prerequisites

- `uv` and the project `.env` from the [Quick start](../README.md#quick-start), so
  `uv run vg-probe` works.
- The `databricks` CLI authenticated to a profile with access to the backing warehouse.
  The examples use profile `aws-partner-rk` and backing warehouse `b0fffb8e3255bf85`
  (`vg demo sql warehouse`). Substitute your own.
- The MCP `databricks` server is optional; everything below runs without it.

## Worked example: one phase, end to end

This reproduces Phase 1 of the plan, Test set A on the 2X-Small warehouse, then resizes and
repeats on Small (Phase 2). The same shape applies to every phase. The five steps:

1. Restart the warehouse to clear its cache, at the target size.
2. Record the client-floor baseline.
3. Run the test query five times, keeping every timing.
4. Pull the Databricks-side metrics after the lag.
5. Resize and repeat for the next phase.

### 1. Restart the warehouse to clear its cache, at the target size

- Stopping clears the warehouse local disk cache, so the next run is a genuine cold scan.
- Set the size while it is stopped.
- Start it and poll until it reports `RUNNING`.

Resize with the CLI:

```bash
databricks --profile aws-partner-rk warehouses stop   b0fffb8e3255bf85
databricks --profile aws-partner-rk warehouses update b0fffb8e3255bf85 --cluster-size "2X-Small"
databricks --profile aws-partner-rk warehouses start  b0fffb8e3255bf85
databricks --profile aws-partner-rk warehouses get    b0fffb8e3255bf85   # repeat until state == RUNNING
```

Or resize with the MCP tool, while the warehouse is stopped:

```
manage_sql_warehouse(action="modify", warehouse_id="b0fffb8e3255bf85", size="2X-Small")
```

Notes:

- Valid sizes are the Databricks cluster-size strings, `2X-Small` and `Small` for this test.
- Resize the warehouse only while it is stopped, so it comes back up cold at the new size.

### 2. Record the client-floor baseline

- `RETURN 1` measures the client plus Bolt round-trip floor and never reaches the warehouse.
- It is a baseline, not a warm-up.

```bash
cd virtual-graph-demo
uv run vg-probe "RETURN 1 AS ok"      # ~0.2 to 0.3s
```

### 3. Run the test query five times, keeping every timing

- Run one statement at a time so the connection pool stays free.
- Run 1 is the cold sample (the warehouse was just restarted); runs 2 onward are warm.

Example with A3, the 7-day window:

```bash
uv run vg-probe 'MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH src.account_id AS sender, dst.account_id AS recipient,
     count(t) AS pair_transfers, sum(t.amount) AS pair_outflow
RETURN sender, recipient, pair_transfers, pair_outflow'
```

Then:

- Record the wall-clock and row count from each of the five runs.
- Report min / median / max. These are the numbers that go straight into the results table.

### 4. Pull the Databricks-side metrics after the lag

- Wait at least 12 minutes, then pull `system.query.history` filtered to the backing
  warehouse and the test window.
- The interesting columns:
  - `execution_status`: `FINISHED`, or a failure mode.
  - `total_duration_ms` and `execution_duration_ms`: the Databricks-side time.
  - `produced_rows` and `read_bytes`: rows returned and bytes scanned.
  - `spilled_local_bytes`: a non-zero value is the memory-pressure signal.

**Primary: the Statements API via the CLI.** Submit async so the 60-second synchronous
wait never bites, then poll until the statement reports `SUCCEEDED`.

```bash
# Submit, asking the server to return a handle after 5s instead of blocking:
databricks --profile aws-partner-rk api post /api/2.0/sql/statements --json '{
  "warehouse_id": "b0fffb8e3255bf85",
  "wait_timeout": "5s",
  "on_wait_timeout": "CONTINUE",
  "statement": "SELECT statement_id, execution_status, total_duration_ms, execution_duration_ms, produced_rows, read_bytes, spilled_local_bytes, start_time FROM system.query.history WHERE compute.warehouse_id = '\''b0fffb8e3255bf85'\'' AND statement_text ILIKE '\''%account_links%'\'' AND start_time >= '\''2024-01-01T00:00:00Z'\'' ORDER BY start_time DESC LIMIT 50"
}'

# The response carries a statement_id. Poll it until state == SUCCEEDED, then read the rows:
databricks --profile aws-partner-rk api get /api/2.0/sql/statements/<statement_id>
```

Notes:

- Adjust the `start_time` lower bound to the phase window.
- The response carries a `statement_id`; poll the `GET` endpoint until `state == SUCCEEDED`.
- Because the run is isolated and one query at a time, ordering by `start_time` lines the
  history rows up with the client runs in sequence.

**Optional: the MCP `execute_sql` tool.** Quicker for an interactive pull, with caveats:

- It caps at 60 seconds and ignores its timeout argument, so keep the scan cheap.
- Select computed flags and short `left()` / `right()` slices instead of the full
  `statement_text`.
- Pin a tight `start_time` window so partition pruning keeps the scan inside the cap.

```
execute_sql(warehouse_id="b0fffb8e3255bf85", statement="
  SELECT statement_id, execution_status, total_duration_ms, spilled_local_bytes, start_time
  FROM system.query.history
  WHERE compute.warehouse_id = 'b0fffb8e3255bf85'
    AND start_time >= '<tight-window-start>'
  ORDER BY start_time DESC LIMIT 50")
```

If even the cheap scan times out, fall back to the Statements API above.

### 5. Resize and repeat for the next phase

- Restart at the new size and run the same queries again.
- This keeps the comparison cold-start to cold-start at each size.

```bash
databricks --profile aws-partner-rk warehouses stop   b0fffb8e3255bf85
databricks --profile aws-partner-rk warehouses update b0fffb8e3255bf85 --cluster-size "Small"
databricks --profile aws-partner-rk warehouses start  b0fffb8e3255bf85
databricks --profile aws-partner-rk warehouses get    b0fffb8e3255bf85   # until RUNNING
```

Then repeat steps 2 through 4. When all phases are done, stop the warehouse:

```bash
databricks --profile aws-partner-rk warehouses stop b0fffb8e3255bf85
```

## Correlating a history row to a client run

- Filter on `compute.warehouse_id = 'b0fffb8e3255bf85'` and
  `statement_text ILIKE '%account_links%'`, ordered by `start_time`.
- The warehouse is isolated for the test and queries run one at a time, so the first N rows
  after a test start are that query's N runs.
- No distinctive-literal trick is needed; content plus order is enough.
