# Virtual Graph Best Practices

How to write Cypher that runs well on the Finance Genie Virtual Graph. Aura
compiles Cypher into SQL and pushes most of the work down to the backing Databricks SQL
warehouse, so the rules here are about helping that translation push work down to
Databricks instead of dragging rows back to the graph engine.

The working fraud queries this guide draws on are the demo set in
[`finding-fraud.md`](docs/finding-fraud.md); the warm-up and visualization queries are in
[`basic-graph-examples.md`](basic-graph-examples.md). This document is the reference
for *why* those queries are shaped the way they are, and what to do when a standard
Cypher query will not run.

## How the Virtual Graph works

- **Translation.** Most of a Cypher query becomes SQL over Databricks, with
  graph-specific work handled by the engine. Only a subset of Cypher is
  supported, so a query written for a loaded Aura graph rarely runs verbatim. The
  reference forms in the appendix show the gap.
- **The shared query shape.** Filter, group, and apply thresholds in the query, then order
  and limit in Cypher. Row-level filters such as a time window, an amount range, or an
  account-id anchor push down to Databricks. A `GROUP BY` on a key column pushes down too.
  A HAVING-style `WHERE` on an aggregate alias runs as part of the query, so thresholds stay
  in Cypher. `ORDER BY` runs in the graph engine rather than the warehouse, but it is still
  worth writing. Almost every adaptation in this guide follows from that split.
- **Property mapping.** Relationship and node properties exist only if they were mapped
  from a backing table column in the Aura model. An unmapped column leaves the
  relationship in place with zero properties, and any query touching it fails with
  "Could not resolve property".
- **Labels.** Node labels follow the Neo4j convention of singular PascalCase: `:Account`
  and `:Merchant`. The backing tables keep their SQL names, `accounts` and `merchants`.
  **Generate from schema** names labels after the tables, so rename them in the Aura model
  as described in section 5 of [`virtual-graph.md`](virtual-graph.md). Every query in this
  project uses `:Account` and `:Merchant`.
  - `TRANSFERRED_TO` (`:Account` → `:Account`): `amount`, `transfer_timestamp`, `link_id`.
  - `TRANSACTED_WITH` (`:Account` → `:Merchant`): `amount`, `txn_timestamp`, `txn_hour`, `txn_id`.

Query shape is the first thing to get right. The timings in this guide were measured on
2026-09-23. The absolute seconds for any given query also depend on the warehouse size and
the connection pool. Treat the numbers here as rough and directional.

## What governs performance

Performance comes down to two cost centers plus the machine everything runs on. A few
terms used throughout:

- **Scalar:** A scalar is a single plain value, such as an `account_id`. A whole node
  object is the opposite.
- **Node:** A node is a full graph object, such as an `:Account`, that carries all its
  properties.
- **Pushdown:** Pushdown lets Databricks do the counting and summing. This is the fast path.
- **Materialize:** The graph engine materializes a result when it pulls every matching row
  back to itself first and then counts. This is the slow path.
- **Cardinality:** Cardinality is how many rows a query touches or returns. High
  cardinality means a lot of rows.

**Cost center 1, where the math happens.** Either Databricks or the graph engine does the
counting and summing. Databricks is the fast path. The patterns below keep this work on
Databricks.

**Cost center 2, how much data moves.** Every result row travels back over the wire.
This bites even when cost center 1 is perfect.

**The machine underneath.** Once the query shape is right, the absolute wall-clock time
also depends on the warehouse size and the connection pool, covered under
[Performance and the connection pool](#performance-and-the-connection-pool).

The next sections are the patterns. Each one is stated once, with the worked example
that measured it.

### Pattern 1: group by scalar keys

Group by a scalar key such as `a.account_id` rather than by the whole node `a`. On this
data both forms push the `GROUP BY` down to the warehouse and return in about a second.
The scalar form is still the better default. It names the output column explicitly. It
also returns plain values instead of node objects, which keeps each result row small.

- **Structuring (just-under-threshold transfers).** Grouping by scalar `src.account_id`
  pushes the `GROUP BY` down to the warehouse. The node-grouped form, `WITH src, count(t)
  ...`, also pushes down and returned 196 rows in 1.1s.

  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO]->(:Account)
  WHERE t.amount >= 9000 AND t.amount < 10000
  WITH src.account_id AS account_id, count(t) AS near_threshold, round(sum(t.amount), 2) AS total
  RETURN account_id, near_threshold, total
  ORDER BY near_threshold DESC
  ```

- **New account, high velocity.** The scalar form groups by `a.account_id` and carries
  `a.opened_date` and `a.holder_age` as extra grouping keys. Those values are constant per
  account, so they do not split any group. The node-grouped form `WITH a, count(t) ...`
  returned the same 452 rows in 1.3s.

  ```cypher
  MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
  WHERE a.opened_date >= date("2022-11-06")
  WITH a.account_id AS account_id, a.opened_date AS opened_date,
       a.holder_age AS holder_age, count(t) AS transfers, sum(t.amount) AS outflow
  RETURN account_id, opened_date, holder_age, transfers, outflow
  ```

Carry any constant node property you need, such as the balance or the opened date, as an
additional scalar grouping key. Do not group by the node just to keep that property.

### Pattern 2: count distinct values on the server

`count(DISTINCT x)` is fast on this data. With a scalar group key, it ran in 1.5s on a
1-day window and 2.0s on a 7-day window. The fan-in form with `count(DISTINCT src)` grouped
by the `dst` node ran in 1.8s on the 7-day window. Write the distinct count in Cypher and
let the server return one row per account.

- **Fan-in by distinct senders.** Group by the recipient and count distinct senders.

  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
  WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
  WITH dst.account_id AS recipient, count(DISTINCT src.account_id) AS senders,
       count(t) AS transfers, sum(t.amount) AS inflow
  RETURN recipient, senders, transfers, inflow
  ```

- **Fan-out by distinct recipients** is the mirror. Group by `src.account_id` and count
  distinct `dst.account_id`.

The same `count(DISTINCT ...)` form gives hub statistics. In-degree is
`count(DISTINCT src.account_id)` grouped by the recipient. Out-degree is
`count(DISTINCT dst.account_id)` grouped by the sender. The incoming transfer count is
`count(t)` in the same `WITH` as the in-degree.

**Aliasing rule.** An aggregation over two `Account` variables must alias its key as
`recipient`, `sender`, or `mule_id` in the `WITH`, never `account_id`. A `WITH` alias that
repeats the backing column name fails with `AMBIGUOUS_REFERENCE`. For example,
`WITH dst.account_id AS account_id` fails in a query that also binds `src`. Rename the key
in `RETURN` with `RETURN recipient AS account_id` if you need the familiar column name. The
pass-through mule query aliases its key as `mule_id` for the same reason. The
`TRANSACTED_WITH` backing table also exposes its own
`account_id` column. On the merchant side, alias the group key to another name and rename
it in `RETURN`.

### Pattern 3: filter aggregates, order, and limit in Cypher

Write the threshold, the sort, and the top-N in Cypher. A HAVING-style `WHERE` after an
aggregating `WITH` filters on the aggregate alias. `ORDER BY` and `LIMIT` then rank and
trim what is left. A test query of the form `WITH a.account_id AS id, count(t) AS c WHERE
c > 40` returned rows in 14.6s cold and 5.4s warm. The form runs with or without the
`CYPHER 25` prefix. See [Cypher coverage](#cypher-coverage).

- **Fan-in by transfer count.** This query uses a scalar group key, `count(t)`, and
  `sum(t.amount)`. The `transfers >= 5` threshold, the sort, and the top-N all stay in
  Cypher.

  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
  WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
  WITH dst.account_id AS recipient, count(t) AS transfers, sum(t.amount) AS inflow
  WHERE transfers >= 5
  RETURN recipient AS account_id, transfers, inflow
  ORDER BY transfers DESC, account_id
  LIMIT 50
  ```

`ORDER BY` and `LIMIT` do not reach the warehouse on an aggregation. The captured SQL is
byte-identical with or without them. The warehouse produces the full group set, and the
graph engine sorts and trims it afterward as a post-processing step. Add a secondary sort
key such as `account_id` so rows that tie at the `LIMIT` cutoff come back in a fixed order.
Every demo fraud query except the courier query does this. At tens of thousands
of grouped rows that step is cheap. The engine trims before the engine-to-client leg, so a
top-N sends fewer rows over the wire. Only a filter or an anchor reduces warehouse work. A
`LIMIT` on an aggregation does not.

This is observed behavior, not a documented guarantee. The official docs say ordering and
limits "only work if they do not require post-processing in Cypher." If a sorted or limited
query fails, drop the `ORDER BY` and `LIMIT` and sort in the application.

A leading `WHERE` on a node or edge property such as `WHERE a.balance > 0` or
`WHERE t.amount >= 9000` stays on the server.

### Pattern 4: split cross products into independent halves

An `OPTIONAL MATCH` that branches two ways multiplies rows together. It then needs
`DISTINCT` to undo the mess. The Virtual Graph rejects `OPTIONAL MATCH` with `42NG1:
Unsupported syntax: OPTIONAL MATCH`, which matches the official docs. Split the query into
two single-`MATCH` aggregations and join them client-side instead. The split form also
removes the `DISTINCT`.

- **P2P-heavy, merchant-light.** The original used an undirected transfer pattern plus a
  leading `OPTIONAL MATCH` to merchants, with `count(DISTINCT tr)` and `count(DISTINCT tw)`
  over the cross product. The `DISTINCT` existed only to undo that cross product. Each
  incident edge is already distinct. So `count(DISTINCT tr)` is just `count(tr)`, the plain
  transfer degree. Likewise, `count(DISTINCT tw)` is just `count(tw)`, the merchant count.

  ```cypher
  MATCH (a:Account)-[tr:TRANSFERRED_TO]-(:Account)
  WITH a.account_id AS account_id, count(tr) AS transfer_count
  RETURN account_id, transfer_count
  ```

  ```cypher
  MATCH (a:Account)-[tw:TRANSACTED_WITH]->(:Merchant)
  WITH a.account_id AS acct, count(tw) AS merchant_count
  RETURN acct AS account_id, merchant_count
  ```

  Each half pushes down. The transfer half keeps its threshold in Cypher as
  `WHERE transfer_count >= 100` after the `WITH`. The courier query takes about 15s in the
  demo. Client-side, left-join the two halves on the account. Default `merchant_count` to 0
  for accounts with no merchant activity, then apply the `merchant_count < 20` check. That
  check is the only part that runs client-side. It has to follow the join, because
  accounts with zero merchant activity appear only after the default fills in. The
  transfer half also confirms the undirected `-[tr:TRANSFERRED_TO]-` pattern translates
  and pushes down.

### Pattern 5: anchor deep traversals

A `LIMIT` pushes into the SQL on a traversal, anchored or not, at every depth tested. An
unanchored single-, two-, and four-hop traversal with `LIMIT 25` each produced exactly 25
rows on the warehouse. A limit-bounded visualization query does not need an anchor to be
bounded. The `LIMIT` bounds the output, not the join work. The unanchored four-hop query
returned in 0.6 to 2.4s across runs on this data, but its join work still grows with each hop. An anchor on a
single node id such as `{account_id: 184}` becomes a selective SQL filter. That filter, not
the `LIMIT`, is what keeps a deep traversal cheap.

```cypher
MATCH (a:Account {account_id: $account_id})-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
LIMIT 25
```

For a shallow visualization, a `LIMIT` alone is enough. For a deep one, anchor on a
specific account or merchant. Either way, keep the row count small, because every row
travels back over the wire. The visualization queries in
[`finding-fraud.md`](docs/finding-fraud.md) and [`basic-graph-examples.md`](basic-graph-examples.md)
all follow this rule.

### Pattern 6: keep result sets small

Cost center 2 stands alone: every row travels back over the wire. The all-time fan-out
pair query returns 222,966 rows even though it pushes down fully. Narrowing it to a recent
7-day window cuts it to 22,096 rows. Add a time window or a tighter filter to shrink the
row count. A recent window is also often the cleaner definition of the signal. A burst of
many recipients in one week is a better smurfing signal than an all-time total.

Multi-hop joins are expensive regardless of grouping. Following two steps in a row, where
A sends to B and B sends to C, does a lot of matching work no matter how you group it. The
rapid-turnover query takes about 210s. The pass-through mule query takes about 4 to 6s
with `LIMIT 50`. Always bound or filter a two-hop pattern.

A two-hop pattern also matches once per combination of edges. The round-trip query binds
one row for every pair of one transfer each way, so `count(*)` and `sum()` over it count
each transfer several times. Count each direction with `count(DISTINCT f)` and divide
each direction's sum by the other direction's count, as query 3 does. The pass-through
mule query groups by mule and incoming transfer in a first `WITH`, so each incoming
transfer's dollars count once.

### When plain Cypher is not enough: GDS

The patterns above keep rules-based fraud signals such as degree, fan-in/out, reciprocity,
cycles, and co-occurrence in pushed-down Cypher. A global, transitive score such as PageRank
or community detection cannot be expressed that way. It needs a GDS Session, a separate
ephemeral compute path that projects the data into an in-memory graph. Its cost is
dominated by that projection step, which is insensitive to warehouse size. The default
7-day window holds 23,198 edges and projects in 35 to 38s. Almost all of that is session
provisioning, which took 31 to 44s across runs. PageRank then streams in 2 to 3s. A
projection of all 300,000 transfers took 41.0s. See [`gds-guide.md`](gds-guide.md) for the working projection
pattern and the plain-Cypher-versus-GDS trade-off.

## Adaptation recipes

How to take a standard Cypher query and make it run on the Virtual Graph. Most of these
follow from the patterns above.

- **Keep the threshold filter in Cypher.** Put a HAVING-style `WHERE` on the aggregate
  alias after the aggregating `WITH`, then add `ORDER BY` and `LIMIT`. See
  [Pattern 3](#pattern-3-filter-aggregates-order-and-limit-in-cypher).
- **Replace relative time windows with a parameter.** `datetime() - duration({days: 7})`
  inside `WHERE` is unsupported. Compute the cutoff in the application and pass it as
  `$since`, then use `prop >= $since`. Anchor the window to the dataset's maximum
  timestamp, not `now()`: the synthetic transfer data ends 2024-03-30, so a window
  relative to the present returns nothing. `max(transfer_timestamp)` is fast to query and
  makes a good anchor. The demo cutoff `2024-03-23T23:58:00Z` is that maximum minus 7
  days. The `opened_date` series ends 2022-12-06, so the new-account window uses
  `2022-11-06`, that maximum minus 30 days.
- **Drop the upper bound on a multi-hop time window.** Keep the plain ordering
  `out.transfer_timestamp >= in.transfer_timestamp`. Timestamp-plus-duration comparisons
  across relationships are unsupported in `WHERE`. If you need turnaround time, compute it
  in `RETURN` or an aggregating `WITH`, not in the filter.
- **Avoid `.epochMillis` on a property.** Reading `.epochMillis` off a timestamp property
  raises the `01N52` unknown-property warning. Compute the gap with
  `duration.inSeconds(start, end)` instead, and convert the Duration to hours or seconds
  client-side. Query 10 uses this form. It runs in about 210s, against about 140s for the
  `.epochMillis` form, and its values are exact against Databricks SQL.
- **Project timestamps into GDS with `toInteger()` in a `WITH`.** A GDS projection needs a
  numeric timestamp. Bind it first with
  `WITH src, dst, t, toInteger(t.transfer_timestamp) * 1000 AS transfer_timestamp_ms`,
  then pass `transfer_timestamp_ms` in `relationshipProperties`. The Virtual Graph pushes
  `toInteger()` on a timestamp down to SQL as epoch seconds and raises no warning. Every
  transfer timestamp is a whole second, so the values equal `epochMillis` exactly. The
  same call inside the projection's config map fails with `22N38`. Stock Cypher rejects
  `toInteger()` on a timestamp, so this form works only on the Virtual Graph. See
  [`gds-guide.md`](gds-guide.md).
- **Move node-property predicates to a leading `WHERE`.** `WHERE a.balance > 0` belongs
  before the aggregating `WITH`, where it stays on the server.
- **For cycles**, enumerate fixed-length patterns and `UNION` them, or run against a
  loaded Aura graph. The quantified path `{2,4}` does not translate and returns `42NG1`.
  `UNION ALL` is confirmed to run on the Virtual Graph, with each branch as its own pushed
  warehouse statement. See the two-label count in [Cypher coverage](#cypher-coverage).

## Cypher coverage

### Supported

| Construct | Detail |
|---|---|
| Aggregation in `WITH` and `RETURN` | `count`, `count(DISTINCT ...)`, `sum`, `avg`, `min`, `max`, `stDev`, `round`, `collect(DISTINCT ...)`, `size()`. The official docs say only `count`, `sum`, `min`, `max`, `avg`, and `collect` push down to SQL. Other aggregations are post-processed. `count(DISTINCT ...)` runs in 2.0s on a 7-day window. See [Pattern 2](#pattern-2-count-distinct-values-on-the-server). |
| HAVING-style filtering | A `WHERE` after an aggregating `WITH` can filter on an aggregate alias. It runs with or without the `CYPHER 25` prefix. See [Pattern 3](#pattern-3-filter-aggregates-order-and-limit-in-cypher). |
| Null and existence checks | `IS NULL` and `IS NOT NULL` both run. The tested column has no nulls, so full null semantics were not exercised. |
| `range()` | `RETURN range(1, 3)` runs. |
| Property plus aggregate on the same node | `MATCH (a:Account) RETURN a.region, count(a) AS c ORDER BY c DESC LIMIT 10` runs in 0.5s. |
| `UNION` / `UNION ALL` | Each branch runs as its own pushed warehouse statement and the engine concatenates the results. Verified with a two-label count and used by the cycles recipe. |
| Plain comparisons in `WHERE` | Numeric comparisons, `abs()`, arithmetic on amounts, `timestamp >= timestamp`, `timestamp >= $param`, `timestamp >= datetime("2020-01-01T00:00:00Z")`. |
| Temporal projection in `RETURN` | `date(timestamp)`, `duration.inSeconds(...)` and `duration.between(...)`. The `.epochMillis` property also runs, but it raises the `01N52` warning. See [Adaptation recipes](#adaptation-recipes). |

### Not supported (returns `42NG0` or `42NG1: Unsupported syntax`)

Most rejections return `42NG1` with a specific reason.

| Construct | Detail |
|---|---|
| Writes | `SET`, `CREATE`, `MERGE` all fail. The Virtual Graph is read-only. |
| `OPTIONAL MATCH` | Fails fast with `42NG1: Unsupported syntax: OPTIONAL MATCH`. This matches the [official Cypher coverage](https://neo4j.com/docs/virtual-graph/aura/cypher-coverage/). Use the split form in [Pattern 4](#pattern-4-split-cross-products-into-independent-halves). |
| Temporal arithmetic in a filtering `WHERE` | `datetime() - duration({...})`, `date() - duration({...})`, timestamp-plus-duration compared across relationships, `duration.inSeconds(...)` and `duration.between(...)`, and `.epochMillis` subtraction. The same functions work in `RETURN`. |
| Variable-length and quantified path patterns | For example `(a)-[:TRANSFERRED_TO]->{2,4}(a)`, which returns `42NG1: Equijoin on the outer nodes of a quantified path pattern is not supported`. `*1..2` returns `42NG1: Unsupported var-length relationship`. |
| Counting two labels in one chained statement | `MATCH (a:Account) WITH count(a) ... MATCH (m:Merchant) ...` fails at parse time with `42NG1: Aggregating WITH clause is not supported`. The blocker is the `WITH`, not the `count`. `WITH` is a projection boundary: it ends one query part and begins another, carrying forward only the variables it names. The Virtual Graph cannot translate a second `MATCH` opened after that aggregating `WITH` horizon. The chained form fails regardless of what is being aggregated, and `count()` itself is supported on each side. Workaround: combine two single-label counts with `UNION ALL` in one statement. Each branch has its own independent scope and runs as its own pushed warehouse query. |
| `CYPHER 25` version prefix | Runs, but does not enable any of the above. |
| Subqueries and `CALL` | `EXISTS { MATCH ... }` and other subquery expressions are unsupported. `CALL () { ... }` returns `42NG1: The query must start with at least one MATCH clause`. The one exception is the GDS path in [`gds-guide.md`](gds-guide.md). |
| `UNWIND` followed by `MATCH` | `UNWIND` works only when no `MATCH` clause follows it. |
| APOC, vector search, fulltext search | All unsupported. |

These rows come from the official [Cypher coverage](https://neo4j.com/docs/virtual-graph/aura/cypher-coverage/) page. Neo4j says coverage is subject to change, so recheck that page when a query fails.

## Performance and the connection pool

Three things set how fast a query comes back: its shape, the warehouse size, and the
connection pool. The patterns above cover shape, the largest lever. This section covers
the other two. Every query becomes SQL on the backing Databricks SQL warehouse and runs
through a small JDBC connection pool to it, so both the warehouse and the pool shape what
you observe.

The timings below were measured on 2026-09-23.

| Query shape | Time |
|---|---|
| `RETURN 1` | about 0.2s |
| `count` of 25,000 nodes | 1.4s |
| `max(timestamp)` | 1.3s |
| Single-hop aggregation scanning the full relationship table | 3.6s |
| Aggregation grouped by a whole node | 1.1 to 1.3s |
| `count(DISTINCT ...)` with a scalar key | 1.5s on a 1-day window, 2.0s on 7 days |
| Shared-merchant burst with `collect(DISTINCT ...)` | about 5s |
| Two-hop pattern joins | pass-through mule about 4 to 6s, round trips about 3 to 4s, rapid-turnover about 210s |

How the pool behaves:

- **Pool size.** The Virtual Graph holds a small JDBC connection pool to Databricks, with
  an observed maximum of 10 connections.
- **The transaction timeout is honored.** A Bolt `begin_transaction(timeout=1)` on a
  full-scan query raised `TransactionTimedOutClientConfiguration` after 3.3s. A
  rapid-turnover query stopped by the timeout left no warehouse statement running. It did
  not hold a pool connection.
- **Saturation.** Once enough long-running queries hold connections, the pool is full.
  New queries then return `HikariPool-1 - Connection is not available, request timed out
  after 30000ms (total=10, active=10, idle=0)`. The pool recovers when those queries finish
  on Databricks, or after the instance is restarted.

Recommendations:

- **Run one query at a time.** Letting each finish before starting the next keeps
  connections free.
- **Keep result sets small.** Prefer the lighter aggregations and add time windows. See
  [Pattern 6](#pattern-6-keep-result-sets-small).
- **Do not count on warehouse size for these queries.** A bigger warehouse only helps a query
  that is genuinely scan- or spill-bound, and this workload is neither. A 2X-Small-versus-Small
  test found no difference across every window of the fan-out query. Databricks already
  finishes each one in about 0.2s, and the wait is data movement, not compute. A separate
  stress test confirmed the warehouse aggregated a 100M-row, ~1 GB table in a few seconds
  with zero spill. Reach for a bigger warehouse only when a query shows real scan time or
  spill on Databricks, which on this data almost never happens. Otherwise the lever is query
  shape and result size, not the machine.

## Anti-patterns: the slow and unsupported forms

These forms either fail on the Virtual Graph or run much slower than their rewrite. Each
one names the form to use instead.

- **Hub statistics with a leading `OPTIONAL MATCH` and three `count(DISTINCT ...)`
  aggregates.** This cartesian-fan-out shape fails, because `OPTIONAL MATCH` is rejected.
  Compute the in-degree and out-degree as separate single-`MATCH` aggregations with
  `count(DISTINCT ...)`, as in
  [Pattern 2](#pattern-2-count-distinct-values-on-the-server).
- **P2P-heavy, merchant-light with an `OPTIONAL MATCH` cross product.** This form fails with
  `42NG1`. Split it into two halves per
  [Pattern 4](#pattern-4-split-cross-products-into-independent-halves).
- **A group key aliased to a backing column name.** `mule.account_id AS account_id` fails
  with `AMBIGUOUS_REFERENCE`. Use a distinct alias such as `mule_id`, per the aliasing rule
  in [Pattern 2](#pattern-2-count-distinct-values-on-the-server).
- **Unbounded two-hop joins.** Rapid-turnover completes in about 210s. Bound the window
  and move turnaround time into `RETURN`. See
  [Pattern 6](#pattern-6-keep-result-sets-small).
- **Layering cycles with a `{2,4}` path.** The quantified path returns `42NG1: Equijoin on
  the outer nodes of a quantified path pattern is not supported`. The path itself is the
  coverage gap, so reshaping the `WITH` does not help. Enumerate fixed-length patterns and
  `UNION` them, or run on a loaded Aura graph.

## Appendix: loaded-graph reference forms

These are the standard, loaded-graph forms of each fraud signal. They do not run verbatim
on the Virtual Graph. They show the gap between the textbook query and the adapted form.
The **Virtual Graph: ✓ / ✗** marker shows only whether the signal is achievable at all. It
does not mean the Cypher runs as written. Every ✓ needs the adaptations above, most often
replacing relative time windows with `$since`. The post-aggregation `WHERE` runs as
written. The one ✗, cycles, uses a quantified path the Virtual Graph cannot translate.

These forms use the same `:Account` / `:Merchant` labels as the Virtual Graph model.

### 1. Fan-in (mule collection accounts): ✓

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime() - duration({days: 7})
WITH dst,
     count(DISTINCT src) AS senders,
     count(t)            AS transfers,
     sum(t.amount)       AS inflow
WHERE senders >= 5
RETURN dst.account_id, senders, transfers, round(inflow, 2) AS inflow
ORDER BY senders DESC, inflow DESC
LIMIT 50
```

### 2. Fan-out (distribution / smurfing): ✓

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WITH src,
     count(DISTINCT dst) AS recipients,
     sum(t.amount)       AS outflow
WHERE recipients >= 5
RETURN src.account_id, recipients, round(outflow, 2) AS outflow
ORDER BY recipients DESC
LIMIT 50
```

### 3. Pass-through mule (local betweenness proxy): ✓

```cypher
MATCH (a:Account)-[in:TRANSFERRED_TO]->(mule:Account)-[out:TRANSFERRED_TO]->(b:Account)
WHERE out.transfer_timestamp >= in.transfer_timestamp
  AND out.transfer_timestamp <= in.transfer_timestamp + duration({hours: 48})
  AND abs(out.amount - in.amount) <= 0.05 * in.amount
  AND a <> b
RETURN mule.account_id,
       count(*)                 AS passthroughs,
       round(sum(in.amount), 2) AS volume
ORDER BY passthroughs DESC
LIMIT 50
```

This textbook form sums `in.amount` once per matching outgoing transfer, so `volume`
counts an incoming transfer several times. The Virtual Graph form in the fraud demo groups
by mule and incoming transfer first, so each incoming transfer counts once.

### 4. Reciprocal / round-trip transfers: ✓

```cypher
MATCH (a:Account)-[f:TRANSFERRED_TO]->(b:Account)-[g:TRANSFERRED_TO]->(a)
WHERE a.account_id < b.account_id
RETURN a.account_id, b.account_id,
       round(sum(f.amount + g.amount), 2) AS round_trip_volume,
       count(*)                            AS leg_count
ORDER BY round_trip_volume DESC
LIMIT 50
```

This textbook form binds one row per combination of one transfer each way. `leg_count`
is therefore the product of the two direction counts, and `round_trip_volume` counts each
transfer several times. Query 3 in the fraud demo counts each direction with
`count(DISTINCT ...)` and divides each direction's sum by the other direction's count.

### 5. Layering cycles: ✗ (loaded graph only)

The variable-length path `{2,4}` is a coverage gap. Run on the loaded Aura graph, or
enumerate fixed lengths as separate single-`MATCH` queries and `UNION` them, which is a
confirmed-working construct on the Virtual Graph.

```cypher
MATCH path = (a:Account)-[:TRANSFERRED_TO]->{2,4}(a)
RETURN a.account_id AS ring_origin,
       length(path) AS hops,
       [n IN nodes(path) | n.account_id] AS cycle
LIMIT 50
```

### 6. Shared-merchant burst (coordinated ring): ✓

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
WITH m, date(t.txn_timestamp) AS day,
     collect(DISTINCT a.account_id) AS accounts,
     count(t)                       AS txns
WHERE size(accounts) >= 4
  AND txns <= 200
RETURN m.merchant_id, m.merchant_name, day,
       size(accounts) AS account_count, txns, accounts
ORDER BY account_count DESC
LIMIT 50
```

### 7. Structuring (just-under-threshold transfers): ✓

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE t.amount >= 9000 AND t.amount < 10000
WITH src, count(t) AS near_threshold, round(sum(t.amount), 2) AS total
WHERE near_threshold >= 3
RETURN src.account_id, near_threshold, total
ORDER BY near_threshold DESC
LIMIT 50
```

### 8. New account, high velocity: ✓

```cypher
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE a.opened_date >= date() - duration({days: 30})
WITH a, count(t) AS transfers, round(sum(t.amount), 2) AS outflow
WHERE transfers >= 10
RETURN a.account_id, a.opened_date, a.holder_age, transfers, outflow
ORDER BY outflow DESC
LIMIT 50
```

### 9. Hub network statistics: ✓ (reshaped)

```cypher
MATCH (a:Account)<-[r_in:TRANSFERRED_TO]-(src:Account)
OPTIONAL MATCH (a)-[r_out:TRANSFERRED_TO]->(dst:Account)
WITH a,
     count(DISTINCT src)  AS incoming_conns,
     count(DISTINCT dst)  AS outgoing_conns,
     count(DISTINCT r_in) AS incoming_txns
WHERE incoming_conns >= 100
RETURN a.account_id,
       incoming_conns + outgoing_conns AS total_connections,
       incoming_conns, outgoing_conns, incoming_txns
ORDER BY incoming_conns DESC
LIMIT 15
```

### 10. Rapid-turnover summary per account: ✓

```cypher
MATCH (src:Account)-[in:TRANSFERRED_TO]->(mule:Account)-[out:TRANSFERRED_TO]->(dst:Account)
WHERE out.transfer_timestamp >= in.transfer_timestamp
  AND out.transfer_timestamp <= in.transfer_timestamp + duration({hours: 24})
  AND src <> dst
WITH mule,
     count(*) AS rapid_pairs,
     avg(duration.inSeconds(in.transfer_timestamp, out.transfer_timestamp).seconds) / 3600.0 AS avg_hours
WHERE rapid_pairs >= 50
RETURN mule.account_id, rapid_pairs, round(avg_hours, 1) AS avg_turnaround_hours
ORDER BY rapid_pairs DESC
LIMIT 15
```

### 11. Velocity ratio (volume vs. balance): ✓

`balance` is a single current snapshot. In this dataset it does not move with the
transfers, so a small balance alone pushes an account up the ranking. The top 50 accounts
hold a median balance of $3,776, against $247,686 across all accounts. Treat a high ratio
as one weak signal. Account tenure or
inflow-vs-outflow symmetry is a cleaner denominator.

```cypher
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WITH a, sum(t.amount) AS outflow
WHERE a.balance > 0 AND outflow > 0
RETURN a.account_id,
       round(a.balance, 2)            AS balance,
       round(outflow, 2)              AS outflow_volume,
       round(outflow / a.balance, 1)  AS velocity_ratio
ORDER BY velocity_ratio DESC
LIMIT 25
```

### 12. P2P-heavy, merchant-light disconnect: ✓ (reshaped)

```cypher
MATCH (a:Account)-[tr:TRANSFERRED_TO]-(:Account)
OPTIONAL MATCH (a)-[tw:TRANSACTED_WITH]->(:Merchant)
WITH a,
     count(DISTINCT tr) AS transfer_count,
     count(DISTINCT tw) AS merchant_count
WHERE transfer_count >= 100 AND merchant_count < 20
RETURN a.account_id, transfer_count, merchant_count
ORDER BY transfer_count DESC
LIMIT 25
```
