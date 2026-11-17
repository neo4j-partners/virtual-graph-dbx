# Release Notes

## 2026-09: Virtual Graph engine update

The latest Virtual Graph update is a big step forward. We re-ran every demo, support script, and documented finding in this project against it.

**Faster queries across the board**

- A full scan of the 300,000-row transfer relationship now aggregates in 3.6s. It used to take 40 to 45s.
- Grouping by a whole node is now fast. The structuring query runs in 1.1s, down from about 38s. The new-account velocity query runs in 1.3s, down from about 985s. A node group key still keeps the `GROUP BY` out of the SQL, so the graph engine does the grouping. The demo queries group by scalar ids, which push the `GROUP BY` down.
- `count(DISTINCT ...)` is now fast. A 7-day window takes 2.0s, down from more than 5 minutes.
- An unanchored four-hop traversal with `LIMIT 25` returns in 2.4s, down from about 14s.
- Each of the 15 `basic` queries finishes in 0.4 to 2.0s.
- The fraud visualization stars render in about 1s each, down from 9 to 10s.

**TIMESTAMP results are no longer slow**

- The engine no longer makes a `current_timezone()` round trip for each TIMESTAMP value. Warehouse query history still shows one `SELECT current_timezone()` of about 60ms after each query that returns TIMESTAMP columns. A 210,289-row result triggered only one.
- Queries that return relationships or timestamp columns now come back in under 2s at 25 rows.

**The slow fraud queries now finish**

- The shared-merchant burst query (query 9) completes in 5 to 9s. It used to exceed the 120s timeout.
- The rapid-turnover query (query 10) completes. It used to run past 100s without finishing. Its original form took about 210s. The pushed-down rewrite under **Project changes** takes about 1s.
- The pass-through mule query (query 8) runs with its output column renamed. It takes 1 to 2s after the pushdown rewrite.

**More Cypher runs on the server**

- A HAVING-style `WHERE` on an aggregate alias now works, so threshold filters can stay in Cypher. On the aggregating `WITH` itself, it keeps the `GROUP BY` out of the SQL. In a second `WITH`, the `GROUP BY` pushes down.
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
- Streamed `nodeId`s now decode back to `accountId` with `(nodeId & (2^50 - 1)) >> 1`.

**Databricks side**

- Warehouse query history now lands in about 3 minutes, down from 11 to 25 minutes.
- The 100M-row spike still runs with zero spill.

**Project changes**

- The `slow-gds` demo was removed. Its main failure case, the full-graph projection, now succeeds.
- References to the classic `CALL gds.graph.project('g', 'Account', ...)` form were removed. The Cypher-projection form is the supported path.
- The `timezone` demo was removed. The engine no longer makes the per-row `current_timezone()` round trip it measured.
- The `vg-heavy` support script was removed.
- The fraud demo now runs as one tier. `uv run vg-demo` runs queries 1-10, and the default `--timeout` is 300s.
- Query 8, the pass-through mule, was fixed. It now uses a distinct output alias. It groups per incoming transfer first, adds a `forwardedIn` column, and counts each incoming transfer's dollars once in `volume`.
- Query 3 now counts real transfers and real dollars. The pattern matches once per pair of transfers, so the query counts each direction with `count(DISTINCT f.linkId)` and `count(DISTINCT g.linkId)`. It divides each direction's sum by the other direction's count. The top pair is 7855 and 13727.
- Every fraud query and the `basic` breakdowns now sort on a tie-breaking id, so tied rows come back in a fixed order.
- `basic` query 8 now returns the 25 most recent transfers, so it shows money in both directions. Queries 11, 14, and 15 now return paths, so the relationships draw.
- `basic` queries 8 to 11, 13, and 14 now sort on ids, so they return the same rows on every run. Query 15 stays unsorted, because a sort on the four-hop pattern makes the warehouse build every chain before the `LIMIT` applies.
- The `gds-probe` timestamp scenarios now project `toInteger(t.transferTimestamp) * 1000`, bound in a `WITH`. This form raises no `01N52` warning. The sweep runs on the default 7-day window.
- The `100m` demo now bypasses the warehouse result cache and reports a cached statement as "cached, not measured". It reads `.env` without exporting it, so the Databricks profile alone supplies the host and credentials.
- Queries 3, 5, 6, 7, 8, and 10 were rewritten so their `GROUP BY` pushes down to Databricks. The rules come from the SQL each statement produced in `system.query.history`:
  - Every group key is a scalar property. Query 8 groups on `tIn.linkId` instead of `tIn`, and query 10 groups on `mule.accountId` instead of `mule`.
  - Every threshold sits in a second `WITH`, not on the aggregating `WITH`.
  - Query 3 counts `count(DISTINCT f.linkId)` instead of `count(DISTINCT f)`.
  - Query 10 computes its turnaround as `avg(toInteger(tOut.transferTimestamp) - toInteger(tIn.transferTimestamp))` in seconds instead of `avg(duration.inSeconds(...))`. The demo converts the seconds to hours in Python.
- Each rewrite returns the same rows as the form it replaced. Query 10 went from 208s to about 1s. Query 7's transfer half went from about 11s to under 1s. Query 8 went from about 3.6s to about 1s. Query 3 went from about 2.2s to about 0.6s. The full fraud demo now takes about 20s.
- Query 9 still groups by `date(t.txnTimestamp)`, so it does not push down. An epoch-day key pushes down but took about 35s, because the pushed `collect(DISTINCT ...)` arrays are slow to ingest.
- Fraud thresholds and top-N now run in Cypher instead of Python.
- The `fast-gds` demo now prints the decoded `accountId` next to each streamed `nodeId`.
- The `fast-gds` demo now gives each default run a unique graph name, so a failed session never blocks the next run. On a session conflict it retries once under a new name.
- The read-timeout flags for GDS provisioning were removed. The default 7-day projection finishes without them.
