# Creating a GDS Session on a Virtual Graph

This guide explains how to run Graph Data Science algorithms against a Neo4j Virtual
Graph by creating a **GDS Session**. The rest of the guide calls Graph Data Science GDS.

## Background

GDS is **not** supported as an in-database plugin on Virtual Graph. The only way to
run graph algorithms is through **GDS Sessions**. A GDS Session is an ephemeral, on-demand
compute environment. It projects your data into an in-memory graph, runs algorithms, and
can be torn down afterward.

The working path is the **Cypher projection** form shown below. Streamed `nodeId`s decode
back to source IDs with a simple bit formula, covered below. Session provisioning takes
31 to 44 seconds. A projection of all 300,000 transfers completes in about 41 seconds.

## When you need GDS, and when plain Cypher is enough

Most local fraud signal can be expressed in plain Cypher over the base entities, so reach
for a GDS Session only when you need a global, transitive score. The table maps each GDS
algorithm to its plain-Cypher stand-in.

| Signal | GDS version | Plain-Cypher equivalent |
|---|---|---|
| Mule / hub detection | PageRank | Degree counting (local proxy) |
| Fraud ring discovery | WCC / Louvain | Bounded-depth connectivity, shared-merchant co-occurrence |
| Bridge / layering node | Betweenness | Pass-through pattern (receives then forwards) |
| Coordinated bursts | community + temporal | Same-merchant / same-window grouping |

What plain Cypher keeps: degree, reciprocity, cycles, fan-in/out, velocity, and
co-occurrence, the workhorses of rules-based fraud detection. What it loses is ranking
quality. PageRank weights a hub by the importance of who points at it, not just how many,
so it catches mules one layer removed from the obvious hubs. Louvain and WCC partition the
whole graph into rings rather than surfacing the fixed-shape patterns you anticipated.

The trade-off: plain Cypher gives fast, explainable, rules-based candidates, excellent for
triage and the "find the suspects" step. GDS gives the global scores that catch the rings
your rules did not think to look for. For the plain-Cypher forms of these signals, see
[`best-practices.md`](best-practices.md).

## The key: how a session gets created

A GDS Session is triggered by passing a **configuration** to `gds.graph.project` that
contains **either**:

- an **instance size** in GB of memory, for example `{ memory: '2GB' }`, **or**
- an existing **`sessionId`** to reuse a running session.

Without one of these, the call won't start a session.

## Recommended pattern: Cypher projection

On Virtual Graph, express the projection as a `MATCH ... RETURN gds.graph.project(...)`
statement. The projection takes the source and target nodes. The label and type details
go in the `dataConfig` parameter, and the memory config is the final argument.

This is the projection the `fast-gds` demo runs. It projects the `TRANSFERRED_TO`
relationships between accounts from a recent time window:

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
RETURN gds.graph.project(
  'transfers',
  src,
  dst,
  {
    sourceNodeLabels: labels(src),
    targetNodeLabels: labels(dst),
    relationshipType: type(t)
  },
  { memory: '2GB' }
)
```

The first config object, `dataConfig`, describes the graph structure. The final config
object, `{ memory: '2GB' }`, is what provisions the session.

The `WHERE` clause picks the edges to project. The demo computes the window start from the
dataset's latest transfer and passes it as `$since`, because temporal arithmetic inside a
`WHERE` is unsupported. Drop the `WHERE` to project all 300,000 transfers.

### Projecting a relationship weight

Add `relationshipProperties` to the `dataConfig` to carry a numeric property into the
graph. This projection keeps the transfer amount so an algorithm can use it as a weight:

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= datetime("2024-03-23T23:58:00Z")
RETURN gds.graph.project(
  'transfersWeighted',
  src,
  dst,
  {
    sourceNodeLabels: labels(src),
    targetNodeLabels: labels(dst),
    relationshipType: type(t),
    relationshipProperties: { amount: t.amount }
  },
  { memory: '2GB' }
)
```

A projection weighted by `amount` provisions in the same time as an unweighted one. Both
took about 33s in the `gds-probe` run on 2026-09-24. Scenarios that add a timestamp or a
node property took about 10s longer, at 43.6 to 44.6s.

## ID requirements

The official [GDS page](https://neo4j.com/docs/virtual-graph/aura/gds/) sets two rules for
a GDS projection on a Virtual Graph.

- **The GDS call comes last.** The `gds.graph.project(...)` call must be the last part of
  the query, as in the `MATCH ... RETURN gds.graph.project(...)` form above.
- **Every ID column is a single integer.** The ID column of each projected node and
  relationship table must be a single column, not a composite key. It must hold a 50-bit
  unsigned integer, from 0 to 1,125,899,906,842,623. It must also be unique within its
  source table.

A projection that breaks the ID rule fails with `42NG1: Unsupported syntax: Unsupported id`.
The Finance Genie IDs meet the rule: `account_id`, `merchant_id`, `txn_id`, and `link_id`
are all single integer columns. A model that uses a string or concatenated key works for
plain Cypher but cannot be projected into GDS.

## Project only numeric properties

The projections above carry labels, the relationship type, and at most the numeric
`amount`. Every projected property must be **numeric**. A node property can be a Long, a
Double, or a list of either. A relationship property must be a single Long or Double. A
GDS in-memory graph cannot hold a temporal or string property, so adding one to the
`dataConfig` makes the projection fail fast, before the session provisions, with an
`IllegalArgumentException` that names the offending property and type:

```
The property `relationship.ts` contained a value of type `DateTime`, which is not supported.
The property `sourceNode.account_hash` contained a value of type `String`, which is not supported.
```

So `relationshipProperties: { amount: t.amount }` projects and is usable as a
`relationshipWeightProperty`. For time, convert the timestamp to epoch milliseconds in a
`WITH` before the projection:

```cypher
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
WITH src, dst, t, toInteger(t.transfer_timestamp) * 1000 AS transfer_timestamp_ms
RETURN gds.graph.project(
  $graph,
  src,
  dst,
  { sourceNodeLabels: labels(src),
    targetNodeLabels: labels(dst),
    relationshipType: type(t),
    relationshipProperties: { transfer_timestamp_ms: transfer_timestamp_ms } },
  { memory: $memory }
) AS result
```

The Virtual Graph pushes `toInteger()` on a timestamp down to SQL as epoch seconds, and
the form raises no warning. Every transfer timestamp is a whole second, so `* 1000` gives
the same values as `epochMillis`. Reading `.epochMillis` off the property returns those
values too, but the server flags it with the `01N52` unknown-property warning. The
conversion has to go through the `WITH`, because `toInteger()` on a timestamp inside the
config map fails with `22N38`. Stock Cypher rejects `toInteger()` on a timestamp, so this
form is specific to the Virtual Graph. Keep string identifiers such as
`account_hash` outside the GDS projection and join them to streamed account IDs in the
application when needed. The `gds-probe` demo (`src/demos/gds_probe.py`, run with
`uv run vg-demo --demo gds-probe`) projects the supported numeric values and skips
non-numeric node properties before provisioning. This is standard GDS typing behavior.

## Running an algorithm

Once the session and projection exist, run algorithms against the named graph:

```cypher
CALL gds.pageRank.stream('transfers')
YIELD nodeId, score
RETURN nodeId, score
ORDER BY score DESC
LIMIT 10
```

On the weighted projection, name the property as the weight. PageRank then ranks accounts
by the money that flows into them, not only by the number of senders:

```cypher
CALL gds.pageRank.stream('transfersWeighted', { relationshipWeightProperty: 'amount' })
YIELD nodeId, score
RETURN nodeId, score
ORDER BY score DESC
LIMIT 10
```

Weighted PageRank works. The `gds-probe` sweep runs it on the 7-day window after
projecting the `amount` property.

The standalone `CALL gds.<algorithm>.stream(...)` form works. The stream returns
GDS-internal `nodeId`s. Each one encodes the source table in its top bits. Its low 50
bits hold the source ID shifted left by one. To recover the `account_id`, mask the low 50
bits and shift right by one:

```
account_id = (nodeId & (2^50 - 1)) >> 1
```

The formula was checked against in-degree on live PageRank runs. On a 2-hour window, the
accounts with the highest scores matched the accounts with the highest in-window in-degree
exactly. On the 7-day window, the top 10 accounts each have 18 to 24 incoming transfers,
against a mean of about 2.4. Six of them are in the top 17 by in-degree. PageRank weighs
who sends to an account, not only how many, so a close but inexact match is expected. The
`fast-gds` demo applies this formula and prints the decoded `account_id` next to each
streamed `nodeId`.

## No write-back

There is **no write-back to the relational source** from a session. Options for
handling results include:

- Streaming results back to your application.
- Writing to a separate physical graph via a composite database.
- Writing to Parquet on a cloud bucket and feeding that back into the data warehouse.
- Keeping the session alive and serving from it as an ephemeral cache (re-create the
  projection if it expires).

## Tested: the working path

A live test ran the Cypher projection form for PageRank over `Account` nodes and
`TRANSFERRED_TO` relationships against instance `ge7826b1`. The Sessions path works end
to end: it provisions a session, registers an in-memory graph, streams PageRank, and drops
cleanly. The harness is the `fast-gds` demo (`src/demos/gds_fast.py`, run with
`uv run vg-demo --demo fast-gds`).

The "window" here is a time-range filter on the transfer rows. `--since-days` and
`--since-hours` keep only transfers from the most recent N days or hours of the data. The
resulting row count is the edge count projected into the graph. The default is 7 days. A
run with the default window produced these timings, across runs on 2026-09-23 and
2026-09-24:

- Sizing count: 0.3 to 3.2s for 23,198 edges, no session. The slower figure came from a
  cold warehouse.
- Projection that provisions the session: 34.4 to 38.0s, returning a registered graph of
  15,588 nodes and 23,198 relationships. The returned `projectMillis` was 33,357 to
  34,835.
- `gds.pageRank.stream`: 2.3 to 2.5s for the top 10 accounts, with real scores.
- `gds.graph.drop`: 1.2 to 2.5s.

The Databricks query behind the projection took about 1 second. Almost the entire
projection call is session provisioning, the cold start of the ephemeral compute. The
Databricks query and the algorithm are small by comparison. Provisioning took 31 to 44
seconds across runs, and it was about the same for 298 edges as for 23,198.

**Projection size is flexible.** A projection of all 300,000 transfers completed in 41.0s.
Provisioning is most of that time. Use `--count-only` to count the rows in a window
with one cheap warehouse query and no session. Use `--keep` to reuse a provisioned
session.

**The property sweep.** The `gds-probe` demo runs scenarios A to E on the same 7-day
window. Each scenario provisions its own session and projects in 33 to 46 seconds.

**Cleanup after a failure.** After any projection failure, both demos try to drop the
graph they were projecting. That covers a server error, a dropped Bolt connection, and a
second session conflict. The drop is best effort. If the connection is gone for good,
the graph can stay behind. A default `fast-gds` run uses a new graph name with a random
suffix, so a leftover graph cannot block the next run. If the projection fails with a
session conflict, the demo retries once under a new name. With `--graph`, the demo drops a
stale graph of that name before it starts. `gds-probe` gives each scenario its own name,
built from `--graph` plus a random suffix. A scenario whose final drop fails reports
PARTIAL. To check for leftovers, run `CALL gds.graph.list()`.

## Future expansion: possible new examples

Session provisioning is the fixed cost, and each algorithm after it runs in seconds. The
best new examples project once and run several algorithms on the same session. These are
candidates. None of them has been built or tested on the Virtual Graph yet.

- **Money-flow PageRank:** Weighted PageRank on the transfer amount ranks accounts by the
  value flowing into them. The `gds-probe` demo already shows that the weighted projection
  and algorithm work.
- **Fraud rings with WCC:** Weakly Connected Components splits the transfer graph into
  connected groups of accounts. Small, dense components are ring candidates. This is the
  GDS form of the ring-discovery row in the table above.
- **Communities with Louvain:** Louvain finds groups of accounts that transfer mostly
  among themselves, even when those groups connect to the rest of the graph.
- **Shared merchants with Node Similarity:** A projection of `Account` to `Merchant` over
  `TRANSACTED_WITH` lets Node Similarity score pairs of accounts that shop at the same
  merchants. This is the GDS counterpart of the shared-merchant burst query.
- **Bridge accounts with Betweenness:** Betweenness centrality scores the accounts that
  sit between groups, the layering accounts in a money-laundering chain. Its run time at
  25,000 nodes needs testing.
- **GDS then Cypher:** Take the top PageRank accounts, decode their IDs, and pass them to
  a plain Cypher query on the Virtual Graph, such as the pass-through mule check. GDS
  finds the suspects, and Cypher explains them.

A single `gds-examples` demo could run money-flow PageRank, WCC, Louvain, and the GDS then
Cypher step on one 7-day session. It would cost one provisioning plus a few seconds for
each algorithm.
