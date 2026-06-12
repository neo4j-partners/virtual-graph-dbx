# Virtual Graph Demo

This project demonstrates the Neo4j Virtual Graph, which enables zero-copy querying of
Databricks lakehouses with Cypher. The Virtual Graph compiles Cypher into SQL and pushes
most of the work down to the backing Databricks SQL warehouse; graph-specific operations
are handled by Neo4j's graph compute layer, and how much runs where depends on the query.
The project walks through setting up the Finance Genie sample lakehouse on Databricks,
creating a Virtual Graph over it in Aura, and querying that graph. It also documents best
practices, how to use GDS Sessions, and the current limitations of the Virtual Graph.

Finance Genie is a synthetic dataset of bank accounts, merchants, and the transfers
between them. Virtual Graph is in preview, and the official docs advise against using
sensitive or production data with it; this demo uses only that synthetic dataset.

For further information, see:

- [Introducing Neo4j Virtual Graph](https://neo4j.com/blog/graph-database/introducing-neo4j-virtual-graph-graph-reasoning-on-the-data-you-already-have/)
- [Getting started with Databricks](https://neo4j.com/docs/virtual-graph/aura/getting-started-databricks/)

This [uv](https://docs.astral.sh/uv/) Python demo runs Cypher against the Finance Genie
Virtual Graph. It includes the following demo sets:

| Demo | What it is | Everything works? |
|---|---|---|
| `basic` | warm-up exploration and visualization queries | yes |
| `fraud` | the fast fraud-signal queries from `finding-fraud.md` | yes |
| `fast-gds` | the working GDS Session + PageRank path | yes, on a small window |
| `slow-gds` | the GDS forms that do not work, kept as a demonstration | no, by design |
| `gds-probe` | sweep projections that add node / relationship properties, to isolate which property types the projection rejects | mixed, by design |
| `timezone` | reproduce the per-row `current_timezone()` round trip that dominates wall-clock on TIMESTAMP results | yes |
| `100m` | the SQL-side "zero spill from 100K to 100M rows" spike, confirmed against warehouse query history | yes |

The queries build up gradually, from simple counts, through fraud-signal queries, to
graph algorithms like PageRank, so you can see both what runs well and where the Virtual
Graph reaches its current limits.

Getting it running is three steps: create the Silver tables on Databricks, build the
Virtual Graph over them in Aura, then run the demos.

## Quick start

Prerequisites:

- `uv` installed.
- A Databricks workspace with permission to create a schema, volume, and tables, used
  once by the lakehouse setup notebook (Step 1).
- A project `.env` (copy `.env.sample`) with the Aura connection:

  ```
  NEO4J_URI=neo4j+s://<instance>.graph-engine.neo4j.io
  NEO4J_USERNAME=neo4j
  NEO4J_PASSWORD=<password>
  ```

  At demo runtime only the `NEO4J_*` values are read. The `timezone` demo's optional
  query-history count additionally reads `DATABRICKS_WAREHOUSE_ID` (and optionally
  `DATABRICKS_CONFIG_PROFILE`); see the `timezone` demo below.

  ```bash
  cp .env.sample .env   # then fill in the NEO4J_* values
  ```

## Step 1: Create the Silver tables

The Virtual Graph reads the Finance Genie Silver tables, so they must exist before you
build it. Run [`notebooks/01_setup_lakehouse.ipynb`](notebooks/01_setup_lakehouse.ipynb)
in your Databricks workspace. It downloads the Finance Genie dataset from the public
[graph-on-databricks](https://github.com/neo4j-partners/graph-on-databricks) repo, stages
the CSVs into a Unity Catalog Volume, and builds the five base tables (`accounts`,
`merchants`, `transactions`, `account_links`, `account_labels`) with their column
comments and foreign keys. Set the catalog / schema / volume in the notebook's
configuration cell; the defaults match the Finance Genie pipeline.

## Step 2: Build the Virtual Graph

Build a Virtual Graph over the Finance Genie Silver tables with node labels `:Account`
and `:Merchant` (and the `TRANSACTED_WITH` and `TRANSFERRED_TO` relationships between
them), set up as described in [virtual-graph.md](virtual-graph.md). That walkthrough
covers connecting Databricks as a data source in Aura, defining the schema, and
confirming the graph reads from the warehouse.

## Step 3: Run the demos

Run any demo from the project root (`uv run` installs the project and its dependencies
on first use):

```bash
uv run vg-demo                          # fraud demo (default): the 7 fast queries
uv run vg-demo --all                    # also attempt the slow / unsupported queries
uv run vg-demo --demo basic             # exploration / visualization queries
uv run vg-demo --demo fast-gds --since-hours 2   # working PageRank path (thin window)
uv run vg-demo --demo slow-gds          # demonstrate the GDS forms that fail
uv run vg-demo --demo gds-probe --since-hours 2   # sweep property projections (thin window)
uv run vg-demo --demo timezone --no-history        # quick: timing contrast only, ~15s
uv sync --extra history && uv run vg-demo --demo timezone   # full: timing + direct call count, ~60s
uv sync --extra history && uv run vg-demo --demo 100m       # zero-spill spike: query existing table, confirm from history
```

Useful flags: `--rows N` caps printed rows per query, `--timeout S` sets the per-query
server timeout (default 120s), `--query N` / `--only N M` pick specific fraud queries.

## Documents in this directory

| Document | What it covers |
|---|---|
| [`virtual-graph.md`](virtual-graph.md) | Step-by-step walkthrough to build the Virtual Graph over the Silver tables in Aura, plus the note on when to model transactions as nodes. |
| [`basic-graph-examples.md`](basic-graph-examples.md) | Warm-up counts and small relationship traversals that show the graph's value without fraud logic (backs `--demo basic`). |
| [`finding-fraud.md`](docs/finding-fraud.md) | Plain-English walkthrough of the fraud-signal queries and how to read them (backs `--demo fraud`). |
| [`best-practices.md`](best-practices.md) | How to write Cypher that pushes down well to Databricks, plus how the warehouse and the connection pool shape performance. |
| [`gds-guide.md`](gds-guide.md) | How to run Graph Data Science via a GDS Session on a Virtual Graph, including the no-write-back constraint. |
| [`gds-limitations.md`](gds-limitations.md) | Findings and current limitations from running GDS against a Virtual Graph. |

## What the demos provide

### `basic` demo

Counts, breakdowns, and small anchored traversals that show the value of the
relationships without any fraud logic.

- All queries run.
- `graph` queries return nodes and relationships, so the CLI prints only a row count and
  timing. Paste them into the Aura Workspace Query tab to see the picture.
- The demo prints the anchor account and merchant IDs it picked.
- Queries 12-15 are pushdown demonstrations from
  [`verify-best.md`](test-results/verify-best.md): UNION ALL becomes two pushed SQL
  statements, and a `LIMIT 25` pushes down at every depth (bounding the rows returned, not
  the join work behind them).

| # | What it does | Kind |
|---|---|---|
| 1 | Count of accounts | table |
| 2 | Count of merchants | table |
| 3 | Accounts grouped by type | table |
| 4 | Accounts grouped by region | table |
| 5 | Merchants grouped by category | table |
| 6 | Top 10 merchants by distinct customers | table |
| 7 | Ego network: one account and the merchants it shops at | graph |
| 8 | Ego network: one account and its transfer partners | graph |
| 9 | Merchant star: one merchant and the accounts that use it | graph |
| 10 | Two hops: accounts linked through a shared merchant | graph |
| 11 | Two hops: a transfer chain (who your counterparty pays) | graph |
| 12 | Count accounts and merchants in one statement via UNION ALL | table |
| 13 | Any 25 account-merchant edges (unanchored single-hop, LIMIT) | graph |
| 14 | Any 25 two-hop transfer chains (unanchored, LIMIT) | graph |
| 15 | Any 25 four-hop transfer chains (unanchored, LIMIT) | graph |

### `fraud` demo

The seven fast queries (1-7) are the pushdown-friendly forms from
[`finding-fraud.md`](docs/finding-fraud.md), each returning in a few seconds.

- They group by scalar IDs.
- Fan-in and fan-out queries reshape a `count(DISTINCT)` into pair-grouping plus a
  client-side rollup.
- The courier query splits a cross product into two merged halves.
- The server aggregates and orders; threshold filters and top-N run in Python; recent
  windows are passed as a precomputed `$since` parameter.

| # | What it does | Status |
|---|---|---|
| 1 | Structuring: accounts with many transfers sized just under $10,000 | works |
| 2 | Busy brand-new accounts: recently opened accounts already moving large volume | works |
| 3 | Round trips: account pairs paying each other both ways (wash activity) | works |
| 4 | Velocity ratio: accounts moving far more money than they hold | works |
| 5 | Collection accounts (fan-in): many distinct senders into one account | works |
| 6 | Spray accounts (fan-out): one account paying many distinct recipients | works |
| 7 | Courier accounts: heavy peer-to-peer transfers, little merchant spend | works |

The slow tier (8-11) has no fast equivalent and is kept to demonstrate what does not work.

- Skipped unless you pass `--all`.
- With `--all`, these run behind a printed warning, bounded by `--timeout`.
- Any error is caught and printed so the run continues.

| # | What it does | Status |
|---|---|---|
| 8 | Pass-through mule (local betweenness proxy) | slow: unbounded two-hop join, usually times out |
| 9 | Shared-merchant burst (coordinated ring) | slow: `collect(DISTINCT)` over a node group, hit the 120s timeout |
| 10 | Rapid-turnover per account | slow: unbounded two-hop join, usually times out |
| 11 | Layering cycles | unsupported: variable-length path `{2,4}` fails with `42NG0` |

### `fast-gds` demo

GDS is not an in-database plugin on the Virtual Graph. The path that works in practice is
a **GDS Session**:

- The Cypher-projection form of `gds.graph.project(...)` with a `{ memory }` config
  provisions an ephemeral session, then PageRank streams against the named in-memory graph.
- The demo runs its statements one at a time: size the window, drop any stale projection,
  project (this provisions the session), stream PageRank, drop.
- The "window" is a time-range filter on the transfer rows: `--since-hours` /
  `--since-days` keep only transfers from the most recent N hours or days, and that row
  count is the edge count projected into the graph.
- "Size the window" counts those rows without provisioning a session.

What it looks like on a thin window (the most recent 2 hours of transfers):

```
--- size window (count edges, last 2.0h)        OK 0.6s, 298 edges
--- project ... (provisions the session)        OK 90.9s, 556 nodes / 298 rels
--- PageRank stream (top 10)                     OK 3.9s, 10 rows
--- drop projection                              OK 1.0s
```

Notes:

- Almost all the time is session cold-start, not the query or the algorithm.
- Streamed `nodeId`s are GDS-internal IDs, not `account_id`s. Resolving them back is not
  reliable on the Virtual Graph yet, so the demo streams the raw ID and score.
- **Keep the window small.** A thin window (`--since-hours 2`, a few hundred edges)
  provisions and completes; the default 7-day window is about 23,000 edges, which trips
  the read timeout during provisioning (see the `slow-gds` demo below).

Flags:

- `--count-only` counts the rows in a window for free.
- `--since-hours` / `--since-days` scope the window.
- `--limit` changes the top-N.
- `--memory` sizes the session.
- `--keep` leaves the projection in place for reuse.

### `slow-gds` demo

Demonstrates the two GDS failure modes on the Virtual Graph, both caught and printed:

| Statement | What happens |
|---|---|
| Classic `CALL gds.graph.project('g', 'Account', 'TRANSFERRED_TO')` | rejected fast with `42NG0` (the label/type form is not supported) |
| Full-graph Cypher projection (every transfer) | provisions a session whose long, silent provisioning trips the 60s Bolt read timeout (observed at ~240s) or is reset by the server |

- `--read-timeout 0` lets the full projection survive past 60s to show the later server
  reset.
- The full projection cannot be cancelled once started and can saturate the pool, so run
  this on a clean instance.

### `gds-probe` demo

The fast-gds projection carries only labels and the relationship type; it never projects
`amount` or `transfer_timestamp` as graph properties. This demo isolates what happens when
you add properties: a GDS in-memory graph only accepts **numeric** property types, so it
sweeps a series of projections on a thin window where the timeout stays out of the
picture, adding one node or relationship property at a time, and reports which ones project
and which the server rejects. It first introspects the live schema and prints each property's type, then
runs:

| Scenario | Projection adds | Result |
|---|---|---|
| A_control | labels + `relationshipType` only | projects |
| B_rel_amount | `relationshipProperties { amount }` (float) + weighted PageRank | projects, weight usable |
| C_rel_timestamp | `relationshipProperties { transfer_timestamp }` (`DateTime`) | rejected fast |
| D_rel_both | `amount` + `transfer_timestamp` | rejected on the temporal one |
| E_node_numeric | a numeric node property on both endpoints | projects |
| F_node_nonnumeric | a string node property on both endpoints | rejected fast |

Each rejection comes back in under a second, before the session provisions, as
`IllegalArgumentException: The property ... contained a value of type DateTime/String,
which is not supported`. This is standard GDS typing, not a Virtual Graph defect: project
only numeric columns, and cast or drop temporal and string ones. See the modeling note in
[`gds-guide.md`](gds-guide.md). Use `--count-only` to introspect the schema without
provisioning, and `--since-hours` / `--since-days` to size the window.

### `timezone` demo

The single largest slow path the verification found: when a result carries TIMESTAMP
values, the engine makes one `SELECT current_timezone()` round trip to the warehouse for
every value it materializes into a Cypher datetime, run serially with no caching. DATE
values and plain scalars cost nothing. The demo runs five discriminating queries, each
capped at `LIMIT 25`, so the per-value cost is visible:

| # | Query | What it carries | Predicted calls |
|---|---|---|---|
| A | `RETURN t` (a `TRANSFERRED_TO` relationship) | a TIMESTAMP | 25 |
| B | `RETURN t.amount, t.link_id` | non-temporal scalars | 0 |
| C | `RETURN t.transfer_timestamp` | a bare TIMESTAMP scalar | 25 |
| D | `RETURN a` (an `Account` node) | only a DATE | 0 |
| E | rerun A twice back to back | a TIMESTAMP, no session cache | 25 + 25 |

Two signals are reported. Wall-clock is always available: the TIMESTAMP-bearing runs (A,
C, E) spend seconds on 25 rows while the scalar / DATE runs (B, D) are sub-second. When
the Databricks SDK is installed (`uv sync --extra history`) and a warehouse is configured
(`DATABRICKS_WAREHOUSE_ID`, optionally `DATABRICKS_CONFIG_PROFILE`, in `.env`), the demo
also pulls warehouse query history and counts the actual `current_timezone()` statements
per run, which is the direct proof. Query history lags about 11 minutes, so the demo polls
it every 3 minutes (up to 10 times, ~30 minutes) until all the expected statements have
ingested, then prints the per-run counts. `--no-history` reports wall-clock only;
`--history-wait S` sets the initial wait before the first poll. See
[`findings-summary.md`](findings-summary.md) and
[`verify-best.md`](test-results/verify-best.md) Phases 5 and 9.

### `100m` demo

The "zero spill from 100K to 100M rows" finding, reproduced on the SQL side. Unlike the
other demos, this one talks SQL straight to the backing Databricks warehouse via the
Databricks SDK (not Cypher over Bolt), because the finding is the SQL-side spike: it runs
the C1/C2/C3 aggregation SQL the Virtual Graph pushes down to, directly against
`account_links_large`, then reads `spill_to_disk_bytes` from warehouse query history to
show the warehouse never spills. The SDK ships as the `history` extra
(`uv sync --extra history`), and unlike the `timezone` demo it is required here since this
demo has no Bolt fallback.

| # | Query | What it carries |
|---|---|---|
| C1 | full-table group-by (per-sender rollup) | scan + hash aggregate over every row |
| C2 | windowed group-by (recent transfers only) | filtered scan + hash aggregate |
| C3 | high-cardinality pair group-by, top 100 | scan + two-key aggregate + order/limit |

Each C-query is wrapped in an outer `count(*)` so the full scan-and-aggregate cost is
paid without shipping result rows. By default the demo queries the existing table
read-only, then waits out the warehouse query-history lag (11-25 min, polling on an
interval with a countdown) until every statement finalizes, and prints the confirmatory
metrics with the zero-spill headline. `--build` first rebuilds `account_links_large` at
each ramp size (100K, 250K, 500K, 1M, 10M, 50M, 100M) with a destructive
`CREATE OR REPLACE`; `--sizes` picks specific ramp sizes; `--skip-history` runs the
C-queries but skips the history wait (spill left unconfirmed). Connection details
(profile, catalog, schema) come from the project `.env`; the warehouse defaults to the
backing Virtual Graph warehouse, overridable with `--warehouse`. See
[`findings-summary.md`](findings-summary.md) and
[`perf-tests-results-v2.md`](test-results/perf-tests-results-v2.md) Spike 1.

### Support scripts

These standalone scripts probe and stress the Virtual Graph; they share the connection
helper in `src/connection.py` (reads the project `.env`, or `PROBE_ENV` if set):

- `vg-probe` (`src/probe.py`): run a single ad-hoc Cypher statement and time it
  (`uv run vg-probe "<cypher>"`).
- `vg-heavy` (`src/heavy_run.py`): run the slow fraud queries sequentially with a
  per-query cap and a pool health check between each.
- `vg-viz` (`src/viz_check.py`): find real flagged accounts and confirm each anchored
  visualization renders small and fast.

## Reproducing the warehouse performance tests

For a condensed digest of every finding (warehouse sizing, GDS cost, the timezone round
trip, and what pushes down versus what stays engine-side), start with
[`findings-summary.md`](findings-summary.md); it links back to the source documents below.

The [`test-results/`](test-results/) directory holds the performance and verification work:

- [`perf-test.md`](test-results/perf-test.md): the seven-phase plan.
- [`perf-tests-results.md`](test-results/perf-tests-results.md): the recorded timings.
- [`perf-tests-results-v2.md`](test-results/perf-tests-results-v2.md): the Test set C ramp spike.
- [`verify-best.md`](test-results/verify-best.md): the best-practices verification.

To re-run them, three roles each have one tool that owns them:

| Role | Tool | Notes |
|---|---|---|
| **Reported timings** (the seconds in the results tables) | `vg-probe` client wall-clock | The load-bearing metric: `uv run vg-probe "<cypher>"`, the time the client waits for one statement. Not a Databricks measurement. |
| **Warehouse lifecycle** (cache-clearing restart, resize) | `databricks warehouses` CLI, or the `manage_sql_warehouse` MCP tool | `databricks warehouses stop/start/get <id>` clears the cache; resize with `databricks warehouses update <id> --cluster-size "Small"` or `manage_sql_warehouse(action="modify", ...)`. |
| **Databricks metrics** (exec time, spill, rows) | Databricks SQL Statements API via the CLI (primary); MCP `execute_sql` (optional) | Pull `system.query.history` with `databricks api post /api/2.0/sql/statements`, submitting async (`wait_timeout: "5s"`, `on_wait_timeout: "CONTINUE"`) and polling. |

Why this order:

- **The metrics pull leads with the Statements API, not the MCP `execute_sql` tool.** The
  MCP tool has a hard 60-second cap that ignores its timeout argument, and the history scans
  outrun it.
- **The async Statements API has no cap** and needs only the CLI plus a profile, which the
  lifecycle commands already require.
- **MCP `execute_sql` stays as an optional convenience**, usable when the scan is kept cheap.
- **`vg-probe` wall-clock is the primary reported metric** because `system.query.history`
  lags about 11 minutes; the Databricks-side detail only confirms what the wall-clock showed.

For the full worked example, one phase run end to end with every command, see
[`test-results/reproducing-warehouse-tests.md`](test-results/reproducing-warehouse-tests.md).