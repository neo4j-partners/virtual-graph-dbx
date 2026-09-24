# Virtual Graph Best Practices

How to write Cypher that runs well on the Finance Genie Virtual Graph. Aura
compiles Cypher into SQL and pushes most of the work down to the backing Databricks SQL
warehouse, so the rules here are about helping that translation push work down to
Databricks instead of dragging rows back to the graph engine.

The working fraud queries this guide draws on are the demo set in
[`finding-fraud.md`](docs/finding-fraud.md). The warm-up and visualization queries are in
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
  account-id anchor push down to Databricks. A `GROUP BY` pushes down when every group key
  is a scalar property and the threshold sits in a second `WITH`. Thresholds run in the
  graph engine over the groups the warehouse returns. So do an `ORDER BY` and `LIMIT` that
  follow an aggregating `WITH`. On a traversal, the sort and the limit push into the SQL. Almost every
  adaptation in this guide follows from that split.
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

A few terms used throughout:

- **Scalar:** A scalar is a single plain value, such as an `account_id`. A whole node
  object is the opposite.
- **Pushdown:** Pushdown means the aggregation runs on Databricks as a SQL `GROUP BY`. The
  warehouse then returns one row per group.
- **Raw rows:** When the `GROUP BY` does not push down, the warehouse returns one row per
  matched pattern. The graph engine then does the counting and summing itself.
- **Cardinality:** Cardinality is how many rows a query touches or returns. High
  cardinality means a lot of rows.

**Cost center 1: rows shipped into the graph engine.** The largest cost is how many rows
the warehouse returns to the graph engine, plus the engine's work on them. Warehouse
execution is a small part of the wall time. Uncached pushed aggregations took 150 to 600ms
of warehouse execution on this data. An earlier form of query 10 got 11,151,853 raw rows
back from the warehouse result cache in 2ms, then spent 208s in the engine. Its pushed
form returns 24,319 groups and finishes in about 1s.

**Cost center 2: rows returned to the client.** Every result row also travels back over
the wire to the application. This bites even when the aggregation pushes down. See
[Pattern 6](#pattern-6-keep-result-sets-small).

**The machine underneath.** Once the query shape is right, the absolute wall-clock time
also depends on the warehouse size and the connection pool, covered under
[Performance and the connection pool](#performance-and-the-connection-pool).

**How to check a query.** Look up its statement in the warehouse query history or in
`system.query.history`. A pushed aggregation has a `GROUP BY` in its SQL text, and its row
count equals the number of groups. A query that did not push has no `GROUP BY`, and its
row count equals the number of matches. The `03N97` post-processing notification does not
tell you either way. It fires on every aggregation with `ORDER BY` or `LIMIT`, pushed or
not.

The next sections are the patterns. Each one is stated once, with the worked example
that measured it. The rules come from the SQL text of each statement in
`system.query.history`, captured on 2026-09-23.

### Pattern 1: group by scalar keys

Every group key must be a scalar property such as `a.account_id`. A node such as `a`, a
relationship such as `t`, or a function such as `date(t.txn_timestamp)` used as a group
key keeps the `GROUP BY` out of the SQL. The warehouse then returns every matching row,
and the engine groups them.

The cost of that mistake grows with the number of matches. The structuring query matches
only 200 transfers. Its node-grouped form, `WITH src, count(t) ...`, returns the same rows
as the scalar form in about 0.5s, against about 0.35s. Query 10 matches 11,151,853
transfer pairs. Grouped by the `mule` node, it took 208s. Grouped by `mule.account_id`, it
pushes 24,319 groups and takes about 1s.

- **Structuring (just-under-threshold transfers).** Grouping by scalar `src.account_id`
  pushes the `GROUP BY` down to the warehouse.

  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO]->(:Account)
  WHERE t.amount >= 9000 AND t.amount < 10000
  WITH src.account_id AS account_id, count(t) AS near_threshold, round(sum(t.amount), 2) AS total
  RETURN account_id, near_threshold, total
  ORDER BY near_threshold DESC, account_id ASC
  ```

- **New account, high velocity.** The scalar form groups by `a.account_id` and carries
  `a.opened_date` and `a.holder_age` as extra grouping keys. Those values are constant per
  account, so they do not split any group.

  ```cypher
  MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
  WHERE a.opened_date >= date("2022-11-06")
  WITH a.account_id AS account_id, a.opened_date AS opened_date,
       a.holder_age AS holder_age, count(t) AS transfers, sum(t.amount) AS outflow
  RETURN account_id, opened_date, holder_age, transfers, outflow
  ```

Carry any constant node property you need, such as the balance or the opened date, as an
additional scalar grouping key. Do not group by the node just to keep that property.

To group per relationship, group on its unique id. Query 8 groups on `t_in.link_id` and
carries `t_in.amount` as an extra key, instead of grouping on `t_in`. That change took it
from 210,289 raw rows to 98,474 groups, and from about 3.6s to about 1s.

Query 9 is the one exception in the demo. It groups on `date(t.txn_timestamp)`, which does
not push down, so it ships all 250,000 purchases and runs in 5 to 9s. An epoch-day key,
`toInteger(t.txn_timestamp) / 86400`, does push down. It returns 208,127 groups, but it
takes about 35s. The engine ingests the arrays from `collect(DISTINCT ...)` slowly, so a
pushed `collect` over many groups can cost more than the raw rows. Time both forms when a
query collects.

### Pattern 2: count distinct values on the server

`count(DISTINCT x.prop)` over a scalar property pushes down as a SQL `count(DISTINCT ...)`.
Queries 5 and 6 use it, and each runs in under 1s. Write the distinct count in Cypher and
let the server return one row per account.

`count(DISTINCT r)` over a relationship variable runs, but it keeps the `GROUP BY` out of
the SQL. Count the relationship's unique id instead. Query 3 counts
`count(DISTINCT f.link_id)` rather than `count(DISTINCT f)`. `link_id` is unique, so the
counts are equal. The change took query 3 from 56,254 raw rows to 21,052 groups, and from
about 2.2s to about 0.6s warm.

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

### Pattern 3: put thresholds in a second `WITH`

Write the threshold, the sort, and the top-N in Cypher. Put the threshold in a second
`WITH` that repeats the columns, not on the aggregating `WITH` itself. A `WHERE` attached
to the aggregating `WITH` is the Cypher form of SQL `HAVING`. It runs, but it keeps the
`GROUP BY` out of the SQL. That held for every aggregate tested: `count(t)`, `count(*)`,
`sum`, `max` and `count(DISTINCT ...)`. The second `WITH` restores the push and returns
the same rows.

- **Fan-in by transfer count.** This query uses a scalar group key, `count(t)`, and
  `sum(t.amount)` over a 7-day window. With the threshold on the aggregating `WITH`, the
  warehouse returned 23,198 raw rows, and the query took 0.9s warm and 2.8s cold. With the
  threshold in a second `WITH`, it returned 9,847 groups and took 0.4s warm and 0.7s cold.

  ```cypher
  MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
  WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
  WITH dst.account_id AS recipient, count(t) AS transfers, sum(t.amount) AS inflow
  WITH recipient, transfers, inflow
  WHERE transfers >= 5
  RETURN recipient AS account_id, transfers, inflow
  ORDER BY transfers DESC, account_id
  LIMIT 50
  ```

  The two forms return the same accounts in the same order. The unrounded `inflow` sums
  differ in the twelfth decimal place, because the summation order changes.

- **Courier transfer degree.** Query 7's transfer half moved its `transfer_count >= 100`
  threshold into a second `WITH`. It went from 600,000 raw rows at about 11s to 25,000
  groups at under 1s, with the same 1,200 rows.

A threshold on the aggregating `WITH` also pushes when that `WITH` holds an expression over
an aggregate, such as `round(sum(t.amount), 2)`. Do not rely on it. Dropping that column
silently stops the push. Every demo query uses the second `WITH`.

The threshold itself never reaches the SQL. The warehouse returns every group, and the
graph engine applies the `WHERE`. `ORDER BY` and `LIMIT` after an aggregating `WITH` do
not reach the warehouse either. The captured SQL is byte-identical with or without them. The engine
sorts and trims the groups afterward as a post-processing step. Add a secondary sort key
such as `account_id` so rows that tie at the `LIMIT` cutoff come back in a fixed order.
Every demo fraud query does this. At tens of thousands of grouped rows that step is cheap.
The engine trims before the engine-to-client leg, so a top-N sends fewer rows over the
wire. Only a filter or an anchor reduces warehouse work. A `LIMIT` on an aggregation does
not.

An aggregation in the final `RETURN` behaves differently. Basic query 6 returns
`count(DISTINCT a)` per merchant with `ORDER BY` and `LIMIT 10`. Its warehouse statement
produced 10 rows, not the 7,500 merchant groups. The same query with the count in a `WITH`
produced all 7,500 groups. The warehouse still reads every transaction in both forms.

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
  WITH account_id, transfer_count
  WHERE transfer_count >= 100
  RETURN account_id, transfer_count
  ```

  ```cypher
  MATCH (a:Account)-[tw:TRANSACTED_WITH]->(:Merchant)
  WITH a.account_id AS acct, count(tw) AS merchant_count
  RETURN acct AS account_id, merchant_count
  ```

  Each half pushes down. The transfer half keeps its threshold in a second `WITH`, per
  [Pattern 3](#pattern-3-put-thresholds-in-a-second-with). The courier query takes about
  4s in the demo. Most of that is reading the 24,999 rows of the merchant half.
  Client-side, left-join the two halves on the account. Default `merchant_count` to 0 for
  accounts with no merchant activity, then apply the `merchant_count < 20` check. That
  check is the only part that runs client-side. It has to follow the join, because
  accounts with zero merchant activity appear only after the default fills in. The
  transfer half also confirms the undirected `-[tr:TRANSFERRED_TO]-` pattern translates
  and pushes down.

### Pattern 5: anchor deep traversals

A `LIMIT` pushes into the SQL on a traversal, anchored or not, at every depth tested. An
unanchored single-, two-, and four-hop traversal with `LIMIT 25` each produced exactly 25
rows on the warehouse. A limit-bounded visualization query does not need an anchor to be
bounded. The `LIMIT` bounds the output, not the join work. The unanchored four-hop query
returned in 0.6 to 2.4s across runs on this data, but its join work still grows with
each hop. An anchor on a single node id such as `{account_id: 184}` becomes a selective SQL filter. That filter, not
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

An `ORDER BY` changes the cost of an unanchored deep traversal. The sort pushes into the
SQL along with the `LIMIT`. To return the top 25 sorted rows, the warehouse must first
build the whole join. Without a sort, it can stop once it has 25 rows. These are
warehouse execution times for unanchored path queries with `LIMIT 25`, measured on
2026-09-23:

| Depth | No `ORDER BY` | With `ORDER BY` |
|---|---|---|
| Two hops | about 1s end to end, not timed separately | 0.8s |
| Three hops | 0.7s | 37 to 38s |
| Four hops | 1.0s | over 99s |

Depth drives the cost. Sorting on every node instead of only the first barely changes the
time. A sorted two-hop traversal is cheap. From three hops on, anchor the traversal before
you sort it, or leave the `LIMIT` unsorted and accept an arbitrary sample. Query 15 in
[`basic-graph-examples.md`](basic-graph-examples.md#15-any-25-four-hop-transfer-chains)
stays unsorted for this reason.

### Pattern 6: keep result sets small

Cost center 2 stands alone: every row travels back over the wire. The all-time fan-out
pair query returns 222,966 rows even though it pushes down fully. Narrowing it to a recent
7-day window cuts it to 22,096 rows. Add a time window or a tighter filter to shrink the
row count. A recent window is also often the cleaner definition of the signal. A burst of
many recipients in one week is a better smurfing signal than an all-time total.

A two-hop join can match far more rows than either relationship table holds. Query 10's
pattern matches 11,151,853 transfer pairs out of 300,000 transfers. When the aggregation
pushes down, the warehouse does that join and returns only the groups. Query 10 then runs
in about 1s, and the pass-through mule query in 1 to 2s. When it does not push down,
every match crosses into the graph engine. On a two-hop join, check the pushdown first.
Bound or anchor the pattern when the result itself is large.

A two-hop pattern also matches once per combination of edges. The round-trip query binds
one row for every pair of one transfer each way, so `count(*)` and `sum()` over it count
each transfer several times. Count each direction with `count(DISTINCT f.link_id)` and
divide each direction's sum by the other direction's count, as query 3 does. The
pass-through mule query groups by mule and incoming `link_id` in a first `WITH`, so each
incoming transfer's dollars count once.

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

- **Put the threshold in a second `WITH`.** Repeat the aggregate columns in a second
  `WITH` and attach the `WHERE` there, then add `ORDER BY` and `LIMIT`. A `WHERE` on the
  aggregating `WITH` itself keeps the `GROUP BY` out of the SQL. See
  [Pattern 3](#pattern-3-put-thresholds-in-a-second-with).
- **Replace relative time windows with a parameter.** `datetime() - duration({days: 7})`
  inside `WHERE` is unsupported. Compute the cutoff in the application and pass it as
  `$since`, then use `prop >= $since`. Anchor the window to the dataset's maximum
  timestamp, not `now()`: the synthetic transfer data ends 2024-03-30, so a window
  relative to the present returns nothing. `max(transfer_timestamp)` is fast to query and
  makes a good anchor. The demo cutoff `2024-03-23T23:58:00Z` is that maximum minus 7
  days. The `opened_date` series ends 2022-12-06, so the new-account window uses
  `2022-11-06`, that maximum minus 30 days.
- **Write a multi-hop time window with `toInteger()`.** Timestamp-plus-duration
  comparisons across relationships are unsupported in `WHERE`, and so are
  `duration.inSeconds()` and `duration.between()`. `toInteger()` on a timestamp returns
  epoch seconds and works in `WHERE`, so a 24-hour upper bound is
  `toInteger(t_out.transfer_timestamp) - toInteger(t_in.transfer_timestamp) < 86400`. Keep
  the plain ordering `t_out.transfer_timestamp >= t_in.transfer_timestamp` alongside it. On
  account 7855 this filter returns 148 of 6,458 ordered transfer pairs, which matches the gap
  computed client-side.
- **Compute time gaps with `toInteger()`.** `toInteger()` on a timestamp pushes down to
  SQL as a cast to epoch seconds. `avg(toInteger(t_out.transfer_timestamp) -
  toInteger(t_in.transfer_timestamp))` therefore pushes down with its `GROUP BY`.
  `avg(duration.inSeconds(...))` runs, but it keeps the `GROUP BY` out of the SQL. Reading
  `.epochMillis` off a timestamp property raises the `01N52` unknown-property warning.
  Query 10 uses the `toInteger()` form and converts the seconds to hours client-side. The
  cast drops sub-second parts. Every transfer timestamp in this data is a whole second, so
  the averages match `duration.inSeconds` to within 4e-8 seconds.
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
- **For cycles**, enumerate fixed-length patterns from an anchor and combine them with
  `UNION ALL`, or run against a loaded Aura graph. A quantified path that closes on its
  start node, such as `(a)-[:TRANSFERRED_TO]->{2,4}(a)`, returns `42NG1`. From account
  7855, the 2-hop and 3-hop branches return 144 and 13,474 matching paths in 2.4s. Parallel
  transfers between the same accounts each count as a separate path. Keep the anchor:
  an unanchored traversal of three or more hops forces the warehouse to build the full
  join. See [Pattern 5](#pattern-5-anchor-deep-traversals) and
  [Cypher coverage](#cypher-coverage).

## Cypher coverage

The official [Cypher coverage](https://neo4j.com/docs/virtual-graph/aura/cypher-coverage/)
page lists the supported query shape. The rows below come from live tests against this
Virtual Graph, which reports version `1.0-alpha-01` from `CALL dbms.components()`. Several
results differ from the official page, and the rows note where. Neo4j says coverage is
subject to change, so recheck that page and retest when a query fails.

### Supported

| Construct | Detail |
|---|---|
| Patterns | Relationship chains of any length and direction, undirected patterns such as `(a)-[t:TRANSFERRED_TO]-(b)`, and label expressions such as `(n:Account\|Merchant)` all run. Path returns such as `MATCH p=... RETURN p` run. |
| Aggregation in `WITH` and `RETURN` | `count`, `count(DISTINCT ...)`, `sum`, `avg`, `min`, `max`, `stDev`, `round`, `collect(DISTINCT ...)`, `size()`. The official docs say only `count`, `sum`, `min`, `max`, `avg`, and `collect` push down to SQL. Other aggregations are post-processed. `ORDER BY` and `LIMIT` inside an aggregating `WITH` also run. `count(DISTINCT r)` over a relationship variable runs but does not push down. See [Pattern 2](#pattern-2-count-distinct-values-on-the-server). |
| HAVING-style filtering | A `WHERE` after an aggregating `WITH` can filter on an aggregate alias. It runs with or without the `CYPHER 25` prefix. On the aggregating `WITH` itself, it keeps the `GROUP BY` out of the SQL. In a second `WITH`, the `GROUP BY` pushes down. See [Pattern 3](#pattern-3-put-thresholds-in-a-second-with). |
| `ORDER BY`, `SKIP`, `LIMIT` | All three run in `RETURN`. |
| Null and existence checks | `IS NULL` and `IS NOT NULL` both run. The official page says such checks always fail, but they run here. The tested column has no nulls, so full null semantics were not exercised. |
| `range()` | `RETURN range(1, 3)` runs on its own. The official page lists `range()` as unsupported. With a `MATCH` in the same query it fails with `42NG1: Unsupported parameter type List`. |
| Property plus aggregate on the same node | `MATCH (a:Account) RETURN a.region, count(a) AS c ORDER BY c DESC LIMIT 10` runs and returns the six regions. The official page says this form fails. |
| `UNION` / `UNION ALL` | Each branch runs as its own pushed warehouse statement and the engine concatenates the results. Verified with a two-label count and used by the cycles recipe. |
| Plain comparisons in `WHERE` | Numeric comparisons, `abs()`, arithmetic on amounts, `timestamp >= timestamp`, `timestamp >= $param`, `timestamp >= datetime("2020-01-01T00:00:00Z")`. |
| `toInteger()` on a timestamp | It returns epoch seconds, in `RETURN`, in a `WITH`, and in `WHERE`. `toInteger(o.transfer_timestamp) - toInteger(i.transfer_timestamp) < 86400` filters to a 24-hour gap. Stock Cypher rejects `toInteger()` on a temporal value, so this form works only on the Virtual Graph. |
| Temporal projection in `RETURN` | `date(timestamp)`, `duration.inSeconds(...)` and `duration.between(...)`. The `.epochMillis` property also runs, but it raises the `01N52` warning. `duration.inSeconds` inside an aggregate, or `date()` as a group key, keeps the `GROUP BY` out of the SQL. See [Adaptation recipes](#adaptation-recipes). |
| Open quantified path patterns | An anchored `(a:Account {account_id: 7855})-[:TRANSFERRED_TO]->{1,2}(b:Account)` runs. It must be the only `MATCH` in the query, and no hop may follow the quantified part. The warehouse runs it as a recursive query, so deeper bounds grow fast: `{1,3}` from the same anchor fails after 17s with the Databricks `RECURSION_ROW_LIMIT_EXCEEDED` error at 1,000,000 rows. |
| `MATCH` followed by `UNWIND` | `MATCH (a:Account {account_id: 7855}) UNWIND [1, 2] AS x RETURN a.account_id, x` runs. A bare `UNWIND [1, 2, 3] AS x RETURN x` also runs. |
| Correlated `CALL` subquery | `MATCH (a:Account {account_id: 7855}) CALL (a) { MATCH (a)-[t:TRANSFERRED_TO]->(b:Account) RETURN count(t) AS c } RETURN c` runs and returns 119. The official page lists `CALL` as unsupported. |
| APOC functions in `RETURN` | `RETURN apoc.version()` and `apoc.text.capitalize(a.region)` in `RETURN` run. The official page lists APOC as unsupported. APOC in `WHERE` fails with `42NG0`. |
| Schema and system procedures | `CALL db.labels()`, `CALL dbms.components()` and `SHOW PROCEDURES` run. |

### Not supported (returns `42NG0` or `42NG1: Unsupported syntax`)

Most rejections return `42NG1` with a specific reason.

| Construct | Detail |
|---|---|
| Writes | `SET` fails with `42NG0`. The official page lists every write clause, including `CREATE` and `MERGE`, as unsupported. The Virtual Graph is read-only. |
| `OPTIONAL MATCH` | Fails fast with `42NG1: Unsupported syntax: OPTIONAL MATCH`, with or without `CYPHER 25`. This matches the official page. Use the split form in [Pattern 4](#pattern-4-split-cross-products-into-independent-halves). |
| Temporal arithmetic in a filtering `WHERE` | `datetime() - duration({...})`, `date() - duration({...})`, timestamp-plus-duration compared across relationships, `duration.inSeconds(...)` and `duration.between(...)`, and `.epochMillis` subtraction all fail with `42NG0`. The same functions work in `RETURN`. `toInteger()` arithmetic works in `WHERE` instead. |
| Variable-length paths and closed quantified paths | `*1..2` returns `42NG1: Unsupported var-length relationship`. `(a)-[:TRANSFERRED_TO]->{2,4}(a)` returns `42NG1: Equijoin on the outer nodes of a quantified path pattern is not supported`. A hop after the quantified part returns `42NG1: Path concatenation is only supported in the context of a (node pattern, quantified path pattern, node pattern)`. A second `MATCH` returns `42NG1: Quantified path patterns are only supported in queries containing a single MATCH clause`. |
| `MATCH` after `WITH` | `MATCH (a:Account) WITH count(a) ... MATCH (m:Merchant) ...` fails at parse time with `42NG1: Aggregating WITH clause is not supported`. A plain `WITH a` followed by `MATCH` fails with `42NG0`. The official page states the rule: a `MATCH` after a `WITH` is unsupported. `WITH` is a projection boundary: it ends one query part and begins another, carrying forward only the variables it names. The Virtual Graph cannot translate a second `MATCH` opened after that horizon. `count()` itself is supported on each side. Workaround: combine two single-label counts with `UNION ALL` in one statement. Each branch has its own independent scope and runs as its own pushed warehouse query. |
| `CYPHER 25` version prefix | Runs, but does not enable any of the above. |
| Subquery expressions and leading `CALL` | `EXISTS { MATCH ... }` and `COUNT { ... }` fail with `42NG0`. `CALL () { ... }` at the start of a query returns `42NG1: The query must start with at least one MATCH clause`. The GDS path in [`gds-guide.md`](gds-guide.md) and the correlated `CALL` above run. |
| `UNWIND` followed by `MATCH` | `UNWIND [7855, 1032] AS id MATCH (a:Account {account_id: id}) ...` fails with `42NG0`. This matches the official page. |
| Vector search | `db.index.vector.queryNodes` fails with `The attempted kind of operations are not supported on virtual graph databases`. |
| Fulltext search | The official page lists it as unsupported. This graph has no fulltext index, so the test query failed with `There is no such fulltext schema index` and the procedure itself was not exercised. |

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
| Pushed aggregations, fraud queries 1 to 6 | 0.5 to 0.9s |
| Courier query, both halves | about 4s |
| Shared-merchant burst with `collect(DISTINCT ...)` | 5 to 9s |
| Two-hop pattern joins | round trips about 1s, pass-through mule 1 to 2s, rapid-turnover about 1s |

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
- **A node or relationship as a group key.** `WITH mule, count(*) ...` or
  `WITH mule_id, t_in, ...` keeps the `GROUP BY` out of the SQL, so every match crosses
  into the graph engine. Query 10 in that form took 208s. Group on scalar properties such
  as `mule.account_id` and `t_in.link_id`, per
  [Pattern 1](#pattern-1-group-by-scalar-keys).
- **A threshold on the aggregating `WITH`.** `WITH k, count(t) AS c WHERE c >= 100` keeps
  the `GROUP BY` out of the SQL. Query 7's transfer half took about 11s in that form. Move
  the `WHERE` into a second `WITH`, per
  [Pattern 3](#pattern-3-put-thresholds-in-a-second-with).
- **`count(DISTINCT r)` on a relationship, or `duration.inSeconds` inside an aggregate.**
  Both run, and both keep the `GROUP BY` out of the SQL. Count the relationship's
  `link_id`, per [Pattern 2](#pattern-2-count-distinct-values-on-the-server). Compute gaps
  with `toInteger()`, per [Adaptation recipes](#adaptation-recipes).
- **`ORDER BY` with `LIMIT` on an unanchored traversal of three or more hops.** The
  warehouse builds the full join before it can pick the top rows. A sorted three-hop query
  takes about 37s, and a sorted four-hop query ran past 99s. Anchor the traversal, or drop
  the sort. See [Pattern 5](#pattern-5-anchor-deep-traversals).
- **Layering cycles with a `{2,4}` path.** The quantified path returns `42NG1: Equijoin on
  the outer nodes of a quantified path pattern is not supported`. The path itself is the
  coverage gap, so reshaping the `WITH` does not help. Enumerate fixed-length patterns and
  `UNION` them, or run on a loaded Aura graph.

## Appendix: loaded-graph reference forms

These are the standard, loaded-graph forms of each fraud signal. They show the gap
between the textbook query and the adapted form. Forms 2, 4, 6, 7 and 11 run verbatim on
the Virtual Graph. They group by nodes and put thresholds on the aggregating `WITH`, so
their aggregation does not push down. The other forms need the adaptations above, most
often replacing relative time windows with `$since`. The **Virtual Graph: ✓ / ✗** marker
shows only whether the signal is achievable at all. It does not mean the Cypher runs as
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
MATCH (a:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)-[t_out:TRANSFERRED_TO]->(b:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND t_out.transfer_timestamp <= t_in.transfer_timestamp + duration({hours: 48})
  AND abs(t_out.amount - t_in.amount) <= 0.05 * t_in.amount
  AND a <> b
RETURN mule.account_id,
       count(*)                 AS passthroughs,
       round(sum(t_in.amount), 2) AS volume
ORDER BY passthroughs DESC
LIMIT 50
```

This textbook form sums `t_in.amount` once per matching outgoing transfer, so `volume`
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

The quantified path `{2,4}` that closes on its start node is a coverage gap. Run on the loaded Aura graph, or
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
MATCH (src:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)-[t_out:TRANSFERRED_TO]->(dst:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND t_out.transfer_timestamp <= t_in.transfer_timestamp + duration({hours: 24})
  AND src <> dst
WITH mule,
     count(*) AS rapid_pairs,
     avg(duration.inSeconds(t_in.transfer_timestamp, t_out.transfer_timestamp).seconds) / 3600.0 AS avg_hours
WHERE rapid_pairs >= 50
RETURN mule.account_id, rapid_pairs, round(avg_hours, 1) AS avg_turnaround_hours
ORDER BY rapid_pairs DESC
LIMIT 15
```

On the Virtual Graph, `avg(duration.inSeconds(...))` keeps the `GROUP BY` out of the SQL.
The demo's query 10 computes the gap with `toInteger()` arithmetic instead, which pushes
down.

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
