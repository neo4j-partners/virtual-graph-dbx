# Performance Test Results and Work Log

Companion to [`perf-test.md`](perf-test.md). The plan defines **what** to run and how; this
file captures **what was run and what came back**. Keep the methodology in the plan and every
result, timing, and log entry here. This file is written to stand alone: the next section
defines the system, the data, and the workloads, so the findings can be read without the plan.

---

## What was tested: the system, the data, and the workloads

**The question.** Does resizing the Databricks SQL warehouse from its smaller size (2X-Small)
one step up (Small) make these queries faster? Each of the three workloads (defined below) runs
once per size: Phases 1–2 are Workload A on 2X-Small then Small, Phases 3–4 are Workload B on
the same two sizes, and Phases 5–6 are Workload C.

**The data.** A synthetic finance dataset in Unity Catalog, schema
`graph-on-databricks`.`graph-enriched-schema`: an `accounts` table and an `account_links` table
of 300,000 account-to-account money transfers. In graph terms each account is a node and each
transfer is a `TRANSFERRED_TO` relationship between two accounts. The transfer timestamps span
2024-01-01 to 2024-03-30, and the whole transfers table is a single ~4 MB file. That size matters:
it is far too small to ever strain a warehouse, which is what motivated Workload C.

**How the tests vary the load.** Every test filters the transfers by time. A "7-day window" means
the query keeps only transfers from the last 7 days of the dataset, which ends 2024-03-30. The
filter is a `WHERE transfer_timestamp >= <cutoff>` pushed down to the warehouse. Windows count back
from the data's latest timestamp, not from today. A wider window includes more transfers and
therefore returns more result rows; "all time" means no filter.

**The three workloads:**

- **Workload A, the "who paid whom" query (tests A1–A5):** a Cypher aggregation that, for a time
  window, lists every sender → recipient pair with how many transfers and how much money flowed
  between them. Aura pushes the whole thing down as a single SQL group-by, so the warehouse does
  the aggregation and Neo4j ships the result rows back to the client. A1 filters to the data's
  last 1 day and A5 applies no filter, so the result grows from ~3,300 rows to ~223,000.
- **Workload B, PageRank (tests B1–B5):** ranks accounts by their importance in the money-flow
  graph. It first builds an in-memory graph from the window's transfers inside a Neo4j GDS
  Session, a separate piece of Neo4j compute that is not the warehouse, then runs the PageRank
  algorithm on it. Each run has four steps: count the relationships (size), build the in-memory
  graph (project), rank and return the top 10 accounts (stream), and clean up (drop). Windows are
  hours here rather than days, chosen to hit target relationship counts: B1 covers the data's
  last 1.5 hours, about 233 relationships, up to B5 at about 10,000.
- **Workload C, the warehouse stress test (tests C1–C3):** group-by queries over a purpose-built
  ~100M-row table. It was added after Phase 0 showed the real dataset was too small to ever stress
  the warehouse; this is the workload designed to make warehouse size matter, with memory spill as
  the metric to watch.

---

## Summary: what this measures and what we found

**What this measures:** whether moving the Databricks SQL warehouse from its smaller size
(2X-Small) up one size (Small) makes Neo4j Aura Virtual Graph queries faster.

**What we found so far:** For both workloads tested, the slow part is on the Neo4j / Aura side,
not on Databricks. The warehouse does its share of the work in about 0.2 seconds either way, with
no memory pressure, so a bigger warehouse has nothing to speed up.

- **Result-set size, not warehouse size, sets the wait time for the transfer-pairs query
  (Workload A).** The time is almost entirely spent sending the result rows back to the client, so
  it grows with the number of rows returned. To make it faster, narrow the time window so fewer
  rows come back.
- **PageRank (Workload B) is limited by Neo4j's graph-build step and becomes unusable near 5,000
  relationships.** The time is almost entirely spent building the in-memory graph inside Neo4j
  before the ranking can run, and it grows steeply with the number of transfer relationships in
  the window: about 2,000 relationships took ~6 minutes, and about 5,000 never finished within
  33 minutes. Warehouse size makes no difference.
- **Even a 100M-row aggregation never stressed the warehouse (Workload C spike).** This group-by
  workload over a large (~100M-row) table was built and run as a spike on the 2X-Small to find
  the table size where a bigger warehouse would finally help. That size did not
  appear. The warehouse aggregated the full 100M-row, ~1 GB table in about 5 seconds with **zero
  spill**, even on the high-cardinality pair group-by with ~95M distinct groups. The only query
  with any real compute cost was that pair group-by (about 0.5s at 1M rows rising to ~5s at 100M);
  the others stayed sub-second. The 100M rows are scanned and aggregated inside the warehouse,
  not returned: C1/C2 produce at most ~25,000 group rows, C3 has a `LIMIT 100`, and the spike
  wrapped each query in an outer `count(*)` so no result rows were shipped at all. With no spill
  to remove, a bigger warehouse has at most a few
  seconds of scan-and-aggregate floor to lower. The formal 2X-Small versus Small comparison
  (Phases 5–6) and the Cypher pushdown path remain to be run, but the precondition that justified
  them, spill on the smaller warehouse, is absent. Detail in
  [`perf-tests-results-v2.md`](perf-tests-results-v2.md).
- **This confirms the best-practices guide's Pattern 6.** Workload A, which runs on the live
  300,000-row `account_links` table, not the 100M-row Workload C table, reproduced the guide's
  data-movement numbers almost exactly: with no time
  filter it returned 222,966 rows in ~27 s (the 300k transfers group into 222,966 distinct
  sender → recipient pairs, all shipped back to the client), while filtering to the data's last
  7 days cut that to 22,096 rows in ~3 s. Fewer rows returned means a proportionally shorter wait, so "keep result
  sets small" is the right lever. Warehouse size, by contrast, moved nothing in Phases 1–4, and
  the Workload C spike showed even a 100M-row aggregation finishing in seconds without spill.
  Warehouse size only helps a genuinely scan- or spill-bound query, which this data never
  produces.

---

## Results tables

Client wall-clock is reported in seconds, min / median / max over the runs (run 1 cold). Spill
and Databricks-exec columns are filled from `system.query.history` after the ~11 min lag.

### Test set A: fan-out pair query at growing windows (5 runs each)

This is the "who paid whom" query (Workload A): for a time window, list sender → recipient pairs
with how many transfers and how much money flowed between them. Each row is one time filter, from
A1, which keeps only transfers from the data's last 1 day, through A5, which applies no filter
(all time); wider windows include more transfers and so return more result rows.

Each warehouse column holds **client wall-clock in seconds** as `min / median / max` over the 5
runs (run 1 cold). The remaining columns are:

* **Rows produced**: rows returned by the query.
* **DB exec ms**: median Databricks-side `total_duration_ms` from `system.query.history`.
* **Spill**: `spilled_local_bytes` (0 = none).
* **OK runs**: successful runs out of 5.
* **2XS / S**: 2X-Small value / Small value.

| Test | Time filter (data's last N days) | Rows produced | 2X-Small wall-clock, sec (min/med/max) | Small wall-clock, sec (min/med/max) | DB exec, ms (med, 2XS / S) | Spill, bytes (2XS / S) | OK runs (2XS / S) | Bottleneck |
|------|--------|---------------|------------------------|---------------------|---------------------------|-----------------|-------------------|------------|
| A1 | last 1 day | 3,303 | 0.9 / 1.0 / 1.2 | 1.0 / 1.0 / 2.0 | 208 / 286 | 0 / 0 | 5 / 5 | data-movement (small) |
| A2 | last 3 days | 9,630 | 1.5 / 1.6 / 2.4 | 1.6 / 1.6 / 1.9 | 219 / 252 | 0 / 0 | 5 / 5 | data-movement |
| A3 | last 7 days | 22,096 | 2.9 / 3.1 / 3.7 | 2.9 / 3.0 / 3.4 | 191 / 237 | 0 / 0 | 5 / 5 | data-movement |
| A4 | last 30 days | 85,490 | 9.4 / 11.0 / 12.0 | 10.7 / 11.4 / 11.7 | 232 / 228 | 0 / 0 | 5 / 5 | data-movement |
| A5 | all time (no filter) | 222,966 | 25.1 / 26.6 / 29.1 | 25.1 / 25.7 / 27.3 | 224 / 225 | 0 / 0 | 5 / 5 | data-movement (DB ~0.2s vs client ~27s) |

### Test set B: fast-gds path (3 runs each, four printed steps)

This is the PageRank workload (Workload B): build an in-memory graph from a window's transfers,
then rank the accounts. The windows here are hours rather than days, counted back from the data's
latest timestamp of 2024-03-30 23:58Z, and were chosen to hit target relationship counts: B1
covers the data's last 1.5 hours, about 233 relationships, up to B5 at about 10,000. Each window
runs as four steps; the **build (project)** step is the one that dominates the time.

Each warehouse column holds **the duration of that one step in seconds**, as `min / median / max`
over the OK runs. The four steps per window are:

* **size**: count edges in the window.
* **project**: provision the GDS Session + project the subgraph (the dominant step).
* **stream**: stream top-10 PageRank.
* **drop**: drop the projection.

The remaining columns are:

* **OK runs**: successful runs out of 3.
* **2XS / S**: 2X-Small value / Small value.

See notes below on read-timeout.

| Test | Edges (time filter: data's last N hours) | Step | 2X-Small step time, sec (min/med/max) | Small step time, sec (min/med/max) | OK runs (2XS / S) |
|------|----------------|------|------------------------|---------------------|-------------------|
| B1 | 233 (1.5h) | size | 0.4 / 0.4 / 0.4 | 0.4 / 0.4 / 0.6 | 3 / 3 |
| B1 | 233 (1.5h) | project | 87.6 / 88.3 / 105.6 | 85.6 / 91.1 / 91.7 | 3 / 3 |
| B1 | 233 (1.5h) | stream | 2.6 / 2.8 / 2.9 | 2.5 / 3.6 / 3.8 | 3 / 3 |
| B1 | 233 (1.5h) | drop | 0.8 / 0.8 / 0.8 | 0.8 / 0.8 / 0.8 | 3 / 3 |
| B2 | 986 (7h) | size | 0.3 / 0.4 / 0.4 | 0.3 / 0.3 / 0.4 | 3 / 3 |
| B2 | 986 (7h) | project | 221.4 / 226.7 / 230.1 | 188.7 / 203.4 / 209.1 | 3 / 3 |
| B2 | 986 (7h) | stream | 2.6 / 2.8 / 2.9 | 2.5 / 2.6 / 3.7 | 3 / 3 |
| B2 | 986 (7h) | drop | 0.8 / 0.8 / 1.1 | 0.8 / 0.8 / 1.1 | 3 / 3 |
| B3 | 1,987 (14h) | size | 0.4 / 0.4 / 0.5 | 0.3 / 0.3 / 0.4 | 3 / 3 |
| B3 | 1,987 (14h) | project | 364.1 / 372.9 / 384.7 | 336.5 / 339.3 / 341.1 | 3 / 3 |
| B3 | 1,987 (14h) | stream | 3.5 / 3.5 / 4.0 | 3.2 / 3.6 / 3.6 | 3 / 3 |
| B3 | 1,987 (14h) | drop | 0.8 / 0.8 / 1.1 | 0.9 / 1.1 / 1.4 | 3 / 3 |
| B4 | 5,036 (37h) | size | – | 0.4 (1 obs) | n/a / 0 |
| B4 | 5,036 (37h) | project | – | **did not complete**, still running at ~33 min when stopped | n/a / 0 |
| B4 | 5,036 (37h) | stream | – | not reached | n/a / 0 |
| B4 | 5,036 (37h) | drop | – | not reached | n/a / 0 |
| B5 | 9,998 (73h) | all | – | **not run**, extension stopped after B4 did not complete | n/a / 0 |

> **B4/B5 extension** (added at user request after Phase 4), probing the large end of the
> edge-count curve on the **Small** warehouse only (no resize).
>
> - **What it tested:** B4 ≈ 5,036 edges (the data's last 37 hours); B5 ≈ 9,998 edges (the
>   data's last 73 hours).
> - **Outcome: B4 did not complete.** Its projection was still running at **~33 min** when we
>   stopped it. The first attempt hit the `--read-timeout 1800` cap at 1,980 s (~33 min) with no
>   result; we stopped rather than chase ever-longer timeouts.
> - **B5 was not run.** At ~2× the edges it could only be slower.
> - **Warehouse side stayed trivial:** the sizing count returned normally (0.4 s, 5,036 edges).
> - **Where the time went:** the unbounded cost is the Aura GDS Session **projection**, which
>   jumps **super-linearly** at this scale. The B1–B3 fit predicted ~13 min for 5k edges, but the
>   real projection blew past ~33 min without returning.

> Operational record: these read-timeout / failure notes explain the retries and timeouts behind
> the Test set B runs. They are client-side connection mechanics, not warehouse behavior, and don't
> affect the findings; kept for completeness.

**Read-timeout / failure notes (2X-Small):**
- **B1**: 4 attempts, 3 OK + 1 TIMEOUT. One run failed in projection at 90.1s
  (SessionExpired / defunct connection) at the demo's default read-timeout; reran with
  `--read-timeout 300` → OK at 87.6s. The B1 projection itself (88-106s) already exceeds the
  60s Bolt hint, so the demo's fast-gds default already raises the read-timeout; the one
  failure was a transient connection drop, not the 60s cap.
- **B2**: needed `--read-timeout 900`. Two attempts at `--read-timeout 300` both FAILED in
  projection at ~420s (SessionExpired); the ~986-edge projection takes ~226s and under session
  cold-start the single Bolt read exceeded 300s, dropping the connection. At rt900 all 3 runs
  succeeded cleanly. The read-timeout is a client-side disconnect guard; it does not change
  measured step time, so it does not confound the size comparison.
- **B3**: run at `--read-timeout 900` from the start (projection ~373s > 300s); 3/3 OK.
- **Operational note**: back-to-back GDS runs in one shell loop were unreliable (a prior
  session's teardown collided with the next run's projection → hang/defunct connection). Each
  run was therefore executed as its own process with a gap between, which was 100% reliable.

**Read-timeout / failure notes (Small):** the same two operational hiccups recurred, both
unrelated to warehouse size:
- **B1**: 1 transient TIMEOUT (projection failed at 90.2s, SessionExpired / defunct connection,
  at the default read-timeout), the identical transient seen on 2X-Small; reran with
  `--read-timeout 300` → OK at 91.7s. 3/3 OK after the rerun.
- **B2**: one run hung after the first attempt (no output past the sizing step, no error,
  projection never returned). Stopped and relaunched as a fresh process → OK at 188.7s. Same
  session-teardown-collision class as the Phase 3 operational note; the relaunch was clean.
  All B2/B3 runs used `--read-timeout 900` from the start. 3/3 OK.
- **B3**: run at `--read-timeout 900` from the start; 3/3 OK, no hiccups.

### Test set C: large partitioned table (5 runs each); spill is the headline column

This is the stress test (Workload C): a group-by over a large (~100M-row) table,
chosen so the query should genuinely run out of memory and spill to disk on the smaller warehouse.
The formal Phases 5–6 comparison below is still to be run; a spike version already ran on the
2X-Small, with results in [`perf-tests-results-v2.md`](perf-tests-results-v2.md).
This is the one workload where a bigger warehouse is expected to actually help, so **spill** is the
column to watch.

Same columns and units as Test set A:

* **client wall-clock in seconds**: `min / median / max`, 5 runs, run 1 cold.
* **DB exec ms**: median Databricks `total_duration_ms`.
* **Spill**: `spilled_local_bytes` (the headline metric here; a bigger warehouse should reduce spill on this large table).
* **OK runs**: successes out of 5.
* **2XS / S**: 2X-Small / Small.

| Test | Query | Rows produced | 2X-Small wall-clock, sec (min/med/max) | Small wall-clock, sec (min/med/max) | DB exec, ms (med, 2XS / S) | Spill, bytes (2XS / S) | OK runs (2XS / S) |
|------|-------|---------------|------------------------|---------------------|---------------------------|-----------------|-------------------|
| C1 | full-table group-by | ~24k | | | | | |
| C2 | windowed group-by | | | | | | |
| C3 | pair group-by (opt) | 100 | | | | | |

### Cost: one row per phase (price-performance, not just time)

* **DBU/hr**: the warehouse's billed Databricks Units per hour at that size.
* **DBU consumed**: total DBUs the phase burned (from `system.billing.usage`).
* **Phase wall-clock**: elapsed time the phase ran, in `hh:mm`.

At this data size, cost is uptime-dominated (how long the warehouse was up at that size), not
per-query.

| Phase | Set | Size | DBU/hr | DBU consumed | Phase wall-clock (hh:mm) |
|-------|-----|------|--------|--------------|------------------|
| 1 | A | 2X-Small | | | |
| 2 | A | Small | | | |
| 3 | B | 2X-Small | | | |
| 4 | B | Small | | | |
| 5 | C | 2X-Small | | | |
| 6 | C | Small | | | |

### EXPLAIN log

> Operational record: confirms each query's SQL plan was the same on both warehouse sizes (so any
> timing difference would be the warehouse, not a different query). Kept for completeness.

The generated SQL below was recovered from `system.query.history.statement_text` on warehouse
`b0fffb8e3255bf85`: the exact SQL Aura submitted to Databricks. The CLI/driver returns the plan
only in its result summary, not as rows, so query.history is the reliable source. The SQL text is
identical across the 2X-Small and Small phases, so any timing difference is the warehouse, not a
different plan.

What it shows: every Cypher query translates to a single pushed-down SQL statement. The fan-out
aggregation (`count`, `sum`, `GROUP BY`) runs entirely on the warehouse, and the time window pushes
down as a `WHERE transfer_timestamp >= ?`. The relationship `TRANSFERRED_TO` maps to
`account_links`, and both endpoints resolve to `accounts`, so each query is a three-table join.

| Phase | Test | Pushdown or materialize | Generated SQL |
|-------|------|-------------------------|---------------|
| 1 | A1-A4 (windowed fan-out) | full pushdown: aggregation and filter run on the warehouse | query (a) below |
| 1 | A5 (no-window fan-out) | full pushdown: aggregation runs on the warehouse, no filter | query (b) below |
| 3 | B1-B3 sizing count | full pushdown: windowed `count` runs on the warehouse | query (c) below |
| 3 | B1-B3 window anchor | full pushdown: `max(transfer_timestamp)` runs on the warehouse | query (d) below |
| 3 | B1-B3 gds.graph.project | warehouse SQL pulls the windowed rows, then the in-memory build runs in the GDS Session | query (e) below |
| 3 | B1-B3 gds.pageRank.stream | no warehouse SQL; runs on the already-built in-memory graph in the GDS Session | n/a |

Generated SQL, recovered from query.history (backticks and `?` parameter markers exactly as Aura
emitted them):

**(a) A1-A4 windowed fan-out** (46 runs; the parameter is the window cutoff):

```sql
SELECT
  `src`.`account_id` AS `sender`,
  `dst`.`account_id` AS `recipient`,
  count(`t`.`src_account_id`) AS `pair_transfers`,
  sum(`t`.`amount`) AS `pair_outflow`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
GROUP BY `sender`, `recipient`
```

**(b) A5 no-window fan-out** (10 runs; identical to (a) without the `WHERE`):

```sql
SELECT
  `src`.`account_id` AS `sender`,
  `dst`.`account_id` AS `recipient`,
  count(`t`.`src_account_id`) AS `pair_transfers`,
  sum(`t`.`amount`) AS `pair_outflow`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
GROUP BY `sender`, `recipient`
```

**(c) B1-B3 sizing count** (47 runs; the size step, returns 1 row in ~0.4 s on all windows):

```sql
SELECT
  count(`t`.`src_account_id`) AS `edges`
FROM `graph-enriched-schema`.`account_links` AS `t`
WHERE (`t`.`transfer_timestamp` >= ? /*since*/)
```

**(d) B1-B3 window anchor** (47 runs; finds the data's latest timestamp to anchor the window):

```sql
SELECT
  max(`t`.`transfer_timestamp`) AS `mx`
FROM `graph-enriched-schema`.`account_links` AS `t`
```

**(e) B1-B3 gds.graph.project** (29 runs): the projection pulls the full windowed rows (every
column of both account endpoints and the transfer) into the GDS Session, which then builds the
in-memory graph. This corrects the earlier note that the projection had "no warehouse SQL": it does
run this pull on the warehouse, but it returns in well under a second, so the dominant projection
time is the GDS-Session build, not this query.

```sql
SELECT
  `src`.`account_id` AS `src_account_id`,
  `src`.`opened_date` AS `src_opened_date`,
  `src`.`holder_age` AS `src_holder_age`,
  `src`.`balance` AS `src_balance`,
  `src`.`account_hash` AS `src_account_hash`,
  `src`.`region` AS `src_region`,
  `src`.`account_type` AS `src_account_type`,
  `dst`.`account_id` AS `dst_account_id`,
  `dst`.`opened_date` AS `dst_opened_date`,
  `dst`.`holder_age` AS `dst_holder_age`,
  `dst`.`balance` AS `dst_balance`,
  `dst`.`account_hash` AS `dst_account_hash`,
  `dst`.`region` AS `dst_region`,
  `dst`.`account_type` AS `dst_account_type`,
  `t`.`src_account_id` AS `t_src_account_id`,
  `t`.`dst_account_id` AS `t_dst_account_id`,
  `t`.`link_id` AS `t_link_id`,
  `t`.`transfer_timestamp` AS `t_transfer_timestamp`,
  `t`.`amount` AS `t_amount`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*since*/)
```

---

## Phase summaries

### Phase 1: Workload A on the smaller warehouse (2X-Small)

**Result:** The query time is set by how many result rows come back, not by the warehouse. The
wait rises steadily as the time filter widens: about 1 second when filtered to the data's last
day (3,303 rows) up to about 27 seconds with no filter at all (222,966 rows). Databricks isn't the bottleneck: it finishes its part in
roughly 0.2 seconds and never runs short on memory. The rest of the time is Neo4j sending the rows
back to the client. A bigger warehouse isn't expected to help (Phase 2 tests that); the way to
speed this up is to narrow the time window so fewer rows come back.

| Test | Time filter (data's last N days) | Rows | Wait time, sec (min/med/max) |
|------|--------|------|------------------|
| A1 | last 1 day | 3,303 | 0.9 / 1.0 / 1.2 |
| A2 | last 3 days | 9,630 | 1.5 / 1.6 / 2.4 |
| A3 | last 7 days | 22,096 | 2.9 / 3.1 / 3.7 |
| A4 | last 30 days | 85,490 | 9.4 / 11.0 / 12.0 |
| A5 | all time (no filter) | 222,966 | 25.1 / 26.6 / 29.1 |

- **The wait tracks the row count almost exactly** (about 0.12 ms per row on top of a ~1 second
  floor). The only thing growing from A1 to A5 is the number of rows returned.
- **No cold-start penalty.** The first run of each test was not meaningfully slower than the rest,
  because the data is one small file and the warehouse was already running.
- **Repeating a query did not make it faster.** Neo4j re-sends the rows every time, so caching
  doesn't help.

**For the record:** Run 2026-06-09 ~19:32–19:39Z on warehouse `b0fffb8e3255bf85`, freshly
restarted at 2X-Small; all 5 runs of each test succeeded. Spill was zero on every run (one ~4 MB
source file, no memory pressure). The warehouse stayed at 2X-Small / RUNNING throughout (~43
monitor polls, no resize or auto-scale; log `monitor-log-phase1.md`). Databricks-side timing and
cost come from `system.query.history` / `system.billing.usage` after their lag and are recorded in
the tables above. A3 and A5 reproduced the row counts and times from the best-practices guide's
"Pattern 6" data-movement case.

### Phase 2: Workload A on the bigger warehouse (Small)

**Result:** The bigger warehouse made no difference. At every window the wait on Small matched
2X-Small within normal run-to-run noise (and on the 30-day window it was even a touch slower). This
is exactly what Phase 1 predicted: Databricks finishes each query in about 0.2 seconds with no
memory pressure, so there is nothing for a bigger machine to speed up. The wait is all Neo4j
sending rows back. The only real lever for this workload is narrowing the time window.

| Test | Time filter (data's last N days) | Rows | 2X-Small median (sec) | Small median (sec) | verdict |
|------|--------|------|--------------|-----------|---------|
| A1 | last 1 day | 3,303 | 1.0 | 1.0 | tie |
| A2 | last 3 days | 9,630 | 1.6 | 1.6 | tie |
| A3 | last 7 days | 22,096 | 3.1 | 3.0 | tie |
| A4 | last 30 days | 85,490 | 11.0 | 11.4 | tie (Small slightly slower) |
| A5 | all time (no filter) | 222,966 | 26.6 | 25.7 | tie |

**For the record:** Run 2026-06-09 ~19:56–20:02Z on `b0fffb8e3255bf85`, stopped and resized to
Small (which clears caches) then restarted; all 5 runs of each test succeeded. Databricks-side time
held at ~0.2 s per query on both sizes (Small medians A1 286 / A2 252 / A3 237 / A4 228 / A5 225 ms),
with zero spill on all 25 runs: the metric-level confirmation that warehouse size cannot move this
workload. The warehouse stayed steady at Small / RUNNING (~30 monitor polls, no resize or
auto-scale; log `monitor-log-phase2.md`). Cost (DBUs) for all phases is deferred to a single
end-of-run pull. Test set C (Phases 5–6) is the workload built to find a case where size *does*
matter, by scanning a much larger table.

### Phase 3: Workload B (PageRank) on the smaller warehouse (2X-Small)

**Result:** Nearly all the time goes into building the in-memory graph inside Neo4j (the `project`
step), and it grows steeply with the number of transfer relationships: about 1.5 minutes for 233
relationships, ~3.8 minutes for 986, and ~6.2 minutes for 1,987. The other steps (counting the
relationships, running the ranking, and cleaning up) are all under 4 seconds regardless of window.
Databricks does almost nothing here: its only job, counting the relationships in the window,
finishes in ~0.4 seconds, so a bigger warehouse has nothing to speed up. Phase 4 (Small) is
expected to come out the same.

| Test | Edges | size (sec) | project (sec) | stream (sec) | drop (sec) |
|------|-------|------|---------|--------|------|
| B1 | 233 | 0.4 / 0.4 / 0.4 | 87.6 / 88.3 / 105.6 | 2.6 / 2.8 / 2.9 | 0.8 / 0.8 / 0.8 |
| B2 | 986 | 0.3 / 0.4 / 0.4 | 221.4 / 226.7 / 230.1 | 2.6 / 2.8 / 2.9 | 0.8 / 0.8 / 1.1 |
| B3 | 1,987 | 0.4 / 0.4 / 0.5 | 364.1 / 372.9 / 384.7 | 3.5 / 3.5 / 4.0 | 0.8 / 0.8 / 1.1 |

- **The `project` (build) step is ~99% of the time** and is the part that scales with relationship
  count; everything else stays flat and small.
- **That cost is inside Neo4j, not Databricks.** Building the in-memory graph happens in a separate
  Neo4j compute (a "GDS Session") that never touches the warehouse.

**For the record:** Run 2026-06-09 ~20:13–20:45Z on `b0fffb8e3255bf85`, resized to 2X-Small and
restarted; 3 successful runs per window. Windows were chosen by relationship count, anchored to the
data's latest timestamp (2024-03-30 23:58Z): B1 = last 1.5h (233), B2 = last 7h (986), B3 = last
14h (1,987). The build step routinely outlasts the client's default connection timeout, so some
runs needed a longer `--read-timeout` and a couple dropped the connection and were rerun. These are
client-side connection limits, not warehouse behavior, and don't change the measured step times
(see the read-timeout notes above). Running each test as its own process (rather than back-to-back
in a loop) was needed for reliability. The warehouse held steady at 2X-Small / RUNNING throughout
(no resize or auto-scale; log `monitor-log-phase3.md`). Databricks-side timing and cost are deferred
to the end-of-run pull.

### Phase 4: Workload B (PageRank) on the bigger warehouse (Small)

**Result:** Same outcome as Phase 3: the bigger warehouse made no difference. The build step still
dominated and still tracked the relationship count (~1.5 min @233, ~3.4 min @986, ~5.7 min @1,987).
The two larger windows came in about 10% faster on Small, but that is normal variation in how long
Neo4j takes to spin up the build, not a warehouse effect. The only Databricks work is the
sub-second relationship count, which a bigger warehouse can't make meaningfully faster.

| Test | Edges | size (sec) | project (sec) | stream (sec) | drop (sec) |
|------|-------|------|---------|--------|------|
| B1 | 233 | 0.4 / 0.4 / 0.6 | 85.6 / 91.1 / 91.7 | 2.5 / 3.6 / 3.8 | 0.8 / 0.8 / 0.8 |
| B2 | 986 | 0.3 / 0.3 / 0.4 | 188.7 / 203.4 / 209.1 | 2.5 / 2.6 / 3.7 | 0.8 / 0.8 / 1.1 |
| B3 | 1,987 | 0.3 / 0.3 / 0.4 | 336.5 / 339.3 / 341.1 | 3.2 / 3.6 / 3.6 | 0.9 / 1.1 / 1.4 |

- **Build step, smaller → bigger warehouse (median):** 233 rel. 88.3 → 91.1 s (flat), 986 rel.
  226.7 → 203.4 s (−10%), 1,987 rel. 372.9 → 339.3 s (−9%). All within normal build-time variation;
  the other steps stay flat and under 4 s on both sizes.

**For the record:** Run 2026-06-09 ~21:46–22:53Z on `b0fffb8e3255bf85`, stopped and resized to Small
(clears caches) then restarted; same windows as Phase 3, 3 successful runs per window. The same two
connection hiccups as Phase 3 recurred (one dropped connection, one hung run), both rerun cleanly
and unrelated to warehouse size. The warehouse held steady at Small / RUNNING throughout (no resize
or auto-scale; log `monitor-log-phase4.md`). The query plan was identical to Phase 3, so the
comparison is apples-to-apples. Databricks-side timing and cost are deferred to the end-of-run
pull.

### PageRank at larger graphs (extension)

**Result:** PageRank stays usable up to roughly 2,000 transfer relationships; past that it falls
off a cliff. A graph of about 5,000 relationships (the data's last 37 hours of transfers) still
had not finished building after ~33 minutes, so we stopped it, and we didn't bother trying the ~10,000-relationship
graph. As with the smaller runs, the time goes into building the graph inside Neo4j, not into
Databricks: the Databricks side still answered its only query (counting the relationships) in under
half a second. What limits this workload is the number of relationships in the graph, not the size
of the warehouse.

| Test | Relationships (time filter) | Build step | Result |
|------|------------------------|-----------|--------|
| B4 | ~5,000 (data's last 37h) | did not finish | stopped after ~33 min; count query fine at 0.4 s |
| B5 | ~10,000 (data's last 73h) | not run | skipped, would only be slower |

**For the record:** Run on the Small warehouse (no resize). The two windows were the data's last
37 hours (~5,036 relationships) and last 73 hours (~9,998). The ~5,000-relationship build was still running
at ~33 minutes across two attempts (the client gave up at 33 min the first time; a second, longer
attempt was stopped by hand while it was still running), so there is no clean build time to report.
The ~10,000-relationship graph was not attempted. At roughly double the relationships it could only
be slower. For comparison, the smaller graphs from Phase 4 took ~1.5 min at 233 relationships,
~3.4 min at 986, and ~5.7 min at 1,987, so taking more than 33 minutes at ~5,000 is far worse than
that trend predicted. As always, this is time inside Neo4j; the warehouse's only query stayed at
~0.4 seconds.

### Where the bottleneck is so far (through Phase 4): Neo4j, not the warehouse

Across both workloads tested, the slow part is on the Neo4j / Aura side, and the Databricks
warehouse showed no strain of any kind: it never ran short on memory, never queued, and had no
slow first scan worth a bigger machine. That is why moving up a warehouse size didn't help in
Phases 1–4: there was nothing on the Databricks side for a bigger machine to speed up.

**Workload A ("who paid whom"):** Databricks finished each query in about 0.2 seconds at every
window, on both warehouse sizes, and never ran short on memory (the source is one ~4 MB file). The
rest of the wait, up to ~27 seconds for the all-time window, is Neo4j sending the result rows back
to the client, which grows with the number of rows, not with warehouse size.

**Workload B (PageRank):** Databricks' only job, counting the relationships in the window, took
about 0.4 seconds at every size. The slow part, building the in-memory graph inside Neo4j, was
~99% of the time and runs on separate Neo4j compute that never touches the warehouse.

---

## Glossary

- **SQL warehouse / 2X-Small / Small**: the Databricks compute the graph queries run on. The whole
  test asks whether the bigger size (Small) beats the smaller one (2X-Small).
- **Aura Virtual Graph**: a Neo4j feature that runs graph (Cypher) queries over data stored in
  Databricks, by translating them into SQL that runs on the warehouse.
- **transfer relationship (also "edge")**: one account-to-account money transfer.
- **time window**: the time filter each test applies. Only transfers whose `transfer_timestamp`
  falls in the last N days (or hours) of the dataset are included; the cutoff counts back from the
  data's latest timestamp of 2024-03-30, not from today. A wider window includes more transfers,
  and "all time" means no filter.
- **Test set A (A1–A5)**: the "who paid whom" query: for a time window, list sender → recipient
  pairs with how many transfers and how much money flowed between them. A1 = the data's last
  1 day, up to A5 = all time (no filter).
- **Test set B (B1–B5)**: PageRank ranking: build an in-memory graph from a window's transfers,
  then rank the accounts. B1 ≈ 233 relationships, up to B5 ≈ 10,000.
- **Test set C (C1–C3)**: a stress test over a large (~100M-row) table, built to finally tax the
  warehouse. The formal Phases 5–6 comparison is not yet run; a spike version ran on the 2X-Small
  (see [`perf-tests-results-v2.md`](perf-tests-results-v2.md)).
- **build step (also "project")**: building the in-memory graph inside Neo4j before PageRank can
  run. This turned out to be the slow part of Test set B.
- **spill**: when a query runs out of memory and has to write to disk. More memory (a bigger
  warehouse) means less spill. It never happened here, because the data is too small.
- **wall-clock vs Databricks time**: wall-clock is the total time the user waits; Databricks time
  is just the slice spent inside the warehouse. The difference is time spent in Neo4j and on the
  network.

---

## Phase 0 findings (run 2026-06-09)

> Operational record: the detailed setup findings behind the Summary, kept for completeness. The
> key one in plain English: the source data is a single ~4 MB file, so the warehouse was never
> going to be the bottleneck, which is what motivated the larger Test set C.

- **Backing warehouse confirmed.** `vg demo sql warehouse` / `b0fffb8e3255bf85`, serverless
  PRO, found at 2X-Small. The federation metadata calls (`Listing columns ... catalog:
  graph-on-databricks, schemaPattern: graph-enriched-schema`) and the `select 1` keepalives
  land here. A separate `Warehouse (AT)` / `06538df9820b42f9` also shows `select 1`; ignore it.
- **Source tables are tiny single files. This reframes the whole test.** `TRANSFERRED_TO`
  maps to `graph-on-databricks.graph-enriched-schema.account_links`: 300,000 rows,
  **1 file, ~3.9 MB, no partitioning, no clustering**, `transfer_timestamp` spanning
  2024-01-01 to 2024-03-30 23:58. (`transactions` is account-to-merchant and is not the Set
  A source; also 1 file, ~3.0 MB.) Because the scan is one ~4 MB file, **warehouse size
  cannot move the scan floor and spill is impossible**. `spilled_local_bytes` will always be
  0. The only thing that grows across A1-A5 is rows shipped back, so Set A is really a
  Pattern 6 data-movement test, not a scan-or-spill test. Expect 2X-Small and Small to be
  about equal on Set A, for the same reason Set B is warehouse-insensitive. This finding is
  what motivated Test set C.
- **`system.query.history` lags ~11 minutes.** A query run at 19:00 was still absent at
  19:09; `max(start_time)` trailed `current_timestamp()` by ~670 s. The monitor agent
  **cannot** pull metrics right after a test from the system table. Use **client wall-clock
  from `vg-probe` as the primary, real-time metric**, and pull `system.query.history` for the
  spill/exec-time detail at the very end of all phases (or after a >12 min wait). At ~4 MB
  there is no spill to catch anyway, so the history pull is confirmatory, not load-bearing.
- **Correlation works by content + order.** Filter on `compute.warehouse_id =
  'b0fffb8e3255bf85'` and `statement_text ILIKE '%account_links%'`, ordered by `start_time`;
  because the run is isolated and one-at-a-time, the rows line up with the client runs. No
  distinctive-literal trick is needed.
- **Result cache does not short-circuit the Aura path.** The A1 query run five times back to
  back stayed flat at ~0.9-1.0 s and never collapsed toward the 0.2 s client floor, so reruns
  genuinely re-execute on the warm warehouse. **Resolution of the deferred cache question:
  report run 1 (cold) versus runs 2-5 (warm) as-is; no `use_cached_result` change or
  cache-busting is needed.**
- **Wall-clock composition measured.** `RETURN 1` is ~0.2-0.3 s and never reaches Databricks
  (the graph engine answers it), so it is a pure client + Bolt floor, and therefore **not a
  warehouse warm-up**. The A1 1-day window (3,303 rows) was ~5.1 s on the first cold call and
  ~0.9-1.0 s warm. The cold-to-warm gap is cluster spin-up plus first read of the one file,
  not scan volume.
- **Warm-up correction.** Because `RETURN 1` never hits the warehouse, it cannot warm it. The
  timed phases therefore treat **run 1 of each query as the genuine cold sample** (cold
  cluster + cold file) and runs 2 onward as warm; `RETURN 1` is kept only as the client-floor
  baseline for the wall-clock-composition column, not as a warm-up.
- **Cost is capturable but uptime-dominated.** `system.billing.usage` carries per-warehouse
  DBUs under SKU `PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST_N_VIRGINIA`, filterable by
  `usage_metadata.warehouse_id`. Billing also lags, so pull it after the run. At this data
  size the queries are sub-second, so phase DBU cost is driven by how long the warehouse is
  up at each size, not by the queries. Read the cost table as "DBU/hr of the size × time the
  warehouse ran," not as per-query cost.

---

## How to use this file (logging protocol)

For each test (a single query or demo, e.g. `A1`, `B2`, `C1`), in this order:

1. **Before running, log a STARTING entry** in [Work log](#work-log): UTC timestamp, phase,
   warehouse size, test label, the exact query or command about to run, and the `EXPLAIN`
   outcome (pushed down vs a materialize/post-processing step). Log what is running *before*
   it runs, so an interrupted test still leaves a record of what was in flight.
2. **Run it**: five times for sets A and C, three times for set B, one at a time.
3. **After it finishes, fill the matching results-table row(s)** below, then **log a RESULT
   entry**: min / median / max, rows produced, spill, run status, and a one-line takeaway.
4. **At the end of each phase, log a phase summary** in [Phase summaries](#phase-summaries):
   the size, what was observed across the phase's tests, cost, and anything surprising.

Outcome codes (`OK` / `ERROR` / `TIMEOUT` / `QUEUED`) and the meaning of each table column are
defined in
[`perf-test.md` → Failure and partial-result handling](perf-test.md#failure-and-partial-result-handling).
Remember the Phase 0 finding that `system.query.history` lags ~11 minutes: client wall-clock
goes in the RESULT entry immediately; the spill / Databricks-exec columns are filled later
from history.

### Work-log entry format

```
[YYYY-MM-DDTHH:MM:SSZ] Phase N · <size> · <test label> · STARTING
  query:   <cypher or shell command>
  explain: <pushdown | materialize note | n/a>

[YYYY-MM-DDTHH:MM:SSZ] Phase N · <size> · <test label> · RESULT
  wall-clock (min/med/max): … s   rows: …   status: … (OK runs n/5)
  db exec (med): … ms   spill: … bytes
  takeaway: <one line>
```

---

## Work log

> Operational record: the raw, chronological run log, kept for completeness. For the
> plain-English findings see the [Summary](#summary-what-this-measures-and-what-we-found) and
> [Phase summaries](#phase-summaries).

```
[2026-06-09T19:00:00Z] Phase 0 · 2X-Small · instrumentation spike · STARTING
  Ran RETURN 1 ×3, the A1-shape windowed fan-out ×1 then ×5, system.query.history pulls,
  DESCRIBE DETAIL + detailed stats on account_links and transactions, system.billing.usage,
  and a warehouse list, to settle the open methodology questions before any timed phase.

[2026-06-09T19:10:00Z] Phase 0 · 2X-Small · instrumentation spike · RESULT
  Backing warehouse = vg demo sql warehouse / b0fffb8e3255bf85 (2X-Small).
  RETURN 1 ≈ 0.2-0.3 s (never hits Databricks). A1 ≈ 5.1 s cold, ≈ 0.9-1.0 s warm; reruns
  flat at ~1.0 s (result cache does not short-circuit Aura). account_links = 300k rows /
  1 file / ~3.9 MB / no partitioning. system.query.history lags ~11 min. Cost capturable via
  system.billing.usage. takeaway: source too small for warehouse size to matter; added
  Test set C; cache question resolved (report cold vs warm as-is). Full detail below.
```

[2026-06-09T19:32:00Z] Phase 1 · 2X-Small · phase open · STARTING
  Operator confirmed warehouse b0fffb8e3255bf85 freshly restarted, 2X-Small, RUNNING.
  Monitor agent launched (background), confirmed warehouse at 2X-Small/RUNNING.
  RETURN 1 client-floor baseline: 0.2s rows=1 (never reaches Databricks; matches Phase 0).
  EXPLAIN pass: A1-shape and A5-shape both plan without error via vg-probe (rows=0, no
  exception). NOTE: the CLI/driver returns the EXPLAIN plan in the result summary, not as
  rows, so the generated SQL and the pushdown-vs-materialize flag cannot be captured from the
  CLI. That requires the Aura Workspace Query tab. EXPLAIN log rows record this limitation.

[2026-06-09T19:32:00Z] Phase 1 · 2X-Small · A1 (last 1 day, cutoff 2024-03-29T23:58:00Z) · STARTING
  query:   MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
           WHERE t.transfer_timestamp >= datetime("2024-03-29T23:58:00Z")
           WITH src.account_id AS sender, dst.account_id AS recipient,
                count(t) AS pair_transfers, sum(t.amount) AS pair_outflow
           RETURN sender, recipient, pair_transfers, pair_outflow
  explain: plans without error; generated SQL not capturable from CLI (see phase-open note)
  runs:    vg-probe ×5, one at a time (run 1 cold, runs 2-5 warm)

[2026-06-09T19:39:00Z] Phase 1 · 2X-Small · A1-A5 · RESULT
  All five tests ran one-at-a-time, vg-probe ×5 each, all OK (5/5 each, no errors/timeouts).
  Wall-clock (min/med/max s) and rows:
    A1 (1d):  0.9 / 1.0 / 1.2   rows=3,303
    A2 (3d):  1.5 / 1.6 / 2.4   rows=9,630
    A3 (7d):  2.9 / 3.1 / 3.7   rows=22,096   (matches Phase 0 ~22,096-row baseline)
    A4 (30d): 9.4 / 11.0 / 12.0 rows=85,490
    A5 (all): 25.1 / 26.6 / 29.1 rows=222,966 (matches Pattern-6 ~222,966-row / ~24.8s case)
  db exec ms / spill: pending; system.query.history lags ~11 min; pulled at end of phase.
  takeaway: wall-clock tracks rows-shipped almost linearly (~0.12 ms/row above the ~0.9 s
  floor), exactly the Pattern 6 data-movement signature. A1's only cold-warm gap is small
  (1.2→1.0 s) and A5 shows NO cold-warm gap at all (run 1 not slowest), so the cost is
  shipping rows back over Bolt, not cold scan. Spill expected 0 (one ~4 MB file). Warehouse
  size is not expected to move these numbers; Phase 2 (Small) will confirm.

[2026-06-09T19:53:00Z] Phase 1 · 2X-Small · query.history pull · RESULT
  Pulled system.query.history (warehouse b0fffb8e3255bf85, statement_text ILIKE %account_links%,
  start_time >= 19:30Z): exactly 25 rows = the 25 A1-A5 runs, all execution_status=FINISHED.
  - spilled_local_bytes = 0 on ALL 25 runs, no memory pressure, as predicted for a ~4 MB source.
  - Databricks total_duration_ms median per test: A1 208, A2 219, A3 191, A4 232, A5 224 ms
    (cold first-run higher: 428/907/839/861/774 ms, mostly compilation). execution_duration_ms
    is 3-503 ms. Warehouse-side time is ~0.2 s regardless of result size.
  - read_bytes only non-zero on the FIRST run of each group (~2.8-4.5 MB, one-file scan);
    reruns read 0 bytes (Databricks disk-caches the scan, exec ~3 ms), yet client wall-clock
    stays flat. This is the Phase 0 "result cache does not short-circuit Aura" finding, proven
    at the metric level: Databricks serves reruns from cache but Aura re-ships every time.
  - The gap = Aura + Bolt data movement. A5: DB total ~0.22 s median vs client ~26.6 s median
    → ~26 s/run is shipping 222,966 rows back over Bolt, not warehouse work. A1 gap ~0.8 s.
    The gap scales with rows shipped → Set A is data-movement bound, not scan/compute/spill bound.
  - DBU/cost: deferred to end-of-all-phases pull (system.billing.usage lags hours; cost here is
    uptime-dominated, not per-query). Cost table row 1 left pending.
  takeaway: hard confirmation that warehouse size cannot move Set A: the warehouse already
  finishes in ~0.2 s; the wall-clock is Aura data movement. Phase 2 (Small) should match Phase 1.

[2026-06-09T19:56:00Z] Phase 2 · Small · phase open · STARTING
  Primary agent owned the boundary: stopped warehouse (cleared caches), modified size to Small
  while stopped, restarted; polled to RUNNING at Small by 19:55:55Z. Monitor agent (phase 2)
  launched to confirm size=Small and watch state.
  RETURN 1 baseline: 0.2s (matches phase 1). EXPLAIN A1-shape and A5-shape plan without error
  (rows=0), same as phase 1; generated SQL still not capturable from CLI.
  About to run A1-A5, vg-probe ×5 each, one at a time (run 1 cold on freshly restarted Small).

[2026-06-09T20:02:00Z] Phase 2 · Small · A1-A5 · RESULT
  All five ran one-at-a-time, vg-probe ×5 each, all OK (5/5 each).
  Wall-clock (min/med/max s), rows unchanged from phase 1:
    A1 (1d):  1.0 / 1.0 / 2.0   rows=3,303
    A2 (3d):  1.6 / 1.6 / 1.9   rows=9,630
    A3 (7d):  2.9 / 3.0 / 3.4   rows=22,096
    A4 (30d): 10.7 / 11.4 / 11.7 rows=85,490
    A5 (all): 25.1 / 25.7 / 27.3 rows=222,966
  Side-by-side medians (2X-Small → Small): A1 1.0→1.0, A2 1.6→1.6, A3 3.1→3.0, A4 11.0→11.4,
  A5 26.6→25.7. Differences are all within run-to-run noise (<1 s) and not consistently in
  Small's favor (A4 is marginally slower on Small). db exec ms / spill pending the ~11 min
  history lag.
  takeaway: SIZE DOES NOT MATTER for Set A. Small is statistically identical to 2X-Small at
  every window, exactly as predicted, because the warehouse already finishes in ~0.2 s and the
  wall-clock is Aura/Bolt data movement. The lever for Set A is Pattern 6 (shrink the window),
  not a bigger warehouse.

[2026-06-09T20:32:00Z] Phase 2 · Small · query.history pull · RESULT
  Pulled system.query.history (warehouse b0fffb8e3255bf85, start_time 19:55-20:05Z,
  statement_text ILIKE %account_links%): exactly 25 rows = the 25 A1-A5 runs, all
  execution_status=FINISHED. (Note: the broad scan kept hitting the 60s MCP timeout; a tight
  partition-pruning start_time window made it return.)
  - spilled_local_bytes = 0 on ALL 25 runs, no memory pressure on Small either, as predicted.
  - total_duration_ms median per test (Small): A1 286, A2 252, A3 237, A4 228, A5 225 ms.
    Cold first-run higher per group (1162/465/550/545/715 ms); execution_duration_ms 3-8 ms.
  - Side-by-side DB total median (2X-Small → Small): A1 208→286, A2 219→252, A3 191→237,
    A4 232→228, A5 224→225. All ~0.2 s on both sizes; differences are noise. Warehouse-side
    time is ~0.2 s regardless of size or result-set size, confirming Set A wall-clock is Aura +
    Bolt data movement, not warehouse compute. Filled the Small DB-exec/spill columns.
  takeaway: confirms the Phase 2 conclusion at the metric level: Small's warehouse-side time
  matches 2X-Small's (~0.2 s) with zero spill; size cannot move Set A.

[2026-06-09T20:13:00Z] Phase 3 · 2X-Small · phase open + window sizing · STARTING
  Primary agent owned the boundary: stopped warehouse at Small (cleared caches), modified size
  to 2X-Small while stopped, restarted; polled to RUNNING at 2X-Small by ~20:11Z. Background
  bash poller writing monitor-log-phase3.md (the subagent monitor exited early and was replaced
  by a direct poller).
  --count-only window sweep (anchors to data max 2024-03-30 23:58Z, NOT wall-clock):
    1.5h → 233 edges  (B1 baseline)
    6h   → 839,  7h → 986,  7.5h → 1051   → B2 = 7h (~986 edges, closest to 1,000)
    12h  → 1689, 14h → 1987, 15h → 2134   → B3 = 14h (~1,987 edges, closest to 2,000)
  Chosen windows: B1 --since-hours 1.5, B2 --since-hours 7, B3 --since-hours 14.
  explain: sizing count query plans without error; generated SQL NOT capturable from CLI/driver
  (EXPLAIN returns plan in driver summary, not rows, same limitation as Phase 1/2). The
  gds.graph.project and gds.pageRank.stream steps are GDS ops, not pushdown SQL, so no warehouse
  SQL to capture for them.
  About to run B1, B2, B3 via vg-demo --demo fast-gds, ×3 each, one at a time, recording the four
  printed step timings (size / project / stream / drop) per run.

[2026-06-09T20:45:00Z] Phase 3 · 2X-Small · B1-B3 · RESULT
  All three windows reached 3 OK runs (run one-at-a-time as separate processes; chained loops
  collided with prior-session teardown). Step times (min/med/max s):
    B1 (233 edges):   size 0.4/0.4/0.4   project 87.6/88.3/105.6   stream 2.6/2.8/2.9   drop 0.8/0.8/0.8   (3 OK + 1 TIMEOUT)
    B2 (986 edges):   size 0.3/0.4/0.4   project 221.4/226.7/230.1 stream 2.6/2.8/2.9   drop 0.8/0.8/1.1   (3 OK; needed --read-timeout 900)
    B3 (1987 edges):  size 0.4/0.4/0.5   project 364.1/372.9/384.7 stream 3.5/3.5/4.0   drop 0.8/0.8/1.1   (3 OK; --read-timeout 900)
  Failures (recorded, not dropped): B1 1×TIMEOUT (project failed 90.1s, transient SessionExpired
  at default rt; reran at rt300 → OK). B2 2×FAILED at rt300 (project SessionExpired at ~420s;
  the ~226s projection under session cold-start exceeded the 300s single-read cap). All cleared
  at rt900. The read-timeout is a disconnect guard, not part of measured step time.
  takeaway: PROJECT (GDS Session provisioning) is ~99% of the cost and scales with edge count:
  ~88 s @233 → ~227 s @986 → ~373 s @1987 (roughly linear, ~0.16 s/edge above a ~70 s session
  floor). size/stream/drop are flat and sub-4 s. The Databricks side is trivial. This is an
  Aura GDS-Session cost, not a warehouse cost, so Phase 4 (Small) is expected to be flat. The
  windowed edge count is the lever for GDS cost, mirroring Pattern 6 for Set A.

<!-- Append new entries above this line in reverse-chronological or chronological order;
     pick one and stay consistent. -->
