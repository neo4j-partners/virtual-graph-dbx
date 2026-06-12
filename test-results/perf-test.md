# Performance Test Plan: Warehouse Size vs. Query Time

This plan measures whether a larger Databricks SQL warehouse improves Virtual Graph
performance. It runs two test sets against the Finance Genie Virtual Graph, each on a
**2X-Small** warehouse and then on a **Small** warehouse, with a background agent watching
the warehouse the whole time.

> **Backing warehouse: `vg demo sql warehouse`, id `b0fffb8e3255bf85`** (serverless PRO,
> auto-stop 10 min). Every query the Virtual Graph runs becomes SQL on this warehouse. The
> primary agent resizes this same warehouse in place between phases, so the name and id stay
> constant; only `cluster_size` changes between 2X-Small and Small. (A second warehouse,
> `Warehouse (AT)` / `06538df9820b42f9`, also shows `select 1` pings in history but is not
> ours, so always filter history on `b0fffb8e3255bf85`.)

- **Test set A** stresses [Pattern 6 "keep result sets small"](best-practices.md#pattern-6-keep-result-sets-small)
  from the best-practices guide: the fan-out pair query at five growing time windows, so
  the result set climbs from a few thousand rows to roughly 223,000.
- **Test set B** stresses the `fast-gds` GDS Session path at three growing windows: the
  known-good 1.5-hour slice (233 edges), then windows that project about 1,000 and about
  2,000 edges.
- **Test set C** is the answer to Phase 0's finding that the source tables are too small for
  warehouse size to matter. It runs a compute-bound aggregation against a **materially
  larger, partitioned** copy of the transfer table (`account_links_large`), so there is
  enough scan-and-aggregate work for a larger warehouse to actually move. This is the one set
  where a warehouse-size effect is expected.

The exercise is seven phases. **Phase 0 is an instrumentation spike** that runs once, on
the 2X-Small warehouse, to confirm what can actually be measured before any real timing.
The six test phases follow, each set run twice: phases 1, 3, and 5 run on the 2X-Small
warehouse, phases 2, 4, and 6 on the Small warehouse.

Every query in the test phases runs **five times** (sets A and C) or **three times** (set
B, the GDS path), and the results report **min / median / max** rather than a single
sample, so warehouse-size effects are separated from run-to-run noise.

**Cache control is by warehouse restart.** The primary agent restarts the SQL warehouse
between every phase to clear its caches, then sets the size for the next phase; this is fully
automated through the Databricks CLI and the `manage_sql_warehouse` MCP tool, with no human
operator in the loop. See
[Between phases: restart and resize](#between-phases-restart-and-resize). Restart is the cache
clear: the warehouse's local disk/IO cache (the scanned Delta files) survives across sessions
on the same cluster and has no SQL clear command, so a stop/start is the only way to guarantee
a cold scan. Within a phase the warehouse is not restarted, so run 1 of each query is cold and
later runs are warm. Phase 0 found the Databricks result cache does not short-circuit the Aura
path, so the reruns genuinely re-execute and the cold-to-warm spread is real.

**Isolation is confirmed.** Nothing else uses the backing warehouse during the test window,
so every query in `system.query.history` for that warehouse belongs to this test.

## Two agents, two jobs

This plan uses two Claude agents running at the same time.

- **The monitor agent runs in the background.** It watches the SQL warehouse that backs
  the Virtual Graph, polls its state, and pulls per-query metrics from Databricks query
  history after each test. It records timings, memory pressure, cost, and bottlenecks. It
  never runs a fraud or GDS query itself.
- **The primary agent runs the tests.** It runs each query one at a time, **repeats it five
  times for set A and three times for set B**, records the wall-clock time the client sees
  for every run, and waits for the previous run to finish before starting the next. Running
  one at a time keeps the 10-connection JDBC pool free; see
  [Performance and the connection pool](best-practices.md#performance-and-the-connection-pool).
  Run 1 of each query is the cold sample (the warehouse was just restarted for this phase);
  runs 2 onward may be warm. The agent reports min / median / max across the runs that
  succeeded and notes any run that failed (see [Recording the results](#recording-the-results)).
  Between phases the primary agent also drives the restart-and-resize itself, with no human in
  the loop; see [Between phases: restart and resize](#between-phases-restart-and-resize).

Both agents write to [`perf-tests-results.md`](perf-tests-results.md), following its logging
protocol: log what a test is about to run before running it, then capture the results and a
summary after it finishes. The plan stays free of results; that file holds them.

Start the monitor agent first, confirm it has found the warehouse, then start the primary
agent.

---

## Start here: the background monitor agent

Launch the monitor agent as a background task before any test runs. Its job is to capture
what the warehouse is doing while the primary agent drives load through it.

### What the monitor agent needs to know first

- **Which warehouse backs the Virtual Graph.** It is `vg demo sql warehouse`, id
  `b0fffb8e3255bf85` (confirmed in Phase 0), not the `DATABRICKS_WAREHOUSE_ID` in `.env`
  (that one only built the Silver tables). Every query the Virtual Graph runs becomes SQL on
  this warehouse. Filter all `system.query.history` pulls on this id.
- **The current warehouse size.** Record it at the start of each phase so the results are
  labeled with the size they ran on.

### What the monitor agent does, on a loop

- **Poll warehouse state** with the `manage_sql_warehouse` MCP tool every 10 to 15
  seconds. Record size, state (running, starting, scaled), cluster count, and whether the
  warehouse auto-scaled up under load.
- **Pull query history from the `system.query.history` system table** with `execute_sql`,
  filtered to the backing warehouse and the test time range. Phase 0 found this table lags
  about 11 minutes, so do not expect a test's rows immediately: pull at the end of all phases
  (or after a >12 min wait), and rely on client wall-clock for real-time results. The history
  pull is confirmatory detail, not the primary metric. For each query capture:
  - `statement_id` and `execution_status`, so each Databricks row maps to a client run and
    failed runs are visible (see [run status](#failure-and-partial-result-handling))
  - total duration, plus the split between compilation time and execution time
  - rows produced and rows read
  - bytes read and bytes spilled to disk
  - whether the query queued before it ran
  - Photon usage, if any
- **Capture cost per phase.** Record the warehouse size's DBU/hr rate and pull DBU consumed
  for the warehouse over the phase window from `system.billing.usage`, so the comparison is
  price-performance and not just wall-clock. A larger warehouse that is twice as fast but
  four times the DBU/hr is a loss. Phase 0 confirms exactly which cost fields are populated.
- **Flag bottlenecks** from those metrics:
  - **Memory pressure:** any bytes spilled to disk means the query ran out of memory and
    spilled. A larger warehouse should reduce or remove spill. (Phase 0 note: the source is
    one ~4 MB file, so spill is not expected to occur at all; a non-zero value would be a
    surprise worth investigating.)
  - **CPU or scan floor:** high execution time with no spill and few rows produced points
    at a raw scan-and-aggregate floor that warehouse size should lower.
  - **Queueing or pool saturation:** a query that queued, or a client-side
    `HikariPool-1 ... Connection is not available` error, means the pool or the warehouse
    was saturated, not that the query itself was slow.
  - **Data-movement bound:** large rows produced with a wall-clock time well above the
    Databricks execution time means the cost is shipping rows back to the graph engine,
    which warehouse size does not fix.
- **Write a running log** with one row per test: phase, warehouse size, query label,
  client wall-clock time, Databricks execution time, rows produced, bytes spilled, and the
  bottleneck flag.

### Suggested query-history pull

After each test, the monitor agent runs something like this through `execute_sql`,
adjusting the warehouse id and time window:

```sql
SELECT
  statement_id,
  execution_status,
  statement_text,
  total_duration_ms,
  compilation_duration_ms,
  execution_duration_ms,
  produced_rows,
  read_rows,
  read_bytes,
  spilled_local_bytes,
  waiting_for_compute_duration_ms,
  start_time
FROM system.query.history
WHERE compute.warehouse_id = '<backing-warehouse-id>'
  AND start_time >= '<test-start-timestamp>'
ORDER BY start_time DESC
```

A non-zero `spilled_local_bytes` is the memory-pressure signal. A non-zero
`waiting_for_compute_duration_ms` is the queueing signal. An `execution_status` other than
`FINISHED` marks a failed or cancelled run.

Because the agent runs one query at a time and the warehouse is isolated, ordering this
result by `start_time` lines the rows up with the client runs in sequence: the first N rows
after a test start are that query's N runs. Phase 0 confirms this correlation holds and
whether a more robust key (a distinctive literal carried through to `statement_text`) is
needed.

---

## Capture the generated SQL first, with EXPLAIN

Every phase starts the same way: before timing any query, run it once with `EXPLAIN`
prefixed and log the SQL Aura pushes to Databricks. `EXPLAIN` returns the query plan with
the generated SQL and does not run the query, as shown in
[`virtual-graph.md`](virtual-graph.md#6-inspect-your-graph).

How to capture it:

- Run each query with `EXPLAIN` in front in the Aura Workspace Query tab, which renders the
  plan and the generated SQL. Through `vg-probe` and the driver, `EXPLAIN` returns the plan
  in the result summary rather than as result rows, so the Query tab is the reliable place
  to read the SQL.
- Log, per query: the query label, the generated SQL, and whether the plan shows a
  post-processing or materialize step. That step is the pushdown-versus-materialize signal
  from [`best-practices.md`](best-practices.md#what-governs-performance).

Why do this first:

- It confirms each query pushes down as expected before you trust its timing.
- The generated SQL should be identical across the 2X-Small and Small warehouse. Warehouse
  size changes how fast that SQL runs, not the SQL itself, so capturing it once per phase
  documents that the comparison is apples to apples.

For the GDS set, run `EXPLAIN` on the sizing count query to capture its windowed-scan SQL.
The `gds.graph.project` and `gds.pageRank.stream` statements are GDS operations rather than
pushdown SQL, so `EXPLAIN` will not produce warehouse SQL for them; note that and move on.

---

## Between phases: restart and resize

The primary agent owns the warehouse between phases and automates the whole boundary; there
is no human operator. After each phase it stops the warehouse to clear its caches, sets the
size for the next phase, starts it again, and polls until it reports `RUNNING`. Stopping and
restarting at every boundary makes the comparison cold-start to cold-start, so a timing
difference is the size, not leftover cache. Restart is the only reliable cache clear: the
warehouse's local disk/IO cache survives across sessions on the same cluster and has no SQL
clear command, so a stop/start is the only way to force a cold scan.

### Automated restart-and-resize procedure

At each phase boundary the primary agent runs this against the backing warehouse
`b0fffb8e3255bf85`:

1. **Stop** the warehouse, which clears its caches:
   ```bash
   databricks warehouses stop b0fffb8e3255bf85
   ```
2. **Set the size** for the next phase with the `manage_sql_warehouse` MCP tool while the
   warehouse is stopped, so it comes back up cold at the new size:
   ```
   manage_sql_warehouse(action="modify", warehouse_id="b0fffb8e3255bf85", size="<2X-Small|Small>")
   ```
3. **Start** it and poll until the state is `RUNNING`:
   ```bash
   databricks warehouses start b0fffb8e3255bf85
   databricks warehouses get   b0fffb8e3255bf85   # repeat until state == RUNNING
   ```

Only once the warehouse reports `RUNNING` at the target size does the primary agent begin the
next phase. The monitor agent, already polling warehouse state, records the new size at the
start of every phase.

| After | Restart the warehouse, then set it to | For |
|-------|---------------------------------------|-----|
| Phase 0 (spike, 2X-Small) | **2X-Small** | Phase 1 |
| Phase 1 (set A, 2X-Small) | **Small** | Phase 2 |
| Phase 2 (set A, Small) | **2X-Small** | Phase 3 |
| Phase 3 (set B, 2X-Small) | **Small** | Phase 4 |
| Phase 4 (set B, Small) | **2X-Small** | Phase 5 |
| Phase 5 (set C, 2X-Small) | **Small** | Phase 6 |
| Phase 6 (set C, Small) | nothing — tests are done; stop the warehouse | — |

Each phase therefore begins on a freshly restarted warehouse at the stated size. The primary
agent polls until the warehouse reports `RUNNING` before it starts the phase. The monitor
agent records the size at the start of every phase.

---

## Phase 0: instrumentation spike

Run this once, on the 2X-Small warehouse, before any timed test. It is cheap (tiny queries,
tiny result sets) and exists only to confirm what can actually be measured and to settle the
open methodology questions. Record a short findings note from it, then adjust the recording
tables below if anything turns out not to be capturable.

The spike resolves these questions:

- **Can per-query metrics be captured for a tiny query, and do they correlate to the client
  run?** Run `uv run vg-probe "RETURN 1 AS ok"` and a tiny windowed fan-out query (the A1
  shape), then pull `system.query.history` for the backing warehouse. Confirm `statement_id`,
  `execution_status`, the duration split, rows, bytes, and `spilled_local_bytes` are
  populated, and confirm that ordering by `start_time` lines the history rows up with the
  client runs. If ordering is ambiguous, test carrying a distinctive numeric literal in the
  Cypher `WHERE` (for example an unusual cutoff constant) and check whether it survives into
  `statement_text` as a reliable match key.
- **What table-layout information can be captured?** Run `DESCRIBE DETAIL` and
  `get_table_stats_and_schema` (`table_stat_level=DETAILED`) on the `TRANSFERRED_TO` source
  table. Record row count, file count and sizes, and especially whether the table is
  partitioned or liquid-clustered on `transfer_timestamp` and whether column stats exist.
  This determines whether the A1-A4 windows prune or full-scan, which changes what "the scan
  floor lowers with size" actually means.
- **Can cost / DBU be captured?** Confirm `system.billing.usage` has rows for the backing
  warehouse, find the DBU/hr rate for 2X-Small and Small, and confirm DBU can be attributed
  to a phase time window. If billing rows lag, note the lag so cost is pulled after a delay.
- **How is wall-clock composition recorded?** The `RETURN 1` probe measures the client +
  driver + Bolt round-trip floor with no warehouse work. For the tiny windowed query, record
  client wall-clock (from `vg-probe`) next to Databricks `total_duration_ms` and
  `execution_duration_ms` for the same `statement_id`. The gap, minus the `RETURN 1` floor,
  is the Aura-plus-data-movement overhead. Also record where the client runs and its network
  path to Aura. This fixes the columns used to judge whether A5 is data-movement bound.
- **Does the result cache fire on reruns?** After a fresh restart, run the tiny windowed
  query five times back to back. Check whether runs 2-5 return near-zero
  `execution_duration_ms` or are flagged as served from cache. If they are, decide the
  handling: disable the result cache if `SET use_cached_result = false` can be pushed through
  the Aura path, or otherwise treat run 1 as the only warehouse measurement and the rest as
  cache hits. This is the decision deferred from the run-count design.
- **Confirm the warm-up query.** `RETURN 1 AS ok` is the per-phase warm-up: it brings compute
  up after a restart and exercises the driver and Bolt path, but reads none of the transfer
  data, so run 1 of the timed queries is still cold on data. Confirm it does not prime the
  disk cache for the test tables.

Phase 0 output: a findings block recording, per question above, what was capturable and any
adjustment to the recording tables or methodology. Only after that do the timed phases run.

**Phase 0 has been run (2026-06-09).** Its findings live in
[`perf-tests-results.md` → Phase 0 findings](perf-tests-results.md#phase-0-findings-run-2026-06-09).
The headline: the backing warehouse is `b0fffb8e3255bf85`; the live source tables are single
~4 MB files (too small for warehouse size to matter, which is why Test set C exists);
`system.query.history` lags ~11 minutes; and the result cache does not short-circuit the Aura
path. The plan below already reflects those conclusions.

---

## Test set A: Pattern 6, "keep result sets small"

Pattern 6 says every result row travels back over the wire, and that the all-time fan-out
pair query returned 222,966 rows in about 24.8s while the 7-day window returned 22,096
rows in about 3.5s. That makes the fan-out pair query the ideal Pattern 6 probe: same
query shape, result set size set only by the time window.

These five queries run the fan-out pair query at five growing windows. All five push down
cleanly, so the only thing changing is how many rows come back. The open question is
whether a larger warehouse moves the floor on the row-heavy ones, or whether they are
bound by data movement that warehouse size cannot fix.

Run each with `vg-probe`, which times a single statement and never abandons it. Run each
query **five times** and keep all five timings:

```bash
cd virtual-graph-demo
uv run vg-probe "<cypher>"
```

Once per phase, run the `RETURN 1` probe to record the client-floor baseline for the
wall-clock-composition column (Phase 0 found it stays ~0.2-0.3 s and never reaches the
warehouse, so it is a baseline, not a warm-up):

```bash
uv run vg-probe "RETURN 1 AS ok"
```

Do not run a warehouse warm-up: run 1 of each query is the intended cold sample (cold cluster
plus first read of the file after the phase restart), and runs 2-5 are warm.

The data ends 2024-03-30, so windows anchor on a fixed cutoff near the end of the data,
not on today. The five queries, smallest result set to largest:

- **A1, last 1 day** (cutoff `2024-03-29T23:58:00Z`)
- **A2, last 3 days** (cutoff `2024-03-27T23:58:00Z`)
- **A3, last 7 days** (cutoff `2024-03-23T23:58:00Z`, the known ~22,096-row baseline)
- **A4, last 30 days** (cutoff `2024-02-29T23:58:00Z`)
- **A5, all time** (no `WHERE`, the ~222,966-row case)

The windowed query, A1 through A4 (swap the cutoff per row above):

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
WITH src.account_id AS sender, dst.account_id AS recipient,
     count(t) AS pair_transfers, sum(t.amount) AS pair_outflow
RETURN sender, recipient, pair_transfers, pair_outflow
```

The all-time query, A5 (drop the `WHERE`):

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WITH src.account_id AS sender, dst.account_id AS recipient,
     count(t) AS pair_transfers, sum(t.amount) AS pair_outflow
RETURN sender, recipient, pair_transfers, pair_outflow
```

### Phase 1: Test set A on the 2X-Small warehouse

- Primary agent has restarted the warehouse and set it to **2X-Small**, and confirms it
  reports `RUNNING` (see [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the size and starts its loop.
- Primary agent runs the `RETURN 1` probe once for the client-floor baseline (not a warm-up).
- Primary agent runs each of A1 through A5 with `EXPLAIN` first and logs the generated SQL
  and plan shape per query; see [Capture the generated SQL first](#capture-the-generated-sql-first-with-explain).
- Primary agent then runs A1, A2, A3, A4, A5 in order, one at a time, **five times each**,
  recording wall-clock time and row count for every run. Record any failed run rather than
  dropping it; see [Failure and partial-result handling](#failure-and-partial-result-handling).
- Monitor agent pulls query history after each and flags any spill, queueing, or
  data-movement bound, and records DBU consumed for the phase.
- **Primary agent restarts the warehouse and sets it to Small for Phase 2; see
  [Between phases](#between-phases-restart-and-resize).**

### Phase 2: Test set A on the Small warehouse

- Primary agent has restarted the warehouse and set it to **Small**, and confirms it
  reports `RUNNING` (see [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the new size.
- Primary agent runs the `RETURN 1` probe once for the client-floor baseline (not a warm-up).
- Primary agent runs each of A1 through A5 with `EXPLAIN` first and logs the generated SQL
  and plan shape per query; see [Capture the generated SQL first](#capture-the-generated-sql-first-with-explain).
  Confirm the SQL matches phase 1, so the comparison is apples to apples.
- Primary agent then reruns A1 through A5 in the same order, **five times each**.
- Compare phase 2 timings against phase 1, per query, on min / median / max and on cost.
  Expect the largest gains on the scan-bound queries with little spill, and small gains on
  the row-heavy A5 if it turns out to be data-movement bound.
- **Primary agent restarts the warehouse and sets it to 2X-Small for Phase 3.**

---

## Test set B: the fast-gds path

The `fast-gds` demo sizes a window, provisions a GDS Session, projects the windowed
transfer subgraph, streams PageRank, and drops. The known-good run was the last 1.5 hours:
233 transfers, 233 edges, and a successful full path in about 128.8s, almost all of it
session provisioning rather than the Databricks query.

This set keeps the 1.5-hour window and adds two larger windows that project about 1,000
and about 2,000 edges. Because the dominant cost is session cold-start and the Databricks
pull is sub-second, the working hypothesis is that warehouse size barely moves GDS timing.
The test confirms that, and watches for the 60s Bolt read timeout that larger projections
can trip.

### Find the windows that hit 1,000 and 2,000 edges

The edge count is the row count of the window. Size windows for free with `--count-only`
before provisioning anything:

```bash
uv run vg-demo --demo fast-gds --since-hours 1.5 --count-only
uv run vg-demo --demo fast-gds --since-hours 6 --count-only
uv run vg-demo --demo fast-gds --since-hours 12 --count-only
```

Sweep `--since-hours` until one window reports close to 1,000 edges and another close to
2,000. Record the two hour values; those become B2 and B3.

### The three GDS tests

- **B1, last 1.5h, ~233 edges** (the known-good baseline)
  ```bash
  uv run vg-demo --demo fast-gds --since-hours 1.5
  ```
- **B2, the window that sizes to ~1,000 edges**
  ```bash
  uv run vg-demo --demo fast-gds --since-hours <found-value>
  ```
- **B3, the window that sizes to ~2,000 edges**
  ```bash
  uv run vg-demo --demo fast-gds --since-hours <found-value>
  ```

Run each of B1, B2, B3 **three times** and keep all three. For each run, record the four
timings the demo already prints: size window, project (the session provisioning step),
PageRank stream, and drop. The project step is where the cost lives. Report min / median /
max per step across the three runs.

If B2 or B3 trips the 60s Bolt read timeout during provisioning, that is a recorded outcome,
not a silent failure: note the timeout, then extend it and rerun that test (see
[Failure and partial-result handling](#failure-and-partial-result-handling)):

```bash
uv run vg-demo --demo fast-gds --since-hours <found-value> --read-timeout 300
```

### Phase 3: Test set B on the 2X-Small warehouse

- Primary agent has restarted the warehouse and set it to **2X-Small**, and confirms it
  reports `RUNNING` (see [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the size and starts its loop.
- Primary agent runs the `--count-only` sweep to fix the B2 and B3 windows.
- Primary agent runs the B1, B2, B3 sizing count query with `EXPLAIN` first and logs the
  windowed-scan SQL per window; see
  [Capture the generated SQL first](#capture-the-generated-sql-first-with-explain). The
  `project` and PageRank steps are GDS operations, not pushdown SQL, so they have no
  warehouse SQL to capture.
- Primary agent then runs B1, B2, B3 one at a time, **three times each**, recording the
  four printed timings for every run and any failed or timed-out run.
- Monitor agent pulls query history for the sizing and projection pulls, and notes that
  the Databricks pull is sub-second so most of the project time is Aura session cold-start.
- **Primary agent restarts the warehouse and sets it to Small for Phase 4.**

### Phase 4: Test set B on the Small warehouse

- Primary agent has restarted the warehouse and set it to **Small**, and confirms it
  reports `RUNNING` (see [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the new size.
- Primary agent runs the B1, B2, B3 sizing count query with `EXPLAIN` first and logs the
  windowed-scan SQL per window; see
  [Capture the generated SQL first](#capture-the-generated-sql-first-with-explain).
  Confirm the SQL matches phase 3.
- Primary agent reuses the same B2 and B3 windows from phase 3 and reruns B1, B2, B3,
  **three times each**.
- Compare the size-window and project timings against phase 3 on min / median / max. Expect
  the sizing count to drop slightly on the larger warehouse and the project step to stay
  about the same, confirming session provisioning, not the warehouse, is the dominant GDS
  cost.
- **Primary agent restarts the warehouse and sets it to 2X-Small for Phase 5.**

---

## Test set C: a materially larger, partitioned table

Phase 0 found the live `account_links` is one ~4 MB file, too small for warehouse size to
matter. Test set C builds a much larger, partitioned copy and runs a **compute-bound**
query against it: heavy scan and aggregate, small result. That isolates
[cost center 1, where the math happens](best-practices.md#what-governs-performance), which
is the cost a larger warehouse actually lowers. Unlike A5, set C does not ship many rows
back, so it is not data-movement bound. This is the one set where 2X-Small and Small are
expected to differ.

### Prerequisite: build the large table and map it (operator, one-time)

The table must satisfy three things, or warehouse size still will not move:

- **Large row count.** Start at about 100M rows and scale up until the 2X-Small warehouse
  shows non-zero `spilled_local_bytes` or a clearly long scan; without that there is no
  compute pressure to relieve.
- **High distinct `(src_account_id, dst_account_id)` cardinality.** Generate fresh random
  pairs, do not just replicate the existing 300k rows. Replicas keep the group count at
  ~288k, so the hash aggregate stays tiny and never spills. Random pairs over the 24k
  accounts push distinct pairs into the tens of millions.
- **Partitioned on the transfer date**, so the windowed C2 query can test pruning.

A representative build (tune the row count; sample real `account_id`s so no edge is silently
dropped at query time):

```sql
CREATE OR REPLACE TABLE `graph-on-databricks`.`graph-enriched-schema`.`account_links_large`
USING DELTA
PARTITIONED BY (transfer_date)
AS
WITH ids AS (
  SELECT account_id, (row_number() OVER (ORDER BY account_id)) - 1 AS idx
  FROM `graph-on-databricks`.`graph-enriched-schema`.`accounts`
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
                      CAST(rand(5) * 24 AS INT), CAST(rand(6) * 60 AS INT), 0) AS transfer_timestamp
  FROM range(0, 100000000)            -- target row count; raise until 2X-Small spills
)
SELECT g.link_id, s.account_id AS src_account_id, d.account_id AS dst_account_id,
       g.amount, g.transfer_timestamp, CAST(g.transfer_timestamp AS DATE) AS transfer_date
FROM gen g
JOIN ids s ON s.idx = g.src_idx
JOIN ids d ON d.idx = g.dst_idx
```

Then expose it to the graph. In the Aura model editor, map a new relationship exactly like
`TRANSFERRED_TO` in [`virtual-graph.md`](virtual-graph.md#5-define-your-schema), but from
`account_links_large`:

- **Relationship type** `TRANSFERRED_TO_BIG`; properties `link_id`, `amount`,
  `transfer_timestamp`; id `link_id`.
- **From** `Account` ID `account_id` mapped from `src_account_id`; **To** `Account` ID
  `account_id` mapped from `dst_account_id`.
- Add a `range` index on `TRANSFERRED_TO_BIG.transfer_timestamp` for the C2 window.

This adds a relationship and leaves the original `TRANSFERRED_TO` graph untouched, so sets A
and B still work. (The alternative, replacing the contents of `account_links` in place, would
change sets A and B and is not recommended.)

### The set C queries

Run each with `vg-probe`, `EXPLAIN` first, and confirm the plan pushes the aggregate down
with no materialize step before trusting the timing.

- **C1, full-table scalar aggregation** — the primary size probe. Scans every row, groups by
  one scalar, returns ~24k rows:
  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO_BIG]->(:Account)
  WITH src.account_id AS account_id,
       count(t) AS transfers, sum(t.amount) AS outflow,
       avg(t.amount) AS avg_amount, max(t.amount) AS max_amount
  RETURN account_id, transfers, outflow, avg_amount, max_amount
  ```
- **C2, windowed scalar aggregation** — same shape with a `transfer_timestamp` cutoff on a
  partition boundary, to test whether pruning plus warehouse size compound (cutoff
  `2024-03-23T23:58:00Z` prunes to roughly the last week of partitions):
  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO_BIG]->(:Account)
  WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
  WITH src.account_id AS account_id, count(t) AS transfers, sum(t.amount) AS outflow
  RETURN account_id, transfers, outflow
  ```
- **C3, high-cardinality pair aggregation (optional, spill stress)** — groups by the
  `(sender, recipient)` pair, so the hash aggregate state is large and most likely to spill on
  2X-Small. `ORDER BY ... LIMIT` keeps the result small, but confirm via `EXPLAIN` that the
  sort and limit push down; if the plan materializes, this is measuring the slow path, not
  warehouse scan throughput, and should be reported as such:
  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO_BIG]->(dst:Account)
  WITH src.account_id AS sender, dst.account_id AS recipient,
       count(t) AS pair_transfers, sum(t.amount) AS pair_outflow
  RETURN sender, recipient, pair_transfers, pair_outflow
  ORDER BY pair_outflow DESC
  LIMIT 100
  ```

### Phase 5: Test set C on the 2X-Small warehouse

- Operator confirms `account_links_large` exists and `TRANSFERRED_TO_BIG` is mapped (this is
  the one-time table build, the only manual prerequisite). Primary agent has restarted the
  warehouse and set it to **2X-Small**, and confirms it reports `RUNNING` (see
  [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the size and starts its loop.
- Primary agent runs the `RETURN 1` probe once for the client-floor baseline.
- Primary agent runs C1, C2, and (optionally) C3 with `EXPLAIN` first, logging the generated
  SQL and confirming each pushes down with no materialize step.
- Primary agent then runs C1, C2, (C3) one at a time, **five times each**, recording
  wall-clock and row count per run, and any failed run.
- Monitor agent pulls query history (after the ingestion lag) and records execution time,
  rows read, and especially `spilled_local_bytes` — for set C, spill is a real possibility and
  is the key signal.
- **Primary agent restarts the warehouse and sets it to Small for Phase 6.**

### Phase 6: Test set C on the Small warehouse

- Primary agent has restarted the warehouse and set it to **Small**, and confirms it reports
  `RUNNING` (see [Between phases](#between-phases-restart-and-resize)).
- Monitor agent records the new size.
- Primary agent runs the `RETURN 1` probe once for the client-floor baseline.
- Primary agent runs C1, C2, (C3) with `EXPLAIN` first; confirm the SQL matches phase 5.
- Primary agent then reruns C1, C2, (C3), **five times each**.
- Compare phase 6 against phase 5 on min / median / max, on `spilled_local_bytes`, and on
  cost. This is where a real warehouse-size win should appear: a lower scan-and-aggregate
  floor and reduced or removed spill on Small. If even here the times are flat with no spill,
  the honest conclusion is that warehouse size does not help this Virtual Graph workload.
- **Tests are done; the primary agent stops the warehouse**
  (`databricks warehouses stop b0fffb8e3255bf85`).

---

## Recording the results and work log

All results, the EXPLAIN log, the chronological work log, and the per-phase summaries are
captured in [`perf-tests-results.md`](perf-tests-results.md), not here. That file holds the
result-table templates for sets A, B, and C, the cost table, and the Phase 0 findings. This
section defines *how* to record; the results file holds *what* was recorded.

**Logging protocol** (full version in
[`perf-tests-results.md` → How to use this file](perf-tests-results.md#how-to-use-this-file-logging-protocol)):
for each test, **before** running it, log a STARTING entry with the query and its `EXPLAIN`
outcome — log what is running before it runs. Then run it (five times for sets A and C, three
for set B), **fill the matching result row**, and log a RESULT entry with min / median / max,
rows, spill, and status. **At the end of each phase, log a phase summary.**

Record **min / median / max** across the runs rather than a single time, so a warehouse-size
effect is separated from run-to-run noise. Each set's table compares 2X-Small against Small so
the comparison reads straight across.

Keep an **EXPLAIN log**: per query, the generated SQL and whether the plan showed a
post-processing or materialize step. Record it once per phase and confirm the SQL is identical
between the 2X-Small and Small phases, so any timing difference is the warehouse, not a
different query plan.

**Record wall-clock composition too.** For each query, keep the client wall-clock from
`vg-probe` next to the Databricks `total_duration_ms` and `execution_duration_ms` for the
matching `statement_id`. The gap between client wall-clock and Databricks total, minus the
`RETURN 1` floor measured in Phase 0, is the Aura-plus-data-movement overhead. That gap is
what tells A5 apart as data-movement bound versus scan bound.

### Failure and partial-result handling

A failed run is data, not a blank cell. For every run, record an outcome alongside the
timing:

- **OK** — completed; keep the timing.
- **ERROR** — the query errored. Record the client error text and the Databricks
  `execution_status` and error message for that `statement_id`.
- **TIMEOUT** — a client read timeout (set B can trip the 60s Bolt timeout during
  provisioning). Note it, rerun with `--read-timeout 300`, and record both the timed-out
  attempt and the extended rerun. `vg-probe` itself has no client timeout, so set A does not
  client-timeout, but the warehouse can still error or spill.
- **QUEUED / POOL-SATURATED** — a non-zero `waiting_for_compute_duration_ms`, or a
  client-side `HikariPool-1 ... Connection is not available` error. This is a pool or
  warehouse saturation signal, not a slow query.

Compute min / median / max over the **OK** runs only, and record the count of OK runs per
cell so a thin sample is visible. If every run of a query fails, the cell reads the failure
mode, not a time.

### What a good result looks like

- **Test set A:** given the Phase 0 finding that the source is one ~4 MB file, the most
  likely outcome is that 2X-Small and Small are about equal: there is no scan floor to lower
  and no spill to remove. The real driver across A1-A5 is rows shipped back over the wire, so
  A5 (~223,000 rows) should be the slowest on both sizes, and the right lever is Pattern 6:
  shrink the window, not grow the warehouse. A near-flat A-vs-size result is the expected,
  informative outcome. Read it against cost: any small speed-up that doubles DBU is not a win.
- **Test set B:** timings should barely move between 2X-Small and Small, because the
  bottleneck is Aura session provisioning, not the Databricks query. A flat result here is
  the expected and informative outcome.
- **Test set C:** this is where a warehouse-size effect should finally show. On a table large
  enough to create scan-and-aggregate pressure, Small should beat 2X-Small on C1 and C2 and
  should reduce or remove the spill C3 provokes on 2X-Small. If C still comes out flat with no
  spill, raise the table size and rerun; if it stays flat even on a clearly large, spilling
  table, the honest conclusion is that warehouse size does not help this Virtual Graph
  workload, and the lever is query shape (Pattern 6 and friends), not the machine.
