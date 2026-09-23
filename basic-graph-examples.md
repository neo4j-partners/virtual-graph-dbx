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
Workspace. It picks the first account with an outgoing transfer, account 17813, and the
first merchant, merchant 1. Swap in any id you like.

Timings below come from the runs against this instance on 2026-09-23. Every query in the
`basic` demo finishes in 0.5 to 1.4s. The timings are dominated by the backing Databricks
warehouse, so treat them as rough.

## Counts and breakdowns (table)

### 1. How many accounts

A single label count, under a second. Counting two labels in one chained statement
(`MATCH ... WITH count ... MATCH ...`) fails with `42NG1`. Query 12 shows the one-statement workaround with `UNION ALL`.

```cypher
MATCH (a:Account) RETURN count(a) AS accounts
```

### 2. How many merchants

```cypher
MATCH (m:Merchant) RETURN count(m) AS merchants
```

### 3. Accounts by type

Group-by on a scalar property pushes straight down to the warehouse. Good
bar-chart widget. Under a second.

```cypher
MATCH (a:Account)
RETURN a.account_type AS account_type, count(*) AS accounts
ORDER BY accounts DESC
```

### 4. Accounts by region

```cypher
MATCH (a:Account)
RETURN a.region AS region, count(*) AS accounts
ORDER BY accounts DESC
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
and `$merchant_id` parameters are the anchors; replace them with a literal id, or
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
demo anchor it returns 9 outgoing and 16 incoming transfers. About a second.

```cypher
MATCH (a:Account {account_id: $account_id})-[t:TRANSFERRED_TO]-(b:Account)
RETURN a, t, b
ORDER BY t.transfer_timestamp DESC
LIMIT 25
```

### 9. Merchant star: one merchant and the accounts that use it

Flip the ego network around the merchant. One merchant in the center with its
customers around it. About a second.

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant {merchant_id: $merchant_id})
RETURN a, t, m
LIMIT 25
```

### 10. Two-hop: accounts linked through a shared merchant

The query that earns the graph database. Two accounts are connected because they shop at
the same merchant, with no direct transfer between them. A flat table does not make this
link visible. The graph draws it as a two-hop path through the merchant. The anchor keeps
the merchant fan-out small, so it runs in about a second.

```cypher
MATCH (a:Account {account_id: $account_id})-[t1:TRANSACTED_WITH]->(m:Merchant)
      <-[t2:TRANSACTED_WITH]-(b:Account)
WHERE a <> b
RETURN a, t1, m, t2, b
LIMIT 25
```

### 11. Two-hop: transfer chain

Follow the money two hops out: who does my counterparty pay? The query returns the
paths, so the Workspace draws the relationships along with the accounts. `WHERE c <> a`
drops chains that come straight back to the anchor. The chain shape is the point. About
a second.

```cypher
MATCH p=(a:Account {account_id: $account_id})-[:TRANSFERRED_TO]->(b:Account)
        -[:TRANSFERRED_TO]->(c:Account)
WHERE c <> a
RETURN p
LIMIT 25
```

## Pushdown demonstrations

The last four queries show how the Virtual Graph splits and limits work. Each one runs in
about a second.

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
the SQL, so exactly 25 rows come back.

```cypher
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
LIMIT 25
```

### 14. Any 25 two-hop transfer chains

An unanchored two-hop over `TRANSFERRED_TO`. The limit still pushes down to 25 rows. The
pushed SQL also carries Cypher's relationship-uniqueness rule.

```cypher
MATCH (a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
RETURN a, b, c
LIMIT 25
```

### 15. Any 25 four-hop transfer chains

Depth escalation. `LIMIT 25` bounds the output, and exactly 25 rows come back. The join
work behind those rows still grows with each hop. An anchor is what makes a deep
traversal cheap.

```cypher
MATCH (a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
      -[:TRANSFERRED_TO]->(d:Account)-[:TRANSFERRED_TO]->(e:Account)
RETURN a, b, c, d, e
LIMIT 25
```

## Why anchoring matters

The Virtual Graph compiles Cypher into SQL and pushes most of it down to the backing
Databricks warehouse. A query anchored on a single node id becomes a selective SQL filter
that the warehouse runs quickly and that returns a handful of rows. A `LIMIT` bounds how
many rows come back, and queries 13 to 15 show it pushes down at every depth. It does not
bound the join work. The same two-hop pattern under an aggregation, or with no `LIMIT`,
becomes a full join across the relationship table and is slow. An unanchored `LIMIT` also
returns an arbitrary slice of the network. For demos and visualizations, start from a
specific account or merchant.

For how the warehouse and the small JDBC connection pool shape performance, see the
"Performance and the connection pool" section of [`best-practices.md`](best-practices.md).
That section also covers the transaction timeout. The read-only constraint is listed under
"Cypher coverage" in the same guide.
