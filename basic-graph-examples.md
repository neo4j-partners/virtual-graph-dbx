# Basic Graph Examples (Virtual Graph)

Warm-up queries for the Finance Genie Virtual Graph: simple counts and small
relationship traversals that show the value of the graph without any fraud
logic. They are the queries to open a demo with, before the structural fraud
signals in [`finding-fraud.md`](docs/finding-fraud.md).

Every query here was verified live against the Aura Virtual Graph backed by the
Databricks Silver tables. The `basic` demo runs all 15 of them:

```bash
uv run vg-demo --demo basic
```

The dataset is 25,000 accounts, 7,500 merchants, 300,000 transfers, and 250,000
merchant transactions. Node labels are `:Account` and `:Merchant`, backed by the
`accounts` and `merchants` tables. If your model shows `:accounts` / `:merchants`, rename
the labels in the Aura model as described in section 5 of [`virtual-graph.md`](virtual-graph.md).

## How to read the two kinds

The queries split into two kinds.

- **Table** queries return scalar rows. They feed a table or bar-chart widget and
  print directly in the CLI demo.
- **Graph** queries return nodes and relationships, either as variables or as a path.
  The payoff is the picture in the Aura Workspace Query tab, which renders the returned
  nodes and edges as a graph. The CLI demo only reports the row count for these, so run
  them in the Workspace to see the visualization.

Queries 7 to 11 anchor on a specific `account_id` or `merchant_id`. Anchoring pushes a
selective filter down to Databricks, which keeps the query fast and the result small
enough to render. Queries 13 to 15 run without an anchor to show how `LIMIT` pushes down.
The demo prints which anchor ids it picked so you can paste the same query into the
Workspace. It anchors on account 17813 and merchant 1 by default, and the numbers below are
measured against them. If either id is missing, the demo falls back to the lowest
`account_id` with an outgoing transfer or the lowest `merchant_id`. Swap in any id you like.

Every query whose `LIMIT` cuts its result sorts first, so the same rows come back on every
run. Query 7 returns all 12 of the anchor's merchant edges, so it needs no sort. Query 15 is
the exception, as its section explains.

Timings below come from runs against this instance on 2026-09-23. Every query in the `basic`
demo finished in 2s or less, and warm runs took 0.4 to 0.9s. The slowest was a cold run of
query 15. Warm runs are served from the Databricks result cache, with 1 to 3ms of warehouse
execution per statement. Most of the wall time is the Virtual Graph engine, Bolt, and the
network. Queries that return TIMESTAMP columns add one uncached `current_timezone()` round
trip of about 60ms. Treat the timings as rough.

## Counts and breakdowns (table)

### 1. How many accounts

A single label count, under a second. Counting two labels in one chained statement fails
with `42NG1`. The chained form is `MATCH ... WITH count ... MATCH ...`. Query 12 shows the
one-statement workaround with `UNION ALL`.

```cypher
MATCH (a:Account) RETURN count(a) AS accounts
```

### 2. How many merchants

```cypher
MATCH (m:Merchant) RETURN count(m) AS merchants
```

### 3. Accounts by type

Group-by on a scalar property pushes straight down to the warehouse. Good
bar-chart widget. Under a second. The query sorts on the type name second, to match
queries 5 and 6.

```cypher
MATCH (a:Account)
RETURN a.account_type AS account_type, count(*) AS accounts
ORDER BY accounts DESC, account_type ASC
```

### 4. Accounts by region

```cypher
MATCH (a:Account)
RETURN a.region AS region, count(*) AS accounts
ORDER BY accounts DESC, region ASC
```

### 5. Merchants by category

Two categories tie at 960 merchants, so the query sorts on the category name second.

```cypher
MATCH (m:Merchant)
RETURN m.category AS category, count(*) AS merchants
ORDER BY merchants DESC, category ASC
```

### 6. Top merchants by distinct customers

The first query that touches the edges. It scans the full `TRANSACTED_WITH`
relationship and still finishes in about a second on this instance. Three merchants tie
at 95 customers in places 9 to 11, across the `LIMIT 10` cut, so the query sorts on the merchant name second.

```cypher
MATCH (a:Account)-[:TRANSACTED_WITH]->(m:Merchant)
RETURN m.merchant_name AS merchant, count(DISTINCT a) AS customers
ORDER BY customers DESC, merchant ASC
LIMIT 10
```

## Visualizations (graph)

Run these in the Aura Workspace Query tab to see the graph. The `$account_id`
and `$merchant_id` parameters are the anchors. Replace them with a literal id, or
let the demo supply them.

### 7. Ego network: one account and the merchants it shops at

The simplest "here is the graph" shot: one account in the center, its merchants
fanned out around it. To color the merchants by category, add a rule-based style on
`category` in the Workspace. Anchored, so about a second.

```cypher
MATCH (a:Account {account_id: $account_id})-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
LIMIT 25
```

### 8. Ego network: one account and its transfer partners

The peer-to-peer view of the same account. The pattern is undirected, and the query
keeps the 25 most recent transfers, so it shows money flowing both in and out. For the
demo anchor it returns 9 outgoing and 16 incoming transfers. Transfers with the same
timestamp sort on `link_id`. About a second.

```cypher
MATCH (a:Account {account_id: $account_id})-[t:TRANSFERRED_TO]-(b:Account)
RETURN a, t, b
ORDER BY t.transfer_timestamp DESC, t.link_id ASC
LIMIT 25
```

### 9. Merchant star: one merchant and the accounts that use it

Flip the ego network around the merchant. One merchant in the center with its
customers around it. Merchant 1 has 37 customers, and the query keeps the 25 with the
lowest `account_id`. About a second.

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant {merchant_id: $merchant_id})
RETURN a, t, m
ORDER BY a.account_id, t.txn_id
LIMIT 25
```

### 10. Two-hop: accounts linked through a shared merchant

The query that earns the graph database. Two accounts are connected because they shop at
the same merchant, with no direct transfer between them. A flat table does not make this
link visible. The graph draws it as a two-hop path through the merchant. The anchor keeps
the merchant fan-out small, so it runs in about a second. The anchor has 670 such paths to
496 other accounts, and the query keeps the first 25 by merchant and account id.

```cypher
MATCH (a:Account {account_id: $account_id})-[t1:TRANSACTED_WITH]->(m:Merchant)
      <-[t2:TRANSACTED_WITH]-(b:Account)
WHERE a <> b
RETURN a, t1, m, t2, b
ORDER BY m.merchant_id, b.account_id, t1.txn_id, t2.txn_id
LIMIT 25
```

### 11. Two-hop: transfer chain

Follow the money two hops out: who does my counterparty pay? The query returns each chain
as a path, so the Workspace draws both transfers along with the three accounts.
`WHERE c <> a` drops chains that come straight back to the anchor. For the demo anchor that removes 102 of
10,500 chains. The query keeps the first 25 by account id, with `link_id` breaking ties
between repeat transfers. The chain shape is the point. About a second.

```cypher
MATCH p=(a:Account {account_id: $account_id})-[t1:TRANSFERRED_TO]->(b:Account)
        -[t2:TRANSFERRED_TO]->(c:Account)
WHERE c <> a
RETURN p
ORDER BY b.account_id, c.account_id, t1.link_id, t2.link_id
LIMIT 25
```

## Pushdown demonstrations

The last four queries show how the Virtual Graph splits and limits work. Each one runs in
about a second, and a cold run of query 15 can take 2s.

### 12. Count accounts and merchants in one statement

Counting two labels in one chained statement fails, as query 1 notes. `UNION ALL` is the
one-statement workaround. It runs as one Cypher statement and two pushed SQL statements,
one count per branch, and the engine concatenates the results.

```cypher
MATCH (a:Account)  RETURN 'accounts'  AS label, count(a) AS n
UNION ALL
MATCH (m:Merchant) RETURN 'merchants' AS label, count(m) AS n
```

### 13. Any 25 account-merchant edges

An unanchored single hop. There is no starting filter, and `LIMIT 25` still pushes into
the SQL, so exactly 25 rows come back. The sort on ids makes them the same 25 on every run.

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
ORDER BY a.account_id, m.merchant_id, t.txn_id
LIMIT 25
```

### 14. Any 25 two-hop transfer chains

An unanchored two-hop over `TRANSFERRED_TO`. Each row is a path, so the Workspace draws
both transfers with the three accounts. The limit still pushes down to 25 rows. The pushed
SQL also carries Cypher's relationship-uniqueness rule. The sort on account ids and
`link_id` makes the 25 paths the same on every run.

```cypher
MATCH p=(a:Account)-[t1:TRANSFERRED_TO]->(b:Account)-[t2:TRANSFERRED_TO]->(c:Account)
RETURN p
ORDER BY a.account_id, b.account_id, c.account_id, t1.link_id, t2.link_id
LIMIT 25
```

### 15. Any 25 four-hop transfer chains

Depth escalation. Each row is a four-transfer path, so the Workspace draws the whole chain.
`LIMIT 25` bounds the output, and exactly 25 rows come back. The join work behind those rows
still grows with each hop. An anchor is what makes a deep traversal cheap.

This query has no `ORDER BY`, so it returns an arbitrary 25 paths. A sort and a `LIMIT`
both push into the SQL. The sort makes the warehouse build every four-hop chain before it
can keep the first 25. Without the sort, the warehouse can stop once it has 25 chains. With
`ORDER BY a.account_id` added, the query ran past a 90s timeout.

```cypher
MATCH p=(a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
      -[:TRANSFERRED_TO]->(d:Account)-[:TRANSFERRED_TO]->(e:Account)
RETURN p
LIMIT 25
```

## Why anchoring matters

The Virtual Graph compiles Cypher into SQL and pushes most of it down to the backing
Databricks warehouse. A query anchored on a single node id becomes a selective SQL filter
that the warehouse runs quickly and that returns a handful of rows. A `LIMIT` bounds how
many rows come back, and queries 13 to 15 show it pushes down at every depth. It does not
bound the join work. The same two-hop pattern under an aggregation, or with no `LIMIT`,
runs the full join across the relationship table. Without a `LIMIT` it also ships every
matching row back through the engine, and that transfer can cost more than the join. A
`LIMIT` without an `ORDER BY` returns an arbitrary slice, anchored or not. Queries 8 to 11,
13, and 14 sort on ids to fix the slice. For demos and visualizations, start from a specific
account or merchant.

For how the warehouse and the small JDBC connection pool shape performance, see the
"Performance and the connection pool" section of [`best-practices.md`](best-practices.md).
That section also covers the transaction timeout. The read-only constraint is listed under
"Cypher coverage" in the same guide.
