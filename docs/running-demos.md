# Running the Demos

This guide covers configuring the project, running each demo, and what each demo shows.
Set up a data source and build the Virtual Graph first. Follow
[`setup-databricks.md`](setup-databricks.md) or [`setup-aws.md`](setup-aws.md), then
[`vg-model.md`](vg-model.md).

## Configure `.env`

Install [uv](https://docs.astral.sh/uv/). Then create `.env` from the sample:

```bash
cp .env.sample .env
```

Fill in the Aura connection from the credentials file you downloaded when you created
the Virtual Graph:

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
  `virtual-graph-dbx`.
- **`DATABRICKS_SCHEMA`, for `100m` only.** The demo falls back to `SCHEMA`, then to
  `vg-schema`.
- **`PROBE_ENV`, from the shell only.** It points the demos at a different dotenv. The
  demos read it before loading any dotenv, so setting it inside `.env` has no effect.

## Run the demos

Run any demo from the project root (`uv run` installs the project and its dependencies
on first use):

```bash
uv run vg-demo                          # fraud demo (default): queries 1-10
uv run vg-demo --demo basic             # exploration / visualization queries
uv run vg-demo --demo fast-gds          # working PageRank path (7-day window)
uv run vg-demo --demo gds-probe         # sweep property projections (7-day window)
```

Useful flags: `--rows N` caps printed rows per query. `--timeout S` sets the per-query
server timeout, which defaults to 300s. `--rows 0` prints only the match count.
`--query N` and `--only N M` pick specific fraud queries. They cannot be combined. Every
demo exits 1 when a query, projection or statement fails, and a bad flag exits 2.
A flag the chosen demo does not read prints a warning on stderr, such as
`warning: --since-days has no effect with --demo basic`. The demo still runs.

## What each demo shows

### `basic` demo

Counts, breakdowns, and small anchored traversals that show the value of the
relationships without any fraud logic.

- All queries run. Each one finishes in 2s or less, and warm runs take 0.4 to 0.9s.
- `graph` queries return nodes and relationships, so the CLI prints only a row count and
  timing. Paste them into the Aura Workspace Query tab to see the picture.
- The demo anchors on account 17813 and merchant 1 and prints the IDs it used. If either
  ID is missing, it falls back to the lowest account ID with an outgoing transfer or the
  lowest merchant ID.
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

`uv run vg-demo` runs fraud queries 1-10 from [`finding-fraud.md`](finding-fraud.md)
by default. Each query runs under the `--timeout` cap, which defaults to 300s. The whole
run takes about 20 seconds.

- The queries group by scalar IDs, so each aggregation runs on Databricks as a pushed
  `GROUP BY`. Query 9 is the exception, because it groups by day.
- Thresholds and top-N run in Cypher. Each threshold sits in a second `WITH` after the
  aggregating `WITH`, which keeps the `GROUP BY` pushed down. `ORDER BY` with `LIMIT`
  picks the top rows.
- Queries 1-6 and 8-10 return at most 50 rows each, and `--rows` caps how many print,
  10 by default.
- Every fraud query sorts on a tie-breaking id, so its rows come back in the same order
  on every run.
- Fan-in and fan-out count distinct counterparties on the server with `count(DISTINCT ...)`.
- The courier query splits a cross product into two halves and joins them client-side.
  This split is needed because `OPTIONAL MATCH` is unsupported. Its `transferCount >= 100`
  threshold runs in Cypher. Only the `merchantCount < 20` check runs client-side, after
  the join.
- The rapid-turnover query computes its average turnaround in seconds from `toInteger()`
  timestamp arithmetic. The demo converts it to `avgTurnaroundHours`.
- Recent windows are passed as a precomputed `$since` parameter.
- Any error is caught and printed so the run continues.

| # | What it does | Status |
|---|---|---|
| 1 | Structuring: accounts with many transfers sized just under $10,000 | works, under 1s |
| 2 | New accounts: the most recently opened accounts, ranked by outflow | works, under 1s |
| 3 | Round trips: account pairs paying each other both ways (wash activity) | works, about 1s |
| 4 | Velocity ratio: accounts moving far more money than they hold | works, under 1s |
| 5 | Collection accounts (fan-in): many distinct senders into one account | works, under 1s |
| 6 | Spray accounts (fan-out): one account paying many distinct recipients | works, under 1s |
| 7 | Courier accounts: heavy peer-to-peer transfers, little merchant spend | works, about 4s |
| 8 | Pass-through mule (local betweenness proxy) | works, about 1-2s |
| 9 | Shared-merchant burst (coordinated ring) | works, about 5-9s |
| 10 | Rapid-turnover per account | works, about 1s |

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
| Size the window | under 1s warm, about 3s cold | 23,198 edges |
| Project, which provisions the session | 34 to 38s | 15,588 nodes and 23,198 relationships |
| Stream PageRank, top 10 | 2 to 3s | 10 rows |
| Drop the projection | 1 to 3s | graph dropped |

Notes:

- Almost all the time is session cold-start, not the query or the algorithm.
  Provisioning alone took 31 to 44s across runs.
- On a session conflict, the demo retries the projection once under a new name. Aura
  reports a conflict as a missing session or an existing graph mapping. Other projection
  errors stop the run with a nonzero exit. After any projection failure, the demo first
  tries to drop the graph.
- Streamed `nodeId`s are GDS-internal IDs, not `accountId`s. The formula
  `accountId = (nodeId & (2^50 - 1)) >> 1` decodes them. The demo prints the decoded
  `accountId` next to each `nodeId` and score. See [`gds-guide.md`](gds-guide.md).
- **Window size is flexible.** A thin window such as `--since-hours 2` holds a few
  hundred edges and is the cheapest run. A projection of all 300,000 transfers also
  completed, in 41.0s.

Flags:

- `--count-only` counts the rows in a window with one warehouse query and provisions no
  session.
- `--since-hours` / `--since-days` scope the window.
- `--limit` changes the top-N.
- `--memory` sizes the session.
- `--keep` leaves the projection in place for reuse. The output prints its name.

### `gds-probe` demo

The fast-gds projection carries only labels and the relationship type. This demo adds
usable properties to GDS projections:

- A GDS in-memory graph only accepts **numeric** property types.
- The demo converts transfer timestamps to epoch milliseconds with
  `WITH src, dst, t, toInteger(t.transferTimestamp) * 1000 AS transferTimestampMs`.
  The Virtual Graph pushes `toInteger()` on a timestamp down to SQL as epoch seconds, and
  the form raises no warning. Every timestamp is a whole second, so the values equal
  `epochMillis` exactly.
- The conversion has to go through a `WITH`. Inside the projection's config map,
  `toInteger()` on a timestamp fails with `22N38`.
- It leaves string identifiers outside the projection, where they can be looked up by
  account ID.
- The demo sweeps projections on the default 7-day window, adding properties one at a
  time and reporting which project and which are skipped.
- On the 7-day window, scenarios A to E each project in 33 to 46s. Weighted PageRank
  works on the projected `amount`.
- It first samples one row from the window and prints each property's type, then runs:

| Scenario | Projection adds | Result |
|---|---|---|
| A_control | labels + `relationshipType` only | projects |
| B_rel_amount | `relationshipProperties { amount }` (float) + weighted PageRank | projects, weight usable |
| C_rel_timestamp | `relationshipProperties { transferTimestampMs }` (`toInteger(t.transferTimestamp) * 1000`, bound in a `WITH`) | projects |
| D_rel_both | `amount` + `transferTimestampMs` | projects, weight usable |
| E_node_numeric | a numeric node property on both endpoints | projects |
| F_node_nonnumeric | a string or temporal node property on both endpoints | skipped before provisioning |

- A raw `DateTime` or `String` property would produce
  `IllegalArgumentException: The property ... contained a value of type DateTime/String,
  which is not supported`.
- Project numeric columns or numeric conversions of temporal values. Leave string
  identifiers in the source graph. See the modeling note in
  [`gds-guide.md`](gds-guide.md).
- `--count-only` sizes the window and samples one row without provisioning a session.
- `--since-hours` and `--since-days` size the window.

## The `100m` demo on Databricks (optional)

This demo runs on Databricks only. It does not apply to the S3 Tables setup.

The `100m` demo works differently from the other four. It sends SQL straight to a
Databricks SQL warehouse through the Databricks SDK. It never opens a Neo4j connection,
so it needs no Aura instance, no Virtual Graph and no graph mapping. It runs the kind of
aggregation SQL the Virtual Graph pushes down, against a 100M-row `account_links_large`
table. It then reads warehouse query history to confirm that the warehouse never spills
to disk.

It needs the following:

- **The `history` extra.** Run `uv sync --extra history` to install the Databricks SDK.
- **A Databricks profile.** The profile must exist in `~/.databrickscfg` with working
  credentials. Name it with `DATABRICKS_CONFIG_PROFILE` in `.env`, or pass `--profile`.
  The `NEO4J_*` values are not used.
- **A SQL warehouse you can use.** The warehouse is required and has no default. Set
  `VG_BACKING_WAREHOUSE_ID` in the environment or in `.env`, or pass `--warehouse <id>`.
  The flag wins over the setting. `DATABRICKS_WAREHOUSE_ID` is not read.
- **The `account_links_large` table.** The setup notebook does not create it. The demo
  looks for it in `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA`. Build it once with
  `--build`. The build reads the `accounts` table from the setup notebook and generates random
  transfers between those accounts. It needs permission to create a table in the schema.
- **Warehouse time for the build.** `--build` rebuilds the table at seven ramp sizes, from
  100K up to 100M rows, with a destructive `CREATE OR REPLACE`. Pass `--sizes 100000000`
  to build only the final size.

```bash
uv sync --extra history
uv run vg-demo --demo 100m --build --sizes 100000000   # first run: build the 100M-row table
uv run vg-demo --demo 100m                             # later runs: query the existing table
```

A run waits up to a few minutes for query history to land before it prints the spill
metrics. The next section covers the queries, the recorded metrics and every flag.

### What the `100m` demo measures

The "zero spill from 100K to 100M rows" finding, reproduced on the SQL side:

- Unlike the other demos, this one talks SQL straight to the backing Databricks warehouse
  via the Databricks SDK instead of Cypher over Bolt, because the finding is the SQL-side
  spike.
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
  `~/.databrickscfg` alone. The warehouse comes from `VG_BACKING_WAREHOUSE_ID` or
  `--warehouse`. It has no default, and the demo stops if neither is set.

Flags:

- `--build` first rebuilds `account_links_large` at each ramp size (100K, 250K, 500K, 1M,
  10M, 50M, 100M) with a destructive `CREATE OR REPLACE`.
- `--sizes` picks specific ramp sizes.
- `--skip-history` runs the C-queries but skips the history wait, so spill stays
  unconfirmed.
- `--warehouse` overrides the warehouse.
- `--profile` overrides the Databricks profile.
- `--history-lag-minutes` sets the estimated history lag used for the countdown. The
  default is 3 minutes.
- `--poll-minutes` sets how often the demo polls history. The default is 3 minutes.
- `--max-wait-minutes` sets when the demo stops polling. The default is 40 minutes.

## Generate a larger graph and run `gds-examples` (optional)

`vg-generate` runs on Databricks only. On the S3 Tables setup, `gds-examples` still runs
on the base dataset, with fewer edges.

> **WARNING: running the generator changes all results.** `vg-generate --yes` appends
> rows to the existing `accounts`, `merchants`, `transactions` and `account_links`
> tables. Every demo reads those tables through the Virtual Graph, so
> the results of every demo change afterward, including the fraud queries and any
> Genie ground truth built from the original data. Each run appends another batch.
> There is no undo short of re-running the setup notebook.

The generator (`src/generate_large.py`) runs from your machine but does all the work on
the SQL warehouse, with `INSERT INTO ... SELECT` over `range()`. By default it adds
200,000 accounts, 60,000 merchants, 2.5M transactions and about 2.46M transfers. It
draws 2.5M transfers and drops the self-transfers, about 1.6% of them. New IDs start
above each table's current maximum. The new accounts are split into communities of 50,
and 80% of transfers stay inside a community, so WCC and Louvain have structure to
find. The new rows link only to each other, so the generated subgraph has no edges to
the original data. Timestamps fall in the original 2024-01-01 to 2024-03-30 range.

It uses the same profile, warehouse, catalog and schema settings as the `100m` demo.
Writing needs the same `history` extra. Without `--yes` it prints the plan and
writes nothing.

```bash
uv sync --extra history
uv run vg-generate                    # print the plan only
uv run vg-generate --yes              # append the rows (changes all demo results)
uv run vg-generate --yes --transactions 3000000 --transfers 3000000
```

The `gds-examples` demo then projects every transfer once and runs PageRank, WCC and
Louvain on the same GDS Session. The graph is much larger than the 7-day window of
`fast-gds`, so size it first and raise `--memory` if the projection runs out.
The generated transfers also land in the 7-day window of `fast-gds` and `gds-probe`.
With the defaults that window gains about 190,000 edges, so those demos may need a
larger `--memory` too.

```bash
uv run vg-demo --demo gds-examples --count-only   # count the edges, no session
uv run vg-demo --demo gds-examples --memory 8GB   # project, run all three, drop
```

`gds-examples` also accepts `--graph`, `--limit` and `--keep`, which work as they do for `fast-gds`.

## Support scripts

These standalone scripts probe and stress the Virtual Graph. They share the connection
helper in `src/connection.py`, which reads the project `.env`, or `PROBE_ENV` if set:

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
  [`finding-fraud.md`](finding-fraud.md) use fraud-labeled anchors 3375 and 2599
  instead.
