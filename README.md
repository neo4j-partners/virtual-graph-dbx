# Virtual Graph Demo

This project demonstrates the Neo4j Virtual Graph, which enables zero-copy querying of
Databricks lakehouses with Cypher. The Virtual Graph compiles Cypher into SQL and pushes
most of the work down to the backing Databricks SQL warehouse; graph-specific operations
are handled by Neo4j's graph compute layer, and how much runs where depends on the query.
The project walks through setting up the Finance Genie sample lakehouse on Databricks,
creating a Virtual Graph over it in Aura, and querying that graph. It also documents best
practices, how to use GDS Sessions, and the current limitations of the Virtual Graph.

Finance Genie is a synthetic dataset of bank accounts, merchants, and the transfers
between them. This demo uses only that synthetic dataset.

Virtual Graph is in public preview. The official docs advise against using sensitive or
production data with it during the preview. It is available to AuraDB Professional and
Business Critical customers. Billing started on 2026-09-01: instances use Aura Credits
based on their memory size, at parity with AuraDB Pro pricing. There is no GA date yet.

For further information, see:

- [Introducing Neo4j Virtual Graph](https://neo4j.com/blog/graph-database/introducing-neo4j-virtual-graph-graph-reasoning-on-the-data-you-already-have/)
- [Getting started with Databricks](https://neo4j.com/docs/virtual-graph/aura/getting-started-databricks/)

This [uv](https://docs.astral.sh/uv/) Python demo runs Cypher against the Finance Genie
Virtual Graph. It includes the following demo sets:

| Demo | What it is | Everything works? |
|---|---|---|
| `basic` | warm-up exploration and visualization queries | yes |
| `fraud` | fraud-signal queries 1-10 from `finding-fraud.md` | yes |
| `fast-gds` | the working GDS Session + PageRank path | yes |
| `gds-probe` | sweep numeric node / relationship property projections and show how to handle unsupported types | projects or skips unsupported properties |
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

  The demos read these variables:

  - **Required for every demo.** `NEO4J_URI`, `NEO4J_USERNAME` and `NEO4J_PASSWORD`.
  - **`DATABRICKS_CONFIG_PROFILE`, for `100m` only.** It names the Databricks profile in
    `~/.databrickscfg`. The demo falls back to `DATABRICKS_PROFILE`, then to `DEFAULT`.
  - **`DATABRICKS_CATALOG`, for `100m` only.** The demo falls back to `CATALOG`, then to
    `graph-on-databricks`.
  - **`DATABRICKS_SCHEMA`, for `100m` only.** The demo falls back to `SCHEMA`, then to
    `graph-enriched-schema`.
  - **`PROBE_ENV`, from the shell only.** It points the demos at a different dotenv. The
    demos read it before loading any dotenv, so setting it inside `.env` has no effect.

  ```bash
  cp .env.sample .env   # then fill in the NEO4J_* values
  ```

## Step 1: Run the setup notebook in Databricks

The Virtual Graph reads the Finance Genie Silver tables, so they must exist before you
build it. Run [`notebooks/01_setup_lakehouse.ipynb`](notebooks/01_setup_lakehouse.ipynb)
in your Databricks workspace to set up the data for the Virtual Graph. The notebook:

- Downloads the Finance Genie dataset from the public
  [graph-on-databricks](https://github.com/neo4j-partners/graph-on-databricks) repo.
- Stages the CSVs into a Unity Catalog Volume.
- Builds the five base tables (`accounts`, `merchants`, `transactions`, `account_links`,
  `account_labels`) with their column comments and foreign keys.

Set the catalog / schema / volume in the notebook's configuration cell; the defaults
match the Finance Genie pipeline.

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
uv run vg-demo                          # fraud demo (default): queries 1-10
uv run vg-demo --demo basic             # exploration / visualization queries
uv run vg-demo --demo fast-gds          # working PageRank path (7-day window)
uv run vg-demo --demo gds-probe         # sweep property projections (7-day window)
uv sync --extra history && uv run vg-demo --demo 100m       # zero-spill spike: query existing table, confirm from history
```

Useful flags: `--rows N` caps printed rows per query. `--timeout S` sets the per-query
server timeout, which defaults to 300s. `--query N` and `--only N M` pick specific fraud
queries.

## Documents in this directory

| Document | What it covers |
|---|---|
| [`virtual-graph.md`](virtual-graph.md) | Step-by-step walkthrough to build the Virtual Graph over the Silver tables in Aura, plus the note on when to model transactions as nodes. |
| [`basic-graph-examples.md`](basic-graph-examples.md) | Warm-up counts and small relationship traversals that show the graph's value without fraud logic (backs `--demo basic`). |
| [`finding-fraud.md`](docs/finding-fraud.md) | Walkthrough of the fraud-signal queries and how to read them (backs `--demo fraud`). |
| [`best-practices.md`](best-practices.md) | How to write Cypher that pushes down well to Databricks, plus how the warehouse and the connection pool shape performance. |
| [`gds-guide.md`](gds-guide.md) | How to run Graph Data Science via a GDS Session on a Virtual Graph, including the no-write-back constraint. |

## What the demos provide

### `basic` demo

Counts, breakdowns, and small anchored traversals that show the value of the
relationships without any fraud logic.

- All queries run. Each one finishes in 0.5 to 1.4s.
- `graph` queries return nodes and relationships, so the CLI prints only a row count and
  timing. Paste them into the Aura Workspace Query tab to see the picture.
- The demo prints the anchor account and merchant IDs it picked.
- Queries 12-15 are pushdown demonstrations: UNION ALL becomes two pushed SQL
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

`uv run vg-demo` runs fraud queries 1-10 from [`finding-fraud.md`](docs/finding-fraud.md)
by default. Each query runs under the `--timeout` cap, which defaults to 300s. The whole
run takes about 4 minutes.

- The queries group by scalar IDs.
- Thresholds and top-N run in Cypher. A HAVING-style `WHERE` after an aggregating `WITH`
  applies each threshold, and `ORDER BY` with `LIMIT` picks the top rows.
- Queries 1-6 and 8-10 return at most 50 rows each, and `--rows` caps how many print,
  10 by default.
- Every fraud query sorts on a tie-breaking id, so its rows come back in the same order
  on every run.
- Fan-in and fan-out count distinct counterparties on the server with `count(DISTINCT ...)`.
- The courier query splits a cross product into two halves and joins them client-side.
  This split is needed because `OPTIONAL MATCH` is unsupported. Its `transfer_count >= 100`
  threshold runs in Cypher. Only the `merchant_count < 20` check runs client-side, after
  the join.
- The rapid-turnover query returns its average turnaround as a Cypher Duration from
  `duration.inSeconds`. The demo converts it to `avg_turnaround_hours`.
- Recent windows are passed as a precomputed `$since` parameter.
- Any error is caught and printed so the run continues.

| # | What it does | Status |
|---|---|---|
| 1 | Structuring: accounts with many transfers sized just under $10,000 | works, under 1s |
| 2 | New accounts: the most recently opened accounts, ranked by outflow | works, under 1s |
| 3 | Round trips: account pairs paying each other both ways (wash activity) | works, about 3-4s |
| 4 | Velocity ratio: accounts moving far more money than they hold | works, under 1s |
| 5 | Collection accounts (fan-in): many distinct senders into one account | works, under 1s |
| 6 | Spray accounts (fan-out): one account paying many distinct recipients | works, under 1s |
| 7 | Courier accounts: heavy peer-to-peer transfers, little merchant spend | works, about 15s |
| 8 | Pass-through mule (local betweenness proxy) | works, about 4-6s |
| 9 | Shared-merchant burst (coordinated ring) | works, about 5s |
| 10 | Rapid-turnover per account | works, about 210s |

Query 11, layering cycles, is not run. The demo prints "Not run" for it, because the
Virtual Graph does not support it. Its quantified path `{2,4}` fails with `42NG1: Equijoin on the outer nodes of a quantified
path pattern is not supported`. The cycles recipe in
[`best-practices.md`](best-practices.md#adaptation-recipes) describes the workaround.

### `fast-gds` demo

GDS is not an in-database plugin on the Virtual Graph. The path that works in practice is
a **GDS Session**:

- The Cypher-projection form of `gds.graph.project(...)` with a `{ memory }` config
  provisions an ephemeral session, then PageRank streams against the named in-memory graph.
- The demo runs its statements one at a time: size the window, project (this provisions
  the session), stream PageRank, drop.
- Each default run adds a random suffix to the graph name, so a failed session cannot
  leave a name collision for the next run. With `--graph NAME`, the demo drops a stale
  graph of that name first.
- The "window" is a time-range filter on the transfer rows. `--since-hours` and
  `--since-days` keep only transfers from the most recent N hours or days. That row count
  is the edge count projected into the graph. The default window is 7 days.
- "Size the window" counts those rows without provisioning a session.

A default run on the 7-day window looks like this:

| Step | Time | Result |
|---|---|---|
| Size the window | about 3s | 23,198 edges |
| Project, which provisions the session | 35 to 38s | 15,588 nodes and 23,198 relationships |
| Stream PageRank, top 10 | 2 to 3s | 10 rows |
| Drop the projection | about 1s | graph dropped |

Notes:

- Almost all the time is session cold-start, not the query or the algorithm.
  Provisioning alone took 31 to 44s across runs.
- On a session conflict, the demo retries the projection once under a new name. Aura
  reports a conflict as a missing session or an existing graph mapping. Other projection
  errors stop the run with a nonzero exit.
- Streamed `nodeId`s are GDS-internal IDs, not `account_id`s. The formula
  `account_id = (nodeId & (2^50 - 1)) >> 1` decodes them. The demo prints the decoded
  `account_id` next to each `nodeId` and score. See [`gds-guide.md`](gds-guide.md).
- **Window size is flexible.** A thin window such as `--since-hours 2` holds a few
  hundred edges and is the cheapest run. A projection of all 300,000 transfers also
  completed, in 41.0s.

Flags:

- `--count-only` counts the rows in a window for free.
- `--since-hours` / `--since-days` scope the window.
- `--limit` changes the top-N.
- `--memory` sizes the session.
- `--keep` leaves the projection in place for reuse. The output prints its name.

### `gds-probe` demo

The fast-gds projection carries only labels and the relationship type. This demo adds
usable properties to GDS projections:

- A GDS in-memory graph only accepts **numeric** property types.
- The demo converts transfer timestamps to epoch milliseconds with
  `WITH src, dst, t, toInteger(t.transfer_timestamp) * 1000 AS transfer_timestamp_ms`.
  The Virtual Graph pushes `toInteger()` on a timestamp down to SQL as epoch seconds, and
  the form raises no warning. Every timestamp is a whole second, so the values equal
  `epochMillis` exactly.
- The conversion has to go through a `WITH`. Inside the projection's config map,
  `toInteger()` on a timestamp fails with `22N38`.
- It leaves string identifiers outside the projection, where they can be looked up by
  account ID.
- The demo sweeps projections on the default 7-day window, adding properties one at a
  time and reporting which project and which are skipped.
- On the 7-day window, scenarios A to E each project in 34 to 46s. Weighted PageRank
  works on the projected `amount`.
- It first introspects the live schema and prints each property's type, then runs:

| Scenario | Projection adds | Result |
|---|---|---|
| A_control | labels + `relationshipType` only | projects |
| B_rel_amount | `relationshipProperties { amount }` (float) + weighted PageRank | projects, weight usable |
| C_rel_timestamp | `relationshipProperties { transfer_timestamp_ms }` (`toInteger(t.transfer_timestamp) * 1000`, bound in a `WITH`) | projects |
| D_rel_both | `amount` + `transfer_timestamp_ms` | projects, weight usable |
| E_node_numeric | a numeric node property on both endpoints | projects |
| F_node_nonnumeric | a string or temporal node property on both endpoints | skipped before provisioning |

- A raw `DateTime` or `String` property would produce
  `IllegalArgumentException: The property ... contained a value of type DateTime/String,
  which is not supported`.
- Project numeric columns or numeric conversions of temporal values. Leave string
  identifiers in the source graph. See the modeling note in
  [`gds-guide.md`](gds-guide.md).
- `--count-only` introspects the schema without provisioning; `--since-hours` /
  `--since-days` size the window.

### `100m` demo

The "zero spill from 100K to 100M rows" finding, reproduced on the SQL side:

- Unlike the other demos, this one talks SQL straight to the backing Databricks warehouse
  via the Databricks SDK (not Cypher over Bolt), because the finding is the SQL-side spike.
- It runs the C1/C2/C3 aggregation SQL the Virtual Graph pushes down to, directly against
  `account_links_large`. On the 100M-row table the C-queries take 0.9 to 4.5s of client
  wall time. The demo then reads
  `spill_to_disk_bytes` from warehouse query history to show the warehouse never spills.
- The SDK ships as the `history` extra (`uv sync --extra history`). It is required,
  because this demo has no Bolt fallback.

| # | Query | What it carries |
|---|---|---|
| C1 | full-table group-by (per-sender rollup) | scan + hash aggregate over every row |
| C2 | windowed group-by (recent transfers only) | filtered scan + hash aggregate |
| C3 | high-cardinality pair group-by, top 100 | scan + two-key aggregate + order/limit |

Notes:

- Each C-query is wrapped in an outer `count(*)` so the scan-and-aggregate cost is paid
  without shipping result rows. The C1 and C2 wrappers also sum or take the max of every
  inner aggregate, so the optimizer keeps all of them.
- The wrapper also selects `current_timestamp()`. That non-deterministic column keeps the
  warehouse from serving the statement from its result cache.
- A run on the 100M-row table on 2026-09-23 gave these history metrics. No statement came
  from the cache, and every one had zero spill. History landed on the first check.

  | # | Execution | `read_rows` | Groups | Client wall time |
  |---|---|---|---|---|
  | C1 | 733ms | 100,000,000 | 25,000 | 1.4s |
  | C2 | 248ms | 7,778,336 | 25,000 | 0.9s |
  | C3 | 3,836ms | 100,000,000 | 100 | 4.5s |

- By default the demo queries the existing table read-only, then waits out the warehouse
  query-history lag of up to a few minutes, polling with a countdown until every
  statement finalizes. It then prints the confirmatory metrics.
- A statement served from the result cache prints as "cached, not measured". The
  zero-spill headline prints only when no statement was cached or pending.
- The profile name, catalog and schema come from the project `.env`. The demo reads
  `.env` without exporting it, so the host and credentials come from the named profile in
  `~/.databrickscfg` alone. The warehouse defaults to the backing Virtual Graph warehouse.

Flags:

- `--build` first rebuilds `account_links_large` at each ramp size (100K, 250K, 500K, 1M,
  10M, 50M, 100M) with a destructive `CREATE OR REPLACE`.
- `--sizes` picks specific ramp sizes.
- `--skip-history` runs the C-queries but skips the history wait (spill left unconfirmed).
- `--warehouse` overrides the warehouse.
- `--profile` overrides the Databricks profile.
- `--history-lag-minutes` sets the estimated history lag used for the countdown. The
  default is 3 minutes.
- `--poll-minutes` sets how often the demo polls history. The default is 3 minutes.
- `--max-wait-minutes` sets when the demo stops polling. The default is 40 minutes.

### Support scripts

These standalone scripts probe and stress the Virtual Graph; they share the connection
helper in `src/connection.py` (reads the project `.env`, or `PROBE_ENV` if set):

- `vg-probe` (`src/probe.py`): run a single ad-hoc Cypher statement and time it
  (`uv run vg-probe "<cypher>"`). `uv run vg-probe --help` prints its usage. It runs the
  statement with `execute_query`, which retries a read that hits the 60s Bolt read
  timeout, so a query longer than a minute runs twice. Run heavy queries such as query 10
  through `vg-demo`.
- `vg-viz` (`src/viz_check.py`): find a collection account, a spray account and a
  round-trip pair, then confirm each anchored visualization renders small and fast. The
  finders count distinct counterparties with `count(DISTINCT ...)` and sort on a
  tie-breaking id. The pair finder uses the query 3 form. The fan-in and fan-out pictures
  use the same 7-day window as queries 5 and 6.
- `vg-viz` anchors on the top-ranked account from each finder. Those accounts can be
  legitimate hubs. In the current data it picks collection account 184, with 24 senders
  and 24 rows, and spray account 16570, with 26 rows. It prints the pair 7855 <-> 13727
  with a round-trip volume of 122,721.78 and 2 rows. The examples in
  [`docs/finding-fraud.md`](docs/finding-fraud.md) use fraud-labeled anchors 3375 and 2599
  instead.

## Release notes

### 2026-09: Virtual Graph engine update

The latest Virtual Graph update is a big step forward. We re-ran every demo, support script, and documented finding in this project against it.

**Faster queries across the board**

- A full scan of the 300,000-row transfer relationship now aggregates in 3.6s. It used to take 40 to 45s.
- Grouping by a whole node now pushes down to Databricks. The structuring query runs in 1.1s, down from about 38s. The new-account velocity query runs in 1.3s, down from about 985s.
- `count(DISTINCT ...)` is now fast. A 7-day window takes 2.0s, down from more than 5 minutes.
- An unanchored four-hop traversal with `LIMIT 25` returns in 2.4s, down from about 14s.
- Each of the 15 `basic` queries finishes in 0.4 to 1.7s.
- The fraud visualization stars render in about 1s each, down from 9 to 10s.

**TIMESTAMP results are no longer slow**

- The engine no longer makes a `current_timezone()` round trip for each TIMESTAMP value. Warehouse query history shows zero such statements.
- Queries that return relationships or timestamp columns now come back in under 2s at 25 rows.

**The slow fraud queries now finish**

- The shared-merchant burst query (query 9) completes in about 5s. It used to exceed the 120s timeout.
- The rapid-turnover query (query 10) completes in about 210s, inside the 300s default timeout. It used to run past 100s without finishing.
- The pass-through mule query (query 8) runs in about 4 to 6s with its output column renamed.

**More Cypher runs on the server**

- A HAVING-style `WHERE` on an aggregate alias now works, so threshold filters can stay in Cypher.
- `IS NULL`, `IS NOT NULL`, and `range()` now work.
- Returning a node property alongside an aggregate of that node works.
- Error messages are more specific. The engine now returns `42NG1` with a reason, such as `Aggregating WITH clause is not supported`.
- `OPTIONAL MATCH` now fails fast with a clear message. This matches the official coverage docs.

**Safer long-running queries**

- The Bolt transaction timeout is now honored.
- A query stopped by the timeout leaves no warehouse statement running, so it no longer ties up the connection pool.

**GDS Sessions are faster and handle more data**

- Session provisioning now takes 31 to 44s, down from about 91s.
- The default 7-day window of 23,198 edges projects in 35 to 38s, and PageRank streams the top 10 in 2 to 3s.
- A projection of all 300,000 transfers now completes in 41s.
- Streamed `nodeId`s now decode back to `account_id` with `(nodeId & (2^50 - 1)) >> 1`.

**Databricks side**

- Warehouse query history now lands in about 3 minutes, down from 11 to 25 minutes.
- The 100M-row spike still runs with zero spill.

**Project changes**

- The `slow-gds` demo was removed. Its main failure case, the full-graph projection, now succeeds.
- References to the classic `CALL gds.graph.project('g', 'Account', ...)` form were removed. The Cypher-projection form is the supported path.
- The `timezone` demo was removed. The engine no longer makes the per-row `current_timezone()` round trip it measured.
- The `vg-heavy` support script was removed.
- The fraud demo now runs as one tier. `uv run vg-demo` runs queries 1-10, and the default `--timeout` is 300s.
- Query 8, the pass-through mule, was fixed. It now uses a distinct output alias. It groups per incoming transfer first, adds a `forwarded_in` column, and counts each incoming transfer's dollars once in `volume`. It runs in about 4 to 6s.
- Query 3 now counts real transfers and real dollars. The pattern matches once per pair of transfers, so the query counts each direction with `count(DISTINCT ...)` and divides each direction's sum by the other direction's count. The top pair is 7855 and 13727.
- Every fraud query and the `basic` breakdowns now sort on a tie-breaking id, so tied rows come back in a fixed order.
- `basic` query 8 now returns the 25 most recent transfers, so it shows money in both directions. Query 11 now returns the path, so the relationships draw.
- The `gds-probe` timestamp scenarios now project `toInteger(t.transfer_timestamp) * 1000`, bound in a `WITH`. This form raises no `01N52` warning. The sweep runs on the default 7-day window.
- The `100m` demo now bypasses the warehouse result cache and reports a cached statement as "cached, not measured". It reads `.env` without exporting it, so the Databricks profile alone supplies the host and credentials.
- Query 10 now returns its average turnaround as a Duration from `duration.inSeconds` and converts it to hours in Python. This form runs without the `01N52` unknown-property warning, and its values match Databricks SQL exactly.
- Fraud thresholds and top-N now run in Cypher instead of Python.
- The `fast-gds` demo now prints the decoded `account_id` next to each streamed `nodeId`.
- The `fast-gds` demo now gives each default run a unique graph name, so a failed session never blocks the next run. On a session conflict it retries once under a new name.
- The read-timeout flags for GDS provisioning were removed. The default 7-day projection finishes without them.
