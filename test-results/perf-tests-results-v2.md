# Test Set C v2: Staged Ramp Spike

Companion to [`perf-test.md`](perf-test.md) and [`perf-tests-results.md`](perf-tests-results.md).
This file revises how Test set C gets off the ground. The original plan jumps straight to a
~100M-row build (perf-test.md:485-537). This version ramps the table up from small sizes,
proves the queries work and finds where compute pressure actually starts, before committing
to the large build and the 2X-Small-vs-Small comparison.

## Why a staged ramp

Test set C exists for one reason: to create enough scan-and-aggregate work that warehouse size
moves the timing. Phase 0 found the live `account_links` is one ~4 MB file (~300k rows), too
small for size to matter, and Phases 1-4 confirmed no warehouse bottleneck on Sets A and B.
The 100M figure in the original plan is a guess at where a 2X-Small starts to spill. It is not
measured.

Two risks with jumping to 100M:

- The `TRANSFERRED_TO_BIG` mapping or the C-query pushdown might be broken, and that surfaces
  only after an expensive build.
- 100M might be far more or far less than the real spill threshold, so the comparison runs at
  the wrong size.

The ramp removes both. Small builds are cheap and validate the plumbing. Watching scan time
and `spilled_local_bytes` climb as the table grows locates the real threshold, which becomes
the size the Phase 5/6 comparison runs on.

## The two spikes

There are two separate things to validate, and they have different prerequisites.

**Spike 1: SQL-side, no Aura mapping needed.** Build `account_links_large` at each ramp size,
then run the aggregation SQL that C1/C2/C3 push down to, directly against the table on the
backing warehouse. This confirms the table builds correctly and measures scan time and spill
as the table grows. It needs no Aura model change, so it runs end to end from here.

**Spike 2: Cypher pushdown, needs the Aura mapping.** Once `TRANSFERRED_TO_BIG` is mapped to
`account_links_large` in the Aura model editor (perf-test.md:539-547, a manual UI step), run
C1/C2/C3 with `EXPLAIN` to confirm pushdown, then `vg-probe` for client wall-clock. This
validates the actual Virtual Graph path.

Spike 1 runs first and unattended. Spike 2 runs after the operator maps the relationship.

## The ramp

`account_links_large` is built with `CREATE OR REPLACE TABLE`, so each step rebuilds the same
table name and columns. The Aura mapping points at that name, so the relationship is mapped
once and never remapped. Each ramp step is one number change, the `range(0, N)` row count.

Requested starting sizes: 100K, 250K, 500K, 1M. The ramp then continues upward (10M, 50M,
100M) until the 2X-Small shows non-zero `spilled_local_bytes` or a clearly long scan, or until
100M is reached. The size where pressure first appears is the floor for the real comparison.

| Step | Rows | Purpose |
|------|------|---------|
| S0 | 100K | plumbing: build works, queries return correct rows, partitions present |
| S1 | 250K | ramp |
| S2 | 500K | ramp |
| S3 | 1M | ramp; ~3x the live table |
| S4+ | 10M, 50M, 100M | find where scan time climbs and spill appears |

All Spike 1 steps run on the **2X-Small** backing warehouse `b0fffb8e3255bf85`, because that is
the size where spill appears first and the size the comparison most needs a threshold for.

### Build SQL (one number per step)

The build is the original from perf-test.md:511-537, parameterized only on the row count. It
samples real `account_id`s so no edge is dropped at query time, generates fresh random pairs
for high pair cardinality, and partitions on `transfer_date`.

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
  FROM range(0, <N>)
)
SELECT g.link_id, s.account_id AS src_account_id, d.account_id AS dst_account_id,
       g.amount, g.transfer_timestamp, CAST(g.transfer_timestamp AS DATE) AS transfer_date
FROM gen g
JOIN ids s ON s.idx = g.src_idx
JOIN ids d ON d.idx = g.dst_idx
```

### Validation after each build

- row count matches `<N>`
- distinct `(src_account_id, dst_account_id)` pair count (climbs toward tens of millions on the
  large steps, which is what makes C3 able to spill)
- partition count on `transfer_date` (about 90)

### The aggregation SQL (the C-query pushdown equivalents)

Each maps to a C query. These are what the Virtual Graph pushes to the warehouse, run directly
so Spike 1 needs no Aura mapping.

**C1, full-table group-by** (scans every row, ~24k groups):

```sql
SELECT src_account_id AS account_id,
       count(*) AS transfers, sum(amount) AS outflow,
       avg(amount) AS avg_amount, max(amount) AS max_amount
FROM `graph-on-databricks`.`graph-enriched-schema`.`account_links_large`
GROUP BY src_account_id
```

**C2, windowed group-by** (cutoff on a partition boundary, tests pruning):

```sql
SELECT src_account_id AS account_id, count(*) AS transfers, sum(amount) AS outflow
FROM `graph-on-databricks`.`graph-enriched-schema`.`account_links_large`
WHERE transfer_timestamp >= TIMESTAMP('2024-03-23T23:58:00Z')
GROUP BY src_account_id
```

**C3, high-cardinality pair group-by** (large hash-aggregate state, most likely to spill):

```sql
SELECT src_account_id AS sender, dst_account_id AS recipient,
       count(*) AS pair_transfers, sum(amount) AS pair_outflow
FROM `graph-on-databricks`.`graph-enriched-schema`.`account_links_large`
GROUP BY src_account_id, dst_account_id
ORDER BY pair_outflow DESC
LIMIT 100
```

## What Spike 1 records

Per ramp step: build wall-clock, row count, distinct pairs, partition count, and for each of
C1/C2/C3 the query wall-clock and rows produced. `spilled_local_bytes` and Databricks
execution time come from `system.query.history` after the ~11 min lag (Phase 0 finding), so
the wall-clock goes in immediately and the spill columns get filled on a later pull. Spill is
expected to be 0 through at least 1M and to appear, if at all, only on the large steps.

## Exit criteria

Spike 1 is done when one of these holds, and the result names the size to use for Phase 5/6:

- A ramp step shows non-zero `spilled_local_bytes` on the 2X-Small, or a C-query scan time
  that climbs sharply. That step's size is the comparison floor.
- The ramp reaches 100M with no spill. Then the honest read is that this query shape does not
  spill on this data, and the comparison should still run at 100M for maximum scan pressure
  while reporting that spill was never provoked.

---

## Spike 1 work log

```
[2026-06-09] Spike 1 · 2X-Small · ramp 100K -> 1M · RUN
  Warehouse b0fffb8e3255bf85 found already at 2X-Small / RUNNING (no resize; a resize was
  blocked as a shared-infra change, and the small ramp is size-insensitive anyway).
  Built account_links_large at 100K, 250K, 500K, 1M via CREATE OR REPLACE (one number change,
  range(0,N)). Validated row count, distinct pairs, partitions after each build. Ran the C1/C2/C3
  pushdown-equivalent SQL directly against the table, each wrapped in an outer count(*) so the
  scan-and-aggregate cost is preserved without shipping result rows. All four builds and all
  twelve queries succeeded. Every C query returns correct, growing-cardinality results.
  Databricks exec time and spilled_local_bytes pulled from system.query.history (~11 min lag),
  so those columns are filled on a later pull; at <= 1M rows spill is expected to be 0.

[2026-06-09] Spike 1 · 2X-Small · ramp 10M -> 100M · RUN
  Continued the ramp at user go-ahead: built account_links_large at 10M, 50M, 100M (same build,
  larger range(0,N), timeout 600s), and reran C1/C2/C3 at each. All builds and queries succeeded.
  100M table is ~1.07 GB / 90 files (DESCRIBE DETAIL). history lag measured at 18.5 min at the
  end of the ramp, so the large-step exec time / spill land ~04:40Z; pulling after the lag.
```

## Spike 1 results

Rows / distinct pairs / partitions are measured. C1/C2/C3 group counts (rows produced) confirm
correctness. Pair counts are exact through 1M and `approx_count_distinct` (HLL) at 10M and
above. Databricks `execution_duration_ms` and `spilled_local_bytes` are from
`system.query.history`, all statements `FINISHED`.

| Step | Rows | Distinct pairs | Partitions | C1 rows | C2 rows | C3 rows |
|------|------|----------------|------------|---------|---------|---------|
| S0 | 100K | 99,991 | 90 | 24,548 | 6,663 | 100 |
| S1 | 250K | 249,959 | 90 | 24,999 | 13,406 | 100 |
| S2 | 500K | 499,801 | 90 | 25,000 | 19,759 | 100 |
| S3 | 1M | 999,238 | 90 | 25,000 | 23,897 | 100 |
| S4 | 10M | ~9,716,474 | 90 | 25,000 | 25,000 | 100 |
| S5 | 50M | ~49,168,253 | 90 | 25,000 | 25,000 | 100 |
| S6 | 100M | ~94,728,223 | 90 | 25,000 | 25,000 | 100 |

### Databricks exec time and spill (2X-Small)

`exec_ms` is `execution_duration_ms`. `spill` is `spilled_local_bytes`. `build_ms` is the CTAS
`execution_duration_ms` for that step.

| Step | Rows | build_ms | C1 exec_ms | C2 exec_ms | C3 exec_ms | C3 read_rows | Spill (all queries) |
|------|------|----------|-----------|-----------|-----------|--------------|---------------------|
| S0 | 100K | 8,284 | 586 | 468 | 543 | 100,000 | 0 |
| S1 | 250K | 6,448 | 373 | 379 | 436 | 250,000 | 0 |
| S2 | 500K | 4,769 | 374 | 316 | 423 | 500,000 | 0 |
| S3 | 1M | 5,714 | 384 | 394 | 455 | 1,000,000 | 0 |
| S4 | 10M | 6,347 | 340 | 333 | 858 | 10,000,000 | 0 |
| S5 | 50M | 10,960 | 391 | 523 | 3,432 | 50,000,000 | 0 |
| S6 | 100M | 16,922 | 382 | 580 | 5,234 | 100,000,000 | 0 |

At 100M the table is **~1.07 GB across 90 files** (`DESCRIBE DETAIL`: `sizeInBytes`
1,069,160,548, `numFiles` 90, zstd Delta), roughly 270x the ~4 MB live `account_links`.

Findings:

- **Zero spill at every size, including 100M / ~94.7M distinct groups.** The headline. The
  premise of Test set C, that a 2X-Small spills on a large table and a bigger warehouse removes
  the spill, **does not hold for these queries on this data**. The 2X-Small builds its hash
  aggregate of ~95M groups entirely in memory.
- **C3 is the only query with a real compute floor**, and it scales with row count:
  455 ms @1M, 858 ms @10M, 3,432 ms @50M, **5,234 ms @100M**, reading the full table each time.
  This is the genuine scan-and-aggregate cost a larger warehouse could lower.
- **C1 stays flat (~380 ms) but is cache-served, not a true cold scan.** Its `read_rows` caps at
  ~2.25M from cloud storage at 10M+, while C3 reads the full table. Each query ran seconds after
  the build that wrote the data, so the warehouse local disk cache was hot (the Phase 0 finding
  that only a restart clears it). C1's numbers are warm and understate a cold full scan; C3's
  rising curve is the trustworthy compute signal.
- **C2 partition pruning works**: it reads only ~176k rows (the last week of partitions) at
  100M and stays sub-second regardless of table size. Size is irrelevant to C2.
- **Build time scales with rows** (8.3 s @100K is cold-cluster startup; then 5-6 s through 10M,
  11 s @50M, 17 s @100M), confirming the writes are real and the warehouse is doing the work.

## Spike 1 conclusion

The queries work and the pipeline is sound from 100K to 100M. The spike answers the precondition
question that the original Test set C left open: **on this data the 2X-Small never spills, not
even at 100M rows / 1 GB / ~95M aggregate groups.** So the spill-reduction rationale for a bigger
warehouse is gone. The only query with a measurable compute floor is **C3** (~5.2 s at 100M, full
scan and high-cardinality aggregate); C1 is cache-served and C2 prunes to sub-second.

Recommendation for Phase 5/6, the actual size comparison:

- Run the comparison at **100M** (the largest pressure built; no spill regardless, so larger
  would only confirm the same).
- **C3 is the query to watch.** It is the one place Small might beat 2X-Small, by lowering the
  ~5 s scan-and-aggregate floor. Expect no spill-removal win, since there is no spill to remove.
- For a fair cold C1, restart the warehouse before timing (per the plan's between-phase restart),
  or C1 stays cache-served and uninformative.
- This still needs the warehouse-resize authorization that was blocked, plus the Aura mapping for
  Spike 2 (the Cypher pushdown path).

## Spike 2 (Cypher pushdown) — pending Aura mapping

Run after the operator maps `TRANSFERRED_TO_BIG` to `account_links_large`. Per C query:
`EXPLAIN` to confirm pushdown with no materialize step, then `vg-probe` for client wall-clock.
