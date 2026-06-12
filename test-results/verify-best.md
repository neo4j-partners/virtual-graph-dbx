# Verifying the Best-Practices Doc

## ELI5 summary

The graph database here does not store any data. Every time you ask it a graph question, it secretly translates the question into SQL and asks the Databricks warehouse. Some questions translate well and run fast; some translate badly and run slow. This document is the record of catching the actual SQL it sends, to find out which is which and why. The findings, in plain English:

1. **Filters on numbers work great.** "Only transfers between 9,000 and 10,000" gets handed to Databricks just like date filters do. Databricks does the work and sends back only the answers (Phase 4).
2. **UNION ALL works.** You cannot count accounts and merchants in one chained query, it errors, but glue two counts together with UNION ALL and it works. Secretly it runs them as two separate questions and staples the results together (Phase 6).
3. **LIMIT 25 really means 25.** Even on huge "show me any 25 connections" queries with no starting point, Databricks is told to stop at 25. The catch: it still does the searching work to find those 25, so a deep four-hop search took 13 seconds even though only 25 rows came back (Phase 7).
4. **The big mystery, solved.** Some queries were bizarrely slow, taking 10 minutes when Databricks finished its part in under a second. The culprit: every time the engine receives a **timestamp** (a date *and* a time), it phones Databricks again just to ask "what timezone are we in?", once per row. Three thousand rows, three thousand phone calls, and it never remembers the answer. Plain dates with no time part never need the call, which is why some queries dodged it. Full detail in the [Phase 9 results](#phase-9-results-2026-06-11) and the [Phase 9 work log](#phase-9-run-2026-06-11).

The one-line takeaway: the warehouse is almost never the problem; slow queries are slow because of how the translation happens, and the rules are now known. Keep aggregations on plain ID columns, always use LIMIT, and never pull back thousands of rows that contain timestamps. Phase 8 has now folded these findings back into the best-practices doc, so the verification is complete.

## Status

- Phase 1 (top-N and order-by pushdown): **Complete**, run 2026-06-10, history verified. Headline: neither order-by nor limit ever reaches the warehouse SQL; the graph engine sorts and trims after the full group set ships from Databricks. The fast top-N is real but happens engine-side.
- Phase 2 (threshold behavior): **Complete**, run 2026-06-10, history verified. The HAVING-style failure is reproduced and logged, and the working form provably ships the full group set (9,847 produced rows in history, matching the source table).
- Phase 3 (anchored traversal): **Complete**, run 2026-06-10, history verified. Both halves answered: the anchor appears in the pushed SQL as a parameterized filter and the limit pushes down too (`WHERE a.account_id = ? ... LIMIT ?`).
- Phase 4 (capture the slow path): **Complete**, run 2026-06-11, history verified. Only the structuring query needed a new run; the phase's other two captures (node-grouped and count-distinct SQL) were taken in Phase 5 and are reused. Headline: the amount-range filter pushes down exactly like the time filter, as two bound parameters in the pushed SQL, next to a pushed `GROUP BY` that produced exactly the 196 final groups.
- Phase 5 (developer questions): **Complete**, run 2026-06-11. The node-grouped form's SQL is captured: a raw row pull with no GROUP BY, followed by one `SELECT current_timezone()` round trip per row, which is the real slow path. The 1-day node-grouped run never returned because the local network dropped mid-wait; a 6-hour-window rerun finished and settled the equivalence question: the key-grouped and node-grouped forms return identical results (764 rows, same counts, same outflows to the cent). The count-distinct form finished in 4.6s on the 1-day window, and its history row settles the verdict: no pushdown; the warehouse received a raw join pull with no DISTINCT and no GROUP BY, and the engine deduplicated.
- Phase 6 (union all for two-label counts): **Complete**, run 2026-06-11, history verified. The documented chained-form failure is reproduced and logged, and the union-all form works: one Cypher statement, correct counts for both labels. History shows the engine splits it into two pushed statements, one count per label, and concatenates engine-side.
- Phase 7 (limit on an unanchored join): **Complete**, run 2026-06-11, history verified. The developer is right: the limit pushes into the SQL on unanchored traversals at every depth tried, single-hop, two-hop, and a four-hop escalation, each producing exactly 25 rows. The limit bounds the output, not the join work, so the four-hop still cost 13.5 seconds of warehouse time.
- Phase 8 (fold findings into the best-practices doc): **Complete**. The nine phases'
  results were folded into [`best-practices.md`](best-practices.md): Pattern 3 reversed to
  write `ORDER BY`/`LIMIT` in Cypher, a new Pattern 7 added for the per-TIMESTAMP timezone
  round trip, Pattern 1's slow-path explanation corrected, Pattern 5's anchor rule softened,
  the `count(DISTINCT)` claim restated as never-pushes-down, and the coverage tables given
  the logged error strings and the `UNION ALL` two-label workaround.
- Phase 9 (timezone round-trip trigger): **Complete**, run 2026-06-11, history verified. The trigger is settled: one `SELECT current_timezone()` per TIMESTAMP value the engine materializes into a Cypher datetime, regardless of whether it sits in a relationship, a node, or a bare projection. DATE values never trigger it, and there is no session cache; reruns pay full price. All five committed predictions hit exactly (25, 0, 25, 0, and 25 plus 25), and the rule retroactively explains every storm and every quiet run in Phases 3, 5, and 7 with no exceptions.

## TLDR

The best-practices guide makes several claims about what the Virtual Graph pushes down to the Databricks warehouse and what stays client-side. Going in, only the row-level filters were proven to push down; the top-N claim, the sorting claim, and a few others had never been directly tested. This plan verified each open claim using the same method the perf tests already proved out: run the query, then read the exact SQL the warehouse received from query history and check how many rows it produced. Most tests reused queries that already existed in the docs; only small variants were created. The verification is now complete: every question below carries its test result and answer, the exact warehouse SQL is in the work log, and Phase 8 has folded the findings back into the guide.

## Overview

The guide's "shared query shape" says to aggregate and order on the server, then apply the threshold filter and the top-N in the application. That sentence bundles three different behaviors together:

- Row-level filters, such as a time window or an amount range, push down to the warehouse. This is already verified: the captured SQL in the perf results shows the filter inside the SQL the warehouse ran, and the row counts confirm filtering happened before anything came back.
- Threshold filters on an aggregate, the HAVING-style ones, fail outright with an unsupported-syntax error, so they must be client-side. This was documented but had no logged reproduction in the results files; Phase 2 added one.
- Top-N, meaning order-by plus limit, had never been verified either way. None of the previously captured SQL statements contained an order-by or a limit, because every tested query deliberately left them off. Going in, we did not know whether a Cypher limit becomes a SQL limit or whether the graph engine cuts rows after they all ship back; Phase 1 settled it.

The verification method is already established and needs no new tooling. The query history table on the backing warehouse records the exact SQL text Aura submitted and the number of rows each statement produced. That pair answers every question here: if the limit appears in the SQL text and the produced row count is small, the top-N pushed down; if the SQL has no limit and the produced row count is the full group count, the cut happened client-side after the data moved. Wall-clock alone decides nothing, because there is a third possibility: the graph engine could fetch lazily and close the cursor after the limit is satisfied, giving a fast return with no SQL limit at all. The SQL text settles whether the limit pushed down. When it did not, the warehouse produces the full group count either way, and the wall-clock separates the two remaining cases: slow at the full-ship time means every row crossed the wire and was trimmed client-side, fast despite the full produced count means a lazy fetch stopped early. Query history lags about eleven minutes, so each phase records client wall-clock immediately and pulls history afterward. The Aura Workspace query tab can show the plan via explain as a second source; the command line cannot capture it.

Operational note for future agents, on pulling history: the MCP `execute_sql` tool has a hard 60-second cap that `system.query.history` scans regularly outrun, and the cap ignores the tool's timeout parameter. Two workarounds, in order: first, keep the scan cheap by selecting computed flags (`statement_text ILIKE '%GROUP BY%'`) and `left()`/`right()` slices instead of the full statement text. When even that times out, bypass the MCP tool and use the SQL statements API directly: `databricks api post /api/2.0/sql/statements` (profile `aws-partner-rk`, warehouse `b0fffb8e3255bf85`) with `wait_timeout` set to `5s` and `on_wait_timeout` set to `CONTINUE`, then poll `GET /api/2.0/sql/statements/<statement_id>` until SUCCEEDED. Submit async always; the CLI's synchronous wait also dies at 60 seconds of inactivity.

## Outstanding questions

- **Does a Cypher limit push down?** When a query ends with order-by plus limit, does the warehouse receive a SQL limit and return only those rows, or does it return everything and the graph engine trims afterward? This was the headline question going in.
  - Test result (Phase 1, 2026-06-10): all five fan-out variants generated identical SQL with no `LIMIT`, and the warehouse produced the full 22,096 rows every time. Yet the limit runs returned in 0.6 to 0.7 seconds versus 3.3 to 3.4 seconds for full results, and the top-N run returned the correct global top 10. Contrast: the Phase 3 anchored traversal, a plain non-aggregating query, did get its limit pushed into SQL.
  - Answer: on an aggregation, no. The warehouse returns everything and the graph engine trims afterward. The limit is still worth writing, because it cuts the slow engine-to-client shipping to just the trimmed rows; the warehouse cost is unchanged either way. On a plain anchored traversal the limit does push down (see Phase 3).
- **Does a server-side order-by push down or add a post-processing step?** The doc contradicts itself: the shared-shape bullet says to order on the server, while Pattern 3's worked example moves the sort client-side. One of those needs to win, backed by evidence.
  - Test result (Phase 1, 2026-06-10): no generated SQL contained an `ORDER BY`, on either the aggregate alias or the plain column. Both sorted runs returned correctly ordered rows in the same ~3.4 seconds as the unsorted control.
  - Answer: order-by never reaches the warehouse; the graph engine sorts as a post-processing step. At this scale it is free, so the doc's two positions cost the same warehouse work; the contradiction should be resolved by saying the sort happens in the graph engine, and ordering in Cypher is fine and is what makes an engine-side top-N work.
- **Is the HAVING-style failure reproducible and logged?** The coverage table says a threshold on an aggregate alias fails with unsupported syntax. A one-time logged reproduction would make the claim citable.
  - Test result (Phase 2, 2026-06-10): adding only `WHERE transfers >= 5` after the aggregating `WITH` of the otherwise-working fan-in query fails instantly with `Neo.ClientError.Statement.SyntaxError`, GQL status `42NG0: Unsupported syntax`, pointing at the `WITH` line. The textbook appendix form fails with the identical error. Both fail at parse time, so nothing reaches the warehouse.
  - Answer: yes. The failure is reproduced in isolation and the exact error is logged in the Phase 2 work log, so the claim is now citable.
- **Do the working demo queries really return the full ungrouped result?** If the threshold genuinely lives client-side, the produced row count for the fan-in query should equal the full per-account group count, not the thresholded count. Worth confirming once with the produced-rows column.
  - Test result (Phase 2, 2026-06-10): the working fan-in form returned 9,847 rows in 2.1 seconds, and a direct SQL count on `account_links` shows exactly 9,847 distinct recipients in the same 7-day window, of which only 1,157 meet the `transfers >= 5` threshold.
  - Answer: yes. The client receives the full group set, and the client-side threshold then discards about 88 percent of the shipped rows. The check used the source table directly, which is stronger than the produced-rows column.
- **Does the limit on anchored visualization queries push down?** Pattern 5's anchored traversal ends with a limit of 25. Whether that limit reaches the warehouse, and whether the account-id anchor appears in the pushed SQL as a selective filter, has not been captured.
  - Test result (Phase 3, 2026-06-10): the anchored traversal on account 184 returned in 3.5 seconds with exactly 8 rows, and the source table confirms account 184 has exactly 8 transactions to 8 merchants. The history pull then showed the generated SQL ends with `WHERE (a.account_id = ?) LIMIT ?`, and the statement produced 8 rows.
  - Answer: yes on both halves. The account-id anchor reaches the warehouse as a parameterized filter, and the limit pushes down into the SQL too. This is the opposite of the aggregation case in Phase 1, so the rule is: a limit pushes down on a plain traversal but not on an aggregation.
- **What does the slow path actually look like in SQL?** The doc says grouping by a node materializes and that count-distinct blocks pushdown, based on timing alone. Capturing the generated SQL for one slow form would show whether the warehouse receives a different query or whether the engine pulls raw rows and aggregates itself.
  - Test result (Phase 5 captures, reused by Phase 4, 2026-06-11): both slow forms are captured in history. The node-grouped form's SQL is a raw row pull, a `SELECT` of the node's columns joined to the links table with the time filter and no GROUP BY, followed by one `SELECT current_timezone()` round trip per pulled row. The count-distinct form's SQL is also a raw join pull, every column of both endpoint nodes, no `DISTINCT` and no `GROUP BY`.
  - Answer: the second possibility is the real one. The warehouse does not receive a different aggregate query on the slow path; it receives no aggregation at all. The engine pulls raw rows and aggregates itself, and on the node-grouped form the dominant cost is not even the pull but the per-row timezone round trip that follows it. The warehouse side of the slow path finishes in under a second either way.
- **Do amount-range filters push down like time filters do?** Only the timestamp filter has been captured in SQL. The structuring query filters on an amount range and is assumed to push down; one capture would close that.
  - Test result (Phase 4, 2026-06-11): the Pattern 1 structuring query returned 196 rows in 2.0 seconds, equal to the full group count in the source table (196 distinct senders over 200 raw rows in the 9000 to 10000 band). The history row shows both halves of the range in the pushed SQL, `WHERE (t.amount >= ?) AND (t.amount < ?)`, with `GROUP BY account_id`, producing exactly 196 rows in 1.2 seconds.
  - Answer: yes. The amount-range filter pushes down exactly like the time filter, as bound parameters in the SQL `WHERE` clause, and the grouping pushed down with it, so the warehouse shipped only the 196 final groups. The doc's assumption is now a verified claim.
- **Could node grouping be rewritten as primary-key grouping?** This is a question for the original developer: could the translation engine turn a group-by-node into a group-by-primary-key downstream, so it pushes down instead of materializing? We expect the current engine does not do this, and a negative result is the expected outcome, recorded as-is. Two things are worth capturing: whether the node-grouped and key-grouped forms of the same query return identical results on a small window, which is the evidence that such a rewrite would be safe, and what the node-grouped form actually sends to the warehouse today, a different SQL shape or a raw row pull with the aggregation happening in the graph engine.
  - Test result (Phase 5, 2026-06-11): the node-grouped form sends a raw row pull, a `SELECT` of the node's columns joined to the links table with the time filter and no GROUP BY, 3,331 rows in 738 ms. The engine then issued one `SELECT current_timezone()` warehouse round trip per row, about 10 minutes' worth, because the pulled rows carry the temporal `opened_date` property. The client connection died to a network drop before the result returned. The key-grouped form of the same query pushed down (`GROUP BY account_id`) and finished in 1.6 seconds with 2,455 rows.
  - Test result (equivalence rerun, 6-hour window, 2026-06-11): both forms returned the same 764 accounts with identical transfer counts and identical outflows to the cent. Three of 764 rows differ only in floating-point dust past two decimals (for example 110.83000000000001 versus 110.83), the expected artifact of summing in a different order. Key-grouped: 2.7 seconds. Node-grouped: 141 seconds, which is the 839 raw rows at the ~6-per-second timezone-storm rate.
  - Answer: confirmed on both halves. No rewrite happens today; the node-grouped form materializes via a raw pull and the engine aggregates, with a per-row timezone round trip as the dominant cost when temporal properties ride along. And the results are identical, so a group-by-node to group-by-primary-key rewrite would be safe; the ~50x gap on the 6-hour window is pure translation, not semantics.
- **Should count-distinct on a scalar key push down?** Also a developer question: a count-distinct over a plain key column ought to translate to the equivalent SQL count-distinct, and it is unclear what generally prevents that. Today the doc reports it running past five minutes with no result. We expect it still does not push down; the test records the evidence either way by capturing whatever SQL the engine emitted for it.
  - Test result (Phase 5, 2026-06-11): on the 1-day window the count-distinct form finished in 4.6 seconds with 2,455 rows, no error. The doc's never-finishes report came from the 7-day window, so window size matters.
  - Test result (history pull, 2026-06-11): the generated SQL contains no `DISTINCT` and no `GROUP BY` at all. The warehouse received a raw join pull, every column of both endpoint accounts joined through `account_links` with only the time filter pushed down as a parameter, producing 3,331 rows in 901 ms. The engine deduplicated and counted itself. Notably the pull includes the nodes' temporal `opened_date` columns yet there was no per-row timezone storm, so the storm trigger is the engine building node values, not temporal columns merely arriving in the pull.
  - Answer: no, it does not push down, even with a clean scalar group key. The `DISTINCT` makes the whole aggregation materialize: raw join pull, engine-side dedup and count. On a small window that is fast (4.6 seconds); the cost grows with the window, which is why the 7-day form ran past five minutes. The doc's claim should be restated: count-distinct always materializes, and whether it finishes is a matter of window size, not of luck.
- **Does a limit on an unanchored join push down?** Another developer point: even on an unbounded traversal, the join with a limit of 25 ought to be pushed down so the warehouse generates and returns only about 25 rows, not a massive pull. The doc currently says an unanchored traversal scans the full relationship table and is slow, which implies the limit is not reaching the join. If the developer is right, the slow behavior is a missed pushdown rather than an inherent cost, and Pattern 5's anchor-everything rule is a workaround for it. The test records which one is true.
  - Test result (Phase 7, 2026-06-11): the unanchored single-hop with limit 25 returned in 5.9 seconds, the unanchored two-hop in 1.5 seconds, and a four-hop escalation in 14.2 seconds, each with exactly 25 rows. History shows `LIMIT ?` in the generated SQL of all three, each producing exactly 25 rows on the warehouse (867 ms, 1.2 s, and 13.5 s respectively). The two-hop SQL also carries a pushed relationship-uniqueness predicate comparing the two edges.
  - Answer: the developer is right. The limit pushes into the SQL on unanchored traversals, so the warehouse returns 25 rows, not a massive pull. The caveat is that the limit bounds the output and not the join work: warehouse time grows with depth, 0.9 to 1.2 to 13.5 seconds from one to four hops. So Pattern 5's anchor-everything rule is overcautious for limit-bounded visualization queries; the doc's did-not-finish reports came from unbounded shapes without a limit. An anchor is still what cuts the join work itself.
- **Does counting two labels work with union all?** The coverage table says counting accounts and merchants in one statement fails with unsupported syntax when written as two matches chained together, and the workaround is to run each label count as its own statement. Untested: writing it instead as two single-label counts combined with union all. If that runs, it is a one-statement workaround worth adding to the doc. Note the doc already recommends union for the cycles workaround without ever having verified that union works on the Virtual Graph, so this test also closes that gap.
  - Test result (Phase 6, 2026-06-11): the chained form failed at parse time with the documented error, `42NG0: Unsupported syntax`, pointing at the `WITH count(a) AS accounts` line. The union-all form ran in 1.5 seconds and returned both rows correctly, 25,000 accounts and 7,500 merchants, matching direct counts on the source tables. History shows the engine sent two separate statements to the warehouse, one count per label, and concatenated the results itself.
  - Answer: yes. Union all is a working one-statement workaround for the two-label count, and this is also the first direct proof that union works on the Virtual Graph at all, which the cycles recipe was leaning on unverified. Under the hood it is not one pushed query: each union branch becomes its own warehouse statement.
- **What exactly triggers the per-row timezone round trip?** Phase 5 concluded the storm fires when the engine builds node values carrying a temporal property. Phase 7 broke that rule: the two-hop and four-hop runs built node values with `opened_date` and paid zero calls, seconds after the single-hop paid one call per row. The runs that stormed all materialized a TIMESTAMP value (`txn_timestamp` or `transfer_timestamp`); the quiet ones built only DATE and scalar values. So the leading hypothesis is that the round trip is per TIMESTAMP value built, because a timestamp needs a timezone to become a Cypher datetime and a date does not. The rival explanation is a session-level cache that the earlier runs happened to miss. Phase 9 separates them.
  - Test result (Phase 9, 2026-06-11): the free-evidence check first: P5-2's full select list, pulled from history, includes `t.transfer_timestamp`, so its 3,331 calls were one per timestamp-bearing row, consistent with the type hypothesis. Then all five discriminating runs hit their committed predictions exactly: returning the relationship paid 25 calls, returning non-temporal scalars paid 0, returning the bare `t.transfer_timestamp` paid 25, returning account nodes whose only temporal is the DATE `opened_date` paid 0, and the two back-to-back reruns paid 25 each (50 total, no cache).
  - Answer: the round trip happens once per TIMESTAMP value the engine materializes into a Cypher datetime, wherever that value sits: in a relationship, in a node, or as a bare projected column. DATE values never trigger it, which is why account nodes are free and why the old node-value rule misfired. There is no caching; every run pays per value, every time. This explains every observation across Phases 3, 5, and 7 with no exceptions, including why the node-grouped aggregation stormed (its raw pull carried `transfer_timestamp` per row) while the count-distinct pull did not (account columns only). The doc guidance this implies: bulk-returning or bulk-materializing anything that carries a TIMESTAMP costs about 0.1 to 0.2 seconds per row in warehouse round trips; project scalars instead, or accept it only at visualization row counts.

## Phased verification plan

Run every phase one query at a time, because the connection pool holds only ten connections and a slow query keeps its connection until Databricks finishes. All time windows anchor to the data's last day, which is March 30, 2024, not to today.

Every run in every phase carries the same bail-out rule: a five-minute client timeout. If a query has not returned in five minutes, record it as did-not-finish with the elapsed time and move on. The bail-out does not stop the warehouse query, a client-side cancel never does, so the abandoned run holds its pool connection until Databricks finishes on its own; running one query at a time leaves the other nine connections free, and if the pool ever saturates anyway, restart the instance before continuing. A did-not-finish at five minutes is itself a usable result, and the generated SQL still lands in query history for the later pull.

**Phase 1: top-N and order-by pushdown.** Status: **Complete**, run 2026-06-10. The base query is the existing seven-day fan-out pair query from the perf tests, the one known to return about 22,000 rows in about three seconds. Existing query, four trivial new variants. The variants separate the moving parts: if order-by plus limit fails to push down, the limit-only and order-by-only runs show which clause was the blocker, and the two order-by runs show whether sorting on an aggregate alias translates differently from sorting on a plain projected column.

- Run the query as-is, the control, and record wall-clock and row count.
- Run it with only a limit of ten and no sort. A bare limit is a plain row cut rather than a top-N, the case where pushdown is most plausible, and it isolates the limit from the sort.
- Run it with only an order-by on the aggregate alias, the pair outflow descending. This is the case the shared-shape sentence is actually about: ordering by a value the aggregation produced, the shape every fraud query wants.
- Run it with only an order-by on a plain projected column, the sender key, as a comparison. Sorting on a projected column may translate differently from sorting on an aggregate alias, and if one pushes down while the other does not, that distinction belongs in the doc.
- Run it with the aggregate-alias order-by plus a limit of ten, the real top-N. If the limit pushes down, this should come back in about a second because only ten rows cross the wire; if it comes back in three seconds, the rows all shipped and were cut client-side. A fast return on its own proves nothing, though, because a lazy fetch that closes the cursor after ten rows is also fast; the verdict comes from the history pull, not the stopwatch.
- After the history lag, read the SQL text and the produced row count for all five runs and record which clauses reached the warehouse.
- Contingency: if a limit-carrying run's SQL has no limit and its wall-clock is ambiguous, too fast for a full ship but too slow for a clean lazy-fetch verdict, rerun the control and that variant on the 30-day window. There the full ship takes about eleven seconds and a lazy fetch stays around one second, a gap too wide to mistake. The 7-day gap is only about one second versus three, which session noise can blur, and this is the one verdict that leans on the stopwatch: query history has no per-statement bytes-shipped column, so when the SQL carries no limit, wall-clock is the only signal separating ship-and-trim from a lazy fetch.

### Phase 1 results (2026-06-10)

What happened, in brief: all five variants ran without errors on a warm 2X-Small warehouse. Sorting works, on both an aggregate alias and a plain column. Adding a limit made the query come back in under a second with exactly the limited rows.

- **Control (no sort, no limit):** 7.6 seconds, 22,096 rows. Matches the known baseline row count. The extra time over the usual ~3 seconds was first-query session setup.
- **Limit 10 only:** 0.6 seconds, 10 rows.
- **Order by the aggregate alias (pair outflow, descending):** 3.4 seconds, all 22,096 rows, correctly sorted. No error, and no slower than the unsorted control.
- **Order by a plain projected column (sender):** 3.3 seconds, all 22,096 rows, correctly sorted. Behaves the same as sorting on the aggregate alias.
- **Top-N (order by pair outflow descending, limit 10):** 0.7 seconds, 10 rows, and the first row matched the full sort's first row.
- **Warehouse SQL and produced row counts (history, pulled 2026-06-11):** all five runs generated **identical SQL**: the aggregation with `GROUP BY sender, recipient` and the timestamp filter as a parameter, with **no `ORDER BY` and no `LIMIT` in any of them**. Every run produced the full 22,096 rows on the warehouse (total duration 3.6s cold, then 190 to 260 ms warm; warm warehouse execution was 3 to 5 ms because the result cache served the identical statement).

Answer summaries for the questions this phase covered:

- **Does a Cypher limit push down?** No, not on an aggregation. The SQL never contains a limit and the warehouse produces the full group set every time. The trim happens in the graph engine. The top-N is still fast (0.7s) because the full set crosses only the fast Databricks-to-Aura leg; only the 10 trimmed rows cross the slower leg to the client. So the cost of a limit-less result is shipping to the client, and a Cypher limit removes that cost even though the warehouse work is unchanged.
- **Does a server-side order-by push down or add a post-processing step?** It does not push down: no generated SQL contained an `ORDER BY`. The sort is a post-processing step in the graph engine. At this scale it is free, since sorted full-result runs took the same ~3.4 seconds as unsorted ones, so Pattern 3's client-side-sort advice and the shared-shape bullet's order-on-the-server advice produce the same warehouse work; the only difference is whether the engine or the application sorts.

**Phase 2: threshold behavior.** Status: **Complete**, run 2026-06-10. The key here is isolating the claim: the textbook fan-in query from the appendix contains three potentially unsupported constructs at once, the relative time window, the count-distinct, and the post-aggregation threshold, so its error alone cannot say which construct tripped it. The citable reproduction is the working query with only the threshold added back. One trivial new variant; the other two queries exist in the doc.

- Run the working adapted fan-in form with only the post-aggregation threshold added back, nothing else changed, and log the exact error. This is the isolated reproduction that makes the HAVING claim citable.
- Run the full textbook form from the appendix as well and log its error, to document that the textbook query fails as written, without attributing the failure to any one construct.
- Run the adapted working form and, after the lag, confirm its produced row count equals the full group count. The working form contains no threshold at all, so this is not a pushdown check; it confirms the cost claim, that moving the threshold client-side means the warehouse produces and ships the full group set.

### Phase 2 results (2026-06-10)

What happened, in brief: the HAVING-style failure is now reproduced in isolation and logged, the textbook form fails the same way, and the working form provably returns the full group set, confirmed against the source table rather than waiting on query history.

- **Isolated threshold repro:** the working fan-in form with only `WHERE transfers >= 5` added after the aggregating `WITH` fails instantly with `Neo.ClientError.Statement.SyntaxError`, GQL status `42NG0: Unsupported syntax`, pointing at the aggregating `WITH` line (line 3, column 1). It fails at parse time, in under a second, so nothing reaches the warehouse. This is the citable reproduction.
- **Textbook form as written:** fails with the identical error at the identical position, also at parse time. This documents that the appendix query fails as written, without blaming any single construct.
- **Working form, no threshold:** runs in 2.1 seconds and returns 9,847 rows.
- **Full-group-set confirmation:** a direct SQL count on the source table shows exactly 9,847 distinct recipients in the 7-day window, so the client received every group. Only 1,157 of those groups meet the `transfers >= 5` threshold, so the client-side threshold throws away about 88 percent of the shipped rows. This confirms the cost claim with a stronger check than the history pull: the client row count equals the true group count from the source table.

Answer summaries for the questions this phase covered:

- **Is the HAVING-style failure reproducible and logged?** Yes. The exact error is `Neo.ClientError.Statement.SyntaxError` with GQL status `42NG0: Unsupported syntax`, raised at parse time against the aggregating `WITH` clause when a post-aggregation `WHERE` follows it.
- **Do the working demo queries really return the full ungrouped result?** Yes. The working fan-in form returned 9,847 rows, which equals the count of distinct recipients in the source table for the same window. The threshold never ran on the server, and applying it client-side discards 8,690 of the 9,847 shipped rows.

**Phase 3: anchored traversal.** Status: **Complete**, run 2026-06-10. Existing query, no changes.

- Run the Pattern 5 anchored visualization query, the single-account merchant traversal ending in a limit of 25, straight from the existing demo docs.
- After the lag, check whether the account-id anchor shows up as a filter in the pushed SQL and whether the limit made it down.

### Phase 3 results (2026-06-10)

What happened, in brief: the anchored traversal came back in 3.5 seconds with exactly the anchored account's full edge set, and the history pull confirmed both the anchor and the limit reached the warehouse SQL.

- **Anchored traversal (account 184, limit 25):** 3.5 seconds, 8 rows, full node and relationship data for drawing.
- **Anchor confirmation:** the source table has exactly 8 transactions for account 184, to 8 distinct merchants, matching the 8 rows returned. The client received precisely that account's edges and nothing else.
- **History confirmation (pulled 2026-06-11):** the generated SQL joins accounts to merchants and ends with `WHERE (a.account_id = ?) LIMIT ?`, and the statement produced 8 rows in 1.4 seconds. Both the anchor and the limit are in the SQL.

Answer summary for the question this phase covered:

- **Does the limit on anchored visualization queries push down?** Yes, both halves. The anchor becomes a parameterized SQL filter and the limit becomes a SQL `LIMIT`. Note the contrast with Phase 1: on an aggregation the limit stays engine-side, on a plain traversal it pushes down.

**Phase 4: capture the slow path.** Status: **Complete**, run 2026-06-11. The goal is to see what the warehouse receives on the slow path: a different SQL shape, or a raw row pull with the aggregation happening in the graph engine. Existing queries from the doc; pick the smallest windows that still demonstrate the behavior, and run these after the fast phases since they may be slow and hold a pool connection.

- Run the fast scalar-grouped structuring query, the Pattern 1 example with the amount-range filter, and capture its generated SQL. This is the explicit answer to the amount-range question: the filter should appear in the pushed SQL the same way the time filter does. This query is fast and existing, so it runs first in the phase.
- Run one node-grouped query from the anti-patterns section, the short-window version so it finishes, and capture its generated SQL from history.
- Run one count-distinct query from the anti-patterns section the same way and capture its generated SQL.

### Phase 4 results (2026-06-11)

What happened, in brief: only the structuring query needed a new run, since Phase 5 had already captured the node-grouped and count-distinct SQL. The structuring query's history row settles the amount-range question: the range filter pushes down as bound parameters, the grouping pushes down with it, and the warehouse ships only the final groups.

- **Structuring query (Pattern 1, amount range 9000 to 10000):** 2.0 seconds, 196 rows. The source table confirms 196 distinct senders over 200 raw rows in that band, so the client received the full group set.
- **History confirmation:** the generated SQL joins accounts to `account_links`, filters with `WHERE (t.amount >= ?) AND (t.amount < ?)`, and groups with `GROUP BY account_id`, producing exactly 196 rows in 1.2 seconds. Two details match the earlier phases: the Cypher `ORDER BY near_threshold DESC` is absent from the SQL (the engine sorts, as in Phase 1), and the `round(..., 2)` is also absent (the warehouse returned the raw sum and the engine rounded).
- **Node-grouped capture:** reused from Phase 5. Raw row pull with no GROUP BY, then one `SELECT current_timezone()` round trip per row; the warehouse part finished in 738 ms.
- **Count-distinct capture:** reused from Phase 5. Raw join pull with no DISTINCT and no GROUP BY; the engine deduplicated and counted.

Answer summaries for the questions this phase covered:

- **What does the slow path actually look like in SQL?** A raw row pull with the aggregation happening in the graph engine. The warehouse never receives a different aggregate query; it receives no aggregation at all, and on the node-grouped form the real cost is the per-row timezone round trip after the pull, not the pull itself.
- **Do amount-range filters push down like time filters do?** Yes, identically: bound parameters in the SQL `WHERE` clause, with the grouping pushed down alongside, so only the 196 final groups shipped.

**Phase 5: answer the two developer questions.** Status: **Complete**, run 2026-06-11; the 1-day node-grouped run did not finish client-side, and a 6-hour-window rerun closed the equivalence check. Both are expected to fail on the current engine, and that is the result to record: these were questions for the original developer, and the captured SQL and timings are the artifact to hand back, whichever way they land. Same method as Phase 4: small window, one query at a time, read the generated SQL from history afterward.

- For the grouping question, run the key-grouped form of the aggregation over the data's last one day and record its results and timing.
- Run the node-grouped form of the same aggregation over the same one-day window. The full-window node-grouped form took about sixteen minutes in earlier testing, so the one-day window keeps the runtime sane and the connection pool free.
- Compare the two result sets for equivalence, and capture both generated SQL texts from history to show what the warehouse received in each case. Matching results plus a materializing node form is the evidence that the rewrite would be safe but is not happening today.
- For the count-distinct question, run the count-distinct fan-in form over the same one-day window with a clean scalar group key, and capture its timing and whatever SQL shows up in history. The standard five-minute bail-out applies: record a did-not-finish and move on, and the SQL it emitted still shows up in the history pull, which is the evidence the question needs.

Both forms already exist in the best-practices doc; only the one-day-window variants need to be created. Phase 4's captures may already answer part of this; if so, reuse them and only run what is missing.

### Phase 5 results (2026-06-11)

What happened, in brief: the key-grouped form pushed down and finished in seconds. The node-grouped form's generated SQL is captured and it is the smoking gun: a raw row pull with no GROUP BY, followed by one `SELECT current_timezone()` warehouse round trip per returned row. The client never received the 1-day node-grouped result because the local network dropped during the wait; a 6-hour-window rerun completed and proved the two forms return identical results. The count-distinct form, unexpectedly, finished fast on the 1-day window.

- **Key-grouped form (1-day window):** 1.6 seconds, 2,455 rows. History shows clean pushdown: `GROUP BY account_id` with the time filter as a parameter, 947 ms, 2,455 produced rows.
- **Node-grouped form (same window):** did not finish client-side. History shows what the warehouse received: a `SELECT` of the source columns (including `opened_date`) joined to `account_links` with the time filter and **no GROUP BY**, finishing in 738 ms with 3,331 raw rows, one per transfer in the window. The engine planned to aggregate those rows itself.
- **The per-row storm:** after the raw pull, the engine issued 3,339 separate `SELECT current_timezone()` statements at about 5.5 per second over roughly 10 minutes, exactly one per raw row (3,331) plus one per row of the Phase 3 traversal (8). Both queries return rows carrying a temporal property (`opened_date`), which is what triggers the per-row round trip. This, not the warehouse, is the slow path: the warehouse finished in under a second.
- **Why the client got nothing:** the local network dropped mid-wait (DNS failures in the driver log), the Bolt connection went defunct, and the driver's retries all failed. The engine's statement storm ended at 00:10:42; the pool recovered, and `RETURN 1` works again.
- **Result equivalence (key versus node grouped):** settled by a 6-hour-window rerun (839 raw rows, so the per-row storm fits inside the 5-minute bail-out). Both forms returned the same 764 accounts with identical transfer counts and identical outflows to the cent; 3 of 764 rows differ only in floating-point representation past two decimals, from summing in a different order. Key-grouped took 2.7 seconds, node-grouped 141 seconds for the same answer.
- **Count-distinct form (1-day window, scalar group key):** finished in 4.6 seconds with 2,455 rows, against the doc's report of running past 5 minutes on the 7-day window. Its history row settles the SQL verdict: materialized. The warehouse received a raw join pull, all columns of both endpoint accounts with only the time filter pushed down, no `DISTINCT`, no `GROUP BY`, 3,331 rows in 901 ms; the engine deduplicated and counted. The pull carries the temporal `opened_date` columns yet no timezone storm followed, so the storm trigger is the engine building node values, not temporal columns in the pull.

Answer summaries for the developer questions this phase covered:

- **Could node grouping be rewritten as primary-key grouping?** Both halves of the evidence are now in hand. What happens today: the node-grouped form sends a raw row pull with no GROUP BY, and the engine aggregates client-side, paying one timezone round trip per row when a temporal property rides along; the key-grouped form of the same query is a one-second pushdown. Would the rewrite be safe: yes; on the 6-hour rerun the two forms returned identical results (764 rows, same counts, outflows matching to the cent, only floating-point dust differing), with the node form taking ~50x longer for the same answer.
- **Should count-distinct on a scalar key push down?** Fully answered: it does not. The generated SQL is a raw join pull with no `DISTINCT` and no `GROUP BY`; the engine deduplicates and counts itself. On the 1-day window that completes in 4.6 seconds, so the doc's blanket it-never-finishes claim is too strong; count-distinct always materializes, and window size decides whether the materialize is tolerable.

**Phase 6: union all for two-label counts.** Status: **Complete**, run 2026-06-11. These queries are cheap and fast, so this phase can run any time; both are new but trivial, since the failing form is already written out in the coverage table.

- Reproduce the documented failure: run the chained form that matches accounts, takes a count, then matches merchants, and log its exact error.
- Run the union-all form, two single-label count statements combined into one query, and record whether it runs and what it returns.
- After the lag, check what SQL lands in history, including whether it arrives as one statement or two.
- Record the verdict: if it works, it becomes a documented one-statement workaround; if it fails, the coverage table gains a note that union all does not rescue this case. The cycles recipe that leans on union gets its caveat resolved either way.

### Phase 6 results (2026-06-11)

What happened, in brief: the chained form fails exactly as documented, and the union-all form works, returning both label counts correctly in one Cypher statement. History shows it is two pushed statements under the hood.

- **Chained form:** failed at parse time, under a second, with `Neo.ClientError.Statement.SyntaxError`, GQL status `42NG0: Unsupported syntax`, pointing at the `WITH count(a) AS accounts` line. Nothing reached the warehouse. The documented claim is now a logged reproduction.
- **Union-all form:** 1.5 seconds, 2 rows: accounts 25,000 and merchants 7,500. Both match direct `count(*)` checks on the source tables exactly.
- **History confirmation:** the warehouse received two separate statements one second apart, `SELECT ? AS label, count(account_id) FROM accounts GROUP BY label` and the merchant equivalent, each producing 1 row (549 and 407 ms). The engine ran the branches as independent pushed queries and concatenated the results itself.

Answer summary for the question this phase covered:

- **Does counting two labels work with union all?** Yes. It is a working one-statement workaround for the chained-form failure, and the first direct proof that union runs on the Virtual Graph at all. Each branch becomes its own warehouse statement, so the cost is the same as running the two counts separately; the convenience is client-side.

**Phase 7: limit on an unanchored join.** This tests the developer's expectation that a limit should be pushed into the join itself, so even an unbounded traversal returns only about 25 rows. It complements Phase 1, which tests the limit on an aggregation, and Phase 3, which tests it on an anchored traversal; this is the unanchored, no-aggregation case. The queries are trivial new variants: Pattern 5's traversal with the anchor removed. Run this phase late, since the doc predicts the unanchored forms are slow and a slow run holds a pool connection.

- Run the anchored Pattern 5 traversal with its limit of 25 as the fast control, if Phase 3 has not already recorded it.
- Run the same single-hop traversal with the anchor removed, keeping the limit of 25, and record wall-clock and row count. If the limit pushes into the join, this should come back fast with about 25 rows; if the doc's warning holds, it will be slow because the full join ran first.
- If the single-hop form comes back fast, also run the unanchored two-hop form with the limit, the shape the doc says did not finish within 100 seconds, to see whether the pushdown holds as the join deepens. Skip this if the single-hop form was already slow. The standard five-minute bail-out applies here too.
- After the lag, read the generated SQL for each run: whether a limit clause reached the warehouse, where it sat relative to the join, and how many rows the statement produced.

### Phase 7 results (2026-06-11)

What happened, in brief: the limit pushes into the SQL on unanchored traversals at every depth tried. All three runs, single-hop, two-hop, and a four-hop escalation added during the session, returned exactly 25 rows fast, and all three generated SQLs end with `LIMIT ?`. The limit bounds the output, not the join work, so warehouse time still grows with depth.

- **Unanchored single-hop, limit 25:** 5.9 seconds client-side, 25 rows. History: `LIMIT ?` in the SQL, 25 produced rows, 867 ms, followed by exactly 25 `SELECT current_timezone()` calls, one per returned row, which is most of the client wall-clock.
- **Unanchored two-hop, limit 25:** 1.5 seconds, 25 rows. History: `LIMIT ?` in the SQL, 25 produced rows, 1.2 seconds. The full SQL is captured in the work log; it also pushes a relationship-uniqueness predicate, `NOT (t2.src_account_id = t1.src_account_id AND t2.dst_account_id = t1.dst_account_id AND t2.link_id = t1.link_id)`, so Cypher's distinct-relationships rule travels to the warehouse too.
- **Unanchored four-hop, limit 25 (escalation probe):** 14.2 seconds, 25 rows. History: `LIMIT ?` in the SQL, 25 produced rows, 13.5 seconds of warehouse time. This is the depth check: the limit holds at depth four, but the five-way self-join behind it is real work the limit does not remove.
- **Timezone-cache observation:** the two-hop and four-hop runs returned node values carrying `opened_date` yet triggered zero timezone calls, seconds after the single-hop paid one call per row. So the per-row storm is not deterministic per node value; the engine evidently reused the answer within the session here, while the Phase 5 runs stormed thousands of times. Unresolved nuance, recorded as observed.

Answer summary for the question this phase covered:

- **Does a limit on an unanchored join push down?** Yes, at every depth tried, producing exactly the limited rows on the warehouse. The doc's slow unanchored reports came from unbounded shapes; with a limit, an unanchored visualization query is bounded and tolerably fast. The remaining truth in the anchor rule is that only an anchor cuts the join work itself, which is what grew to 13.5 seconds at depth four even with the limit pushed.

**Phase 8: fix the doc.** Status: **Complete**. No queries; writing only. The findings were folded back into the best-practices guide as follows:

- Split the shared-shape bullet so row-level filters are clearly server-side.
- Resolve the order-on-the-server contradiction with Pattern 3 in whichever direction the evidence points.
- State plainly what a limit does, on aggregations, on anchored traversals, and on unanchored joins. Name the mechanism the evidence showed, not just the speedup, because the two measured mechanisms mean different guidance. On traversals, anchored or unanchored at every depth tried, the limit pushes into the SQL and bounds what the warehouse produces, though not the join work behind it, which still grew to 13.5 seconds at four hops. On aggregations the limit never reaches the SQL and the warehouse produces the full group set, but the engine trims before the slow engine-to-client leg, so the top-N still came back in 0.7 seconds instead of 3.4. Either way the conclusion is the same: write the order-by and the limit in Cypher. What a limit cannot do is reduce warehouse work; only a filter or an anchor does that. This reverses Pattern 3's keep-them-off-the-server advice, which the evidence showed buys nothing and costs shipping.
- Soften Pattern 5's anchor-everything rule: Phase 7 settled the conditional in the developer's favor. The limit pushes into the SQL on unanchored traversals at every depth tried, so a limit-bounded visualization query is acceptable without an anchor. Keep the part of the rule that survives: the limit bounds the output, not the join work behind it, which grew to 13.5 seconds at four hops, so an anchor is still what makes a deep traversal cheap.
- Add the per-TIMESTAMP timezone round trip as its own pattern (Phase 9, the biggest slow path found in this verification). The engine issues one `SELECT current_timezone()` warehouse round trip per TIMESTAMP value it materializes into a Cypher datetime, about 0.1 to 0.2 seconds per value, with no caching; DATE values never trigger it. This is the real mechanism behind Pattern 1's slow node-grouped timing, so rewrite that explanation: the materialize cost is the per-row round trips that follow the raw pull (738 ms of warehouse work versus 10 minutes of round trips in Phase 5), not the pull itself. Guidance: in bulk results, project scalars instead of returning nodes or relationships that carry a timestamp, or accept the cost only at visualization row counts.
- Restate the count-distinct claim in Pattern 2, the coverage table, and the timing table. Phase 5 showed `count(DISTINCT ...)` always materializes, even with a clean scalar group key: the generated SQL is a raw join pull with no `DISTINCT` and no `GROUP BY`, and the engine deduplicates itself. Window size decides whether that is tolerable, 4.6 seconds on a 1-day window versus past five minutes on 7 days, so the coverage table's "supported but rarely pushes down" and the timing table's did-not-finish entry should both say: never pushes down, cost scales with the window.
- Add the logged error reproductions to the coverage table, including the union-all verdict (a working one-statement workaround; under the hood each branch is its own warehouse statement) and the two developer-question results (no node-to-key rewrite happens today but it would be safe, since both forms returned identical results; count-distinct materializes as above).

**Phase 9: pin down the timezone round-trip trigger.** Status: **Complete**, run 2026-06-11.

TLDR for this phase: the per-row `SELECT current_timezone()` storm is the single biggest slow path found in this whole verification, it turned a sub-second warehouse query into 10 minutes in Phase 5, and we still cannot predict when it fires. Phase 5 said it fires when the engine builds node values carrying a temporal property. Phase 7 contradicted that: two runs built exactly such node values and paid nothing. What we want to figure out is the precise trigger, because that decides what guidance the doc gives. If the trigger is per TIMESTAMP value built, the leading hypothesis, then the fix is to avoid returning or grouping by things that carry timestamps and the doc can say which property types are safe (`opened_date`, a DATE, costs nothing; `txn_timestamp` and `transfer_timestamp`, TIMESTAMPs, cost one round trip per value). If it is instead a session cache that sometimes saves you, the behavior is unpredictable and the doc has to warn about the worst case everywhere. The evidence so far fits the type hypothesis perfectly: every storming run materialized TIMESTAMP values (Phase 3 and P7-1 returned relationships carrying `txn_timestamp`, one call per row; P5-2's raw pull is suspected to carry `transfer_timestamp`, one call per row) and every quiet run built only DATE node values or scalars (P7-2, P7-3, P5-3).

The plan, two steps:

- Free evidence first: pull P5-2's full select list from history. If its raw pull includes `transfer_timestamp`, the type hypothesis explains every observation made so far with no exceptions; if it does not, the type hypothesis is dead on arrival and the cache idea takes over.
- Then five cheap discriminating runs, all `LIMIT 25`, each isolating one variable, with predictions committed before the history pull. One call per returned row means 25 calls in the run's window; the alternative is zero.
  - P9-A `RETURN t` over TRANSFERRED_TO: a relationship value carrying a TIMESTAMP. Type hypothesis predicts 25.
  - P9-B `RETURN t.amount, t.link_id`: scalars, no temporal anywhere. Predicts 0.
  - P9-C `RETURN t.transfer_timestamp`: a bare TIMESTAMP scalar, no node or relationship value built. The sharpest cell: 25 means the trigger is the timestamp value itself wherever it appears; 0 means it only fires inside node or relationship construction.
  - P9-D `MATCH (a:Account) RETURN a`: a node value whose only temporal is a DATE. Type hypothesis predicts 0; the old Phase 5 rule predicts 25, so this run retests it head on.
  - P9-E: rerun P9-A twice back to back. Two times 25 means no session cache and the type hypothesis stands alone; 0 on the second run means caching is real and both ideas are needed.
- After the lag, count the `current_timezone` statements in each run's time window and compare against the committed predictions.

### Phase 9 results (2026-06-11)

What happened, in brief: a clean sweep. The free-evidence check and all five runs landed exactly on the type hypothesis's predictions, and the cache idea is dead. The trigger is one warehouse round trip per TIMESTAMP value the engine materializes into a Cypher datetime; DATE values are free; nothing is cached.

- **Free evidence:** P5-2's full select list, pulled from history, includes `t.transfer_timestamp` along with all the relationship's columns. So Phase 5's 3,331-call storm was one call per timestamp-bearing raw row, exactly what the type hypothesis requires.
- **P9-A (return the relationship):** 5.2 seconds client-side; history shows exactly 25 timezone calls. A relationship value carrying `transfer_timestamp` pays per row.
- **P9-B (return non-temporal scalars):** 0.8 seconds; 0 calls. Same match, same rows, no timestamp materialized, no storm.
- **P9-C (return the bare timestamp):** 4.8 seconds; 25 calls. The sharpest cell: no node or relationship value was built at all, and it still paid per row. The trigger is the timestamp value itself, not the container.
- **P9-D (return account nodes, DATE only):** 1.1 seconds; 0 calls. This is the head-on retest of the old Phase 5 rule, which predicted 25 here. Node values are innocent; their DATE property never needed a timezone.
- **P9-E (rerun A twice back to back):** 5.2 and 5.0 seconds; 50 calls total across the two windows (the 24/26 split in the per-window count is one call straddling the window boundary). No session cache; every run pays full price every time.
- The per-call cost works out to about 0.1 to 0.2 seconds, matching the ~5.5 to 5.9 calls per second measured in the Phase 5 and equivalence-rerun storms.

Answer summary for the question this phase covered:

- **What exactly triggers the per-row timezone round trip?** One `SELECT current_timezone()` per TIMESTAMP value materialized into a Cypher datetime, wherever it sits. DATE values never trigger it. No caching. The rule explains every storm and every quiet run in Phases 3, 5, and 7 with no exceptions, and replaces Phase 5's node-value rule, which was a correlation: the node-grouped pull stormed because it dragged `transfer_timestamp` along, not because it built node values.

## Existing queries versus new ones

Nearly everything needed already exists.

- **Existing, used as-is:** the fan-out pair query, the fan-in pair and textbook forms, the anchored traversal, the fast scalar-grouped structuring query, and the slow anti-pattern forms are all written out in the best-practices guide, the perf test plan, or the demo files.
- **Existing, for the developer questions:** the node-grouped aggregation is Pattern 1's slow example, and the count-distinct form is the one Pattern 2 reports timing out.
- **Existing, for the union-all test:** the failing two-label form is written out in the coverage table.
- **New, Phase 1:** four versions of the fan-out query, one with only a limit, one ordering by the aggregate alias, one ordering by a plain projected column, and one with the aggregate-alias order-by plus the limit.
- **New, Phase 2:** the working fan-in form with only the post-aggregation threshold added back, the isolated HAVING reproduction.
- **New, Phases 4 and 5:** one-day-window versions of the slow forms.
- **New, Phase 6:** the union-all form, which combines two single-label counts that each already run on their own.
- **New, Phase 7:** unanchored versions of Pattern 5's traversal with the limit kept, single-hop and optionally two-hop.
- **Not needed:** no new tables, mappings, or tooling, and the pending large-table Aura mapping from the perf work is not required for any phase here.

## Work log

### Phase 1, run 2026-06-10

Setup:

- Backing warehouse `vg demo sql warehouse` (`b0fffb8e3255bf85`), serverless 2X-Small, already RUNNING before the first query, so all five runs are warm, not cold.
- Each variant ran once through `uv run vg-probe`, one at a time, between 23:39:14Z and 23:40:11Z.
- Base query: the seven-day fan-out pair query, cutoff `2024-03-23T23:58:00Z`.

Client-side results:

| Run | Variant | Wall-clock | Rows returned |
|-----|---------|------------|---------------|
| P1-1 | Control, no sort, no limit | 7.6s | 22,096 |
| P1-2 | `LIMIT 10` only | 0.6s | 10 |
| P1-3 | `ORDER BY pair_outflow DESC` only | 3.4s | 22,096 |
| P1-4 | `ORDER BY sender` only | 3.3s | 22,096 |
| P1-5 | `ORDER BY pair_outflow DESC LIMIT 10` | 0.7s | 10 |

Notes from the runs:

- Both sorted runs returned correctly ordered rows: P1-3's first row was the largest pair outflow (122,697.95) and P1-4's first row was sender 1.
- P1-5's first row matched P1-3's first row exactly, so the top-N agrees with the full sort.
- The control's 7.6s is slower than its usual ~3s; it was the first query of the session, so the extra time is session setup, not the query.

History pull (pulled 2026-06-11 ~01:35Z; the lag ran closer to 25 minutes than 11):

- All five runs generated byte-identical SQL: the pair aggregation with `GROUP BY sender, recipient` and the cutoff as a bound parameter. No variant's SQL contained `ORDER BY` or `LIMIT`.
- Every run produced 22,096 rows on the warehouse. Durations: 3,556 ms for the cold control, then 233, 205, 259, and 188 ms for the four warm variants.
- Verdict: neither clause reaches the warehouse on an aggregation. The engine sorts and trims after the full group set ships from Databricks. The fast limit runs are explained by the slow leg being engine-to-client: only the trimmed rows cross it.

### Phase 2, run 2026-06-10

Setup:

- Same warehouse and method as Phase 1, runs between 23:50:40Z and 23:51:09Z, one at a time through `uv run vg-probe`.
- Queries: the Pattern 3 working fan-in form (group by `dst.account_id`, cutoff `2024-03-23T23:58:00Z`), the same form with `WHERE transfers >= 5` added back after the `WITH`, and the appendix textbook fan-in as written.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P2-1 | Working form + `WHERE transfers >= 5` after the `WITH` | Failed at parse time, under 1s: `Neo.ClientError.Statement.SyntaxError`, `42NG0: Unsupported syntax. (line 3, column 1)`, pointing at the aggregating `WITH` line |
| P2-2 | Textbook appendix form as written | Failed at parse time with the identical `42NG0` error at the identical position |
| P2-3 | Working form, no threshold | OK, 2.1s, 9,847 rows |

Notes from the runs:

- The exact P2-1 error text: `{neo4j_code: Neo.ClientError.Statement.SyntaxError} {message: 42NG0: Unsupported syntax. (line 3, column 1 (offset: 117)) "WITH dst.account_id AS account_id, count(t) AS transfers, sum(t.amount) AS inflow" ^} {gql_status: 42NG0}`.
- Both failures happen before any SQL is generated, so these statements never appear in warehouse query history. The repro evidence is the client error, not a history row.
- Full-group-set check, done directly against the source table instead of waiting for history: `account_links` has exactly 9,847 distinct `dst_account_id` values in the 7-day window, matching the 9,847 rows P2-3 returned, and only 1,157 of those groups have 5 or more transfers. So the working form ships the full group set and the client-side threshold discards about 88 percent of it.

### Phase 3, run 2026-06-10

Setup:

- Same warehouse and method, run at 23:56:11Z through `uv run vg-probe`.
- Query: the Pattern 5 anchored visualization traversal, `MATCH (a:Account {account_id: 184})-[t:TRANSACTED_WITH]->(m:Merchant) RETURN a, t, m LIMIT 25`, with the doc's example anchor inlined because the probe takes no parameters.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P3-1 | Anchored traversal, account 184, limit 25 | OK, 3.5s, 8 rows with full node and relationship properties |

Notes from the run:

- The source table has exactly 8 transactions for account 184, to 8 distinct merchants. The 8 returned rows are that account's complete edge set, so the anchor restricted the result to the right rows.
- The limit never bound, because 8 is under 25, so the verdict came from the SQL text. The history pull (2026-06-11) shows the generated SQL ends with `WHERE (a.account_id = ?) LIMIT ?` and produced 8 rows in 1,421 ms: both the anchor and the limit pushed down. No rerun on a busier account is needed.
- Side observation, explained fully in the Phase 5 log: this query's 8 returned rows each triggered a separate `SELECT current_timezone()` statement on the warehouse, because the returned nodes carry the temporal `opened_date` property. Eight extra round trips are invisible at visualization scale but the same behavior is what sinks the node-grouped aggregation.

### Phase 5, run 2026-06-11

Setup:

- Same warehouse and method, runs between 00:00:06Z and 01:41:48Z through `uv run vg-probe`, one at a time, 1-day window (cutoff `2024-03-29T23:58:00Z`).
- Queries: the key-grouped fan-out aggregation, the node-grouped form of the same aggregation (Pattern 1's slow shape), and the count-distinct fan-in form with a scalar group key (Pattern 2's shape).

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P5-1 | Key-grouped aggregation | OK, 1.6s, 2,455 rows |
| P5-2 | Node-grouped aggregation | Did not finish client-side: the local network dropped mid-wait, the Bolt connection went defunct, and all driver retries failed with DNS errors until 01:26. The warehouse side finished; see below |
| P5-3 | Count-distinct, scalar group key | OK, 4.6s, 2,455 rows |

Notes from the runs, with the history pull:

- P5-1's generated SQL is a clean pushdown: `GROUP BY account_id` with the time filter as a parameter, 947 ms, 2,455 produced rows.
- P5-2's generated SQL is the slow-path capture the phase wanted: a `SELECT` of the source node's columns (including `opened_date`) joined to `account_links` with the time filter and **no GROUP BY**. It finished in 738 ms and produced 3,331 raw rows, one per transfer in the window. The aggregation was going to happen in the graph engine.
- After that raw pull, the warehouse history shows 3,339 separate `SELECT current_timezone()` statements between 23:56:14 and 00:10:42, exactly one per P5-2 raw row (3,331) plus one per Phase 3 row (8). Rows carrying a temporal property trigger one warehouse round trip each, at about 5.5 per second, roughly 10 minutes for this window. That per-row storm, not warehouse compute, is the materialize slow path.
- Every statement in warehouse history FINISHED; nothing crashed server-side. The client failure was environmental (network drop), not a query error.
- The key-versus-node result-equivalence comparison could not run from these runs, since P5-2 never returned rows. Closed by the 6-hour-window rerun below.
- P5-3's speed is a finding on its own: the doc reports the count-distinct shape running past 5 minutes on the 7-day window, but the 1-day window completes in 4.6 seconds.
- P5-3's history row (pulled 2026-06-11 ~02:10Z via the SQL statements API, since the MCP tool's 60-second cap kept timing out on the history scan): the generated SQL is a raw join pull with no `DISTINCT` and no `GROUP BY`. It selects every column of both endpoint accounts, `accounts AS src JOIN account_links AS t ON ... JOIN accounts AS dst ON ...`, with `WHERE (t.transfer_timestamp >= ?)` as the only filter, 901 ms, 3,331 produced rows. The engine deduplicated and counted client-side. So `count(DISTINCT ...)` materializes even with a clean scalar group key; the only thing that pushed down was the time filter.
- Storm-trigger refinement from the same row: P5-3's pull includes the temporal `opened_date` columns of both nodes, yet history shows zero `SELECT current_timezone()` calls in its window and the query finished in 4.6 seconds. Combined with P5-2 (node group key, storm) and Phase 3 (returns node values, storm), the per-row timezone round trip happens when the engine builds node values carrying a temporal property, not whenever temporal columns arrive in the pull.

### Phase 5 equivalence rerun (6-hour window), run 2026-06-11

Setup:

- Purpose: close the key-versus-node result-equivalence check that P5-2's network drop left open. Window shrunk from 1 day to 6 hours (cutoff `2024-03-30T17:58:00Z`) so the per-row timezone storm fits inside the 5-minute bail-out.
- Window sized first against the source table: 839 raw rows, 764 distinct senders, so an expected storm of ~2.5 minutes.
- Same key-grouped and node-grouped queries as P5-1/P5-2 with only the cutoff changed, run one at a time between 01:53:45Z and 01:56:43Z. Full result sets captured to JSON (a one-off runner script, since `vg-probe` prints only a sample) and diffed in Python.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| EQ-1 | Key-grouped, 6-hour window | OK, 2.7s, 764 rows |
| EQ-2 | Node-grouped, 6-hour window | OK, 141.2s, 764 rows |

Notes:

- The result sets are equivalent: same 764 accounts, identical transfer counts, identical outflows to the cent. Three of 764 rows differ only past the second decimal (for example `110.83000000000001` versus `110.83`), which is floating-point summation order, warehouse-side versus engine-side, not a semantic difference.
- The node-grouped wall-clock confirms the storm model: 839 raw rows in 141 seconds is ~5.9 rows per second, matching the ~5.5-per-second `SELECT current_timezone()` rate measured in the P5-2 history pull.
- Verdict for the developer question: a group-by-node to group-by-primary-key rewrite would be safe, and on this window it is the difference between 2.7 seconds and 141 seconds for the same answer.

### Phase 4, run 2026-06-11

Setup:

- Same warehouse and method, run at 04:14:22Z through `uv run vg-probe`. One new run only: the phase's node-grouped and count-distinct captures were already taken in Phase 5 and are reused per the plan.
- Query: the Pattern 1 structuring query exactly as written in the best-practices doc, amount band `>= 9000 AND < 10000`, no time window, with its `ORDER BY near_threshold DESC` kept.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P4-1 | Structuring query, scalar group key, amount-range filter | OK, 2.0s, 196 rows |

Notes from the run, with the history pull:

- Source-table check: `account_links` has exactly 196 distinct senders and 200 raw rows in the 9000 to 10000 band, so the 196 returned rows are the full group set.
- The history row (04:14:26Z, 1,238 ms, 196 produced rows) shows the full generated SQL: `SELECT sum(t.amount), src.account_id, count(t.src_account_id) FROM accounts AS src JOIN account_links AS t ON t.src_account_id = src.account_id WHERE (t.amount >= ?) AND (t.amount < ?) GROUP BY account_id`. Both halves of the amount range are bound parameters, the same treatment the time filter gets.
- The produced row count equals the final group count, 196, so the warehouse did the aggregation and shipped only the groups. This is the fast path working end to end with an amount-range filter.
- Two absences match earlier findings: no `ORDER BY` in the SQL (Phase 1's rule, the engine sorts) and no `round()` in the SQL (the warehouse returned the raw sum as `unnamed1` and the engine rounded to 2 decimals).

### Phase 6, run 2026-06-11

Setup:

- Same warehouse and method, runs between 04:21:12Z and 04:21:41Z through `uv run vg-probe` (plus one rerun via a one-off script to print both rows, since the probe samples only the first).
- Queries: the chained two-label count written out from the coverage table's failing form, and the union-all form: `MATCH (a:Account) RETURN "accounts" AS label, count(a) AS n UNION ALL MATCH (m:Merchant) RETURN "merchants" AS label, count(m) AS n`.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P6-1 | Chained form (`MATCH ... WITH count(a) ... MATCH ...`) | Failed at parse time, under 1s: `Neo.ClientError.Statement.SyntaxError`, `42NG0: Unsupported syntax. (line 2, column 1 (offset: 18))`, pointing at `WITH count(a) AS accounts` |
| P6-2 | Union-all form | OK, 1.5s, 2 rows: accounts 25,000, merchants 7,500 |

Notes from the runs, with the history pull:

- P6-1 fails before any SQL is generated, the same parse-time behavior as the Phase 2 HAVING failures, so it leaves no history row. The repro evidence is the client error.
- P6-2's counts both match direct source-table counts exactly (25,000 rows in `accounts`, 7,500 in `merchants`).
- History (04:21:27Z and 04:21:28Z): two separate statements, `SELECT ? AS label, count(a.account_id) AS n FROM accounts GROUP BY label` and the merchant twin, 549 and 407 ms, 1 produced row each. The rerun pair at 04:21:41Z took 145 and 164 ms warm. One Cypher statement, two warehouse queries; the engine concatenates the branches.
- The constant label arrives as a bound parameter and the engine groups by it, a harmless quirk (`GROUP BY label` over a constant).

### Phase 7, run 2026-06-11

Setup:

- Same warehouse and method, runs between 04:24:34Z and 04:26:34Z through `uv run vg-probe`, one at a time.
- The anchored control is Phase 3's P3-1, not rerun. Queries: Pattern 5's traversal with the anchor removed and `LIMIT 25` kept; the unanchored two-hop pass-through chain over `TRANSFERRED_TO`; and a four-hop chain added during the session as a depth-escalation probe (P7-3), useful because fixed-length chains are how a user must write layering probes given that `{2,4}` paths do not translate.

Results:

| Run | Variant | Outcome |
|-----|---------|---------|
| P7-1 | Unanchored single-hop (`Account-TRANSACTED_WITH->Merchant`), limit 25 | OK, 5.9s, 25 rows |
| P7-2 | Unanchored two-hop (`a->b->c` over TRANSFERRED_TO), limit 25 | OK, 1.5s, 25 rows |
| P7-3 | Unanchored four-hop (`a->b->c->d->e`), limit 25 | OK, 14.2s, 25 rows |

Notes from the runs, with the history pull:

- All three generated SQLs end with `LIMIT ? /*param_0*/` and all three produced exactly 25 rows on the warehouse: 867 ms, 1,189 ms, and 13,497 ms for one, two, and four hops. The limit pushes down; the join work behind it still grows with depth.
- P7-2's full SQL is the complete capture: it selects all columns of the three account nodes, joins `accounts AS a JOIN account_links AS t1 JOIN accounts AS b JOIN account_links AS t2 JOIN accounts AS c`, and pushes Cypher's relationship-uniqueness rule as `WHERE NOT ((t2.src_account_id = t1.src_account_id) AND (t2.dst_account_id = t1.dst_account_id) AND (t2.link_id = t1.link_id))`.
- P7-3's 14.2s client wall-clock is almost entirely the 13.5s warehouse join, so the depth cost is warehouse compute, not the engine or the wire. Out-degree here averages ~12 (300k edges over 25k accounts), so an unbounded four-hop would be on the order of hundreds of millions of paths; with the limit pushed, the warehouse stopped at 25.
- Timezone nuance: P7-1 was followed by exactly 25 `SELECT current_timezone()` calls (04:24:37 to 04:24:41), one per returned row, matching the known node-value trigger. P7-2 and P7-3 returned node values with the same temporal `opened_date` property seconds later and triggered zero calls (verified by a count over the window through 04:40). So the engine cached or skipped the lookup within the session here, while the Phase 5 node-grouped runs paid it once per row, 3,331 times. The trigger rule from Phase 5 stands, but the storm is evidently not unconditional; left as an open observation at the time, resolved by Phase 9: the trigger is the TIMESTAMP type, and P7-2/P7-3's nodes carry only a DATE.

### Phase 9, run 2026-06-11

Setup:

- Same warehouse and method, runs between 04:52:09Z and 04:53:14Z through `uv run vg-probe`, one at a time, all `LIMIT 25` so each prediction is exactly 25 or 0 calls.
- Free-evidence check first: P5-2's full select list pulled from history (00:00:19Z statement, 3,331 produced rows) before any new query ran. It includes every column of the relationship, `t.transfer_timestamp` among them, which kept the type hypothesis alive and made the predictions worth committing.
- Predictions committed before the history pull: A=25, B=0, C=25 or 0 (the discriminating cell), D=0 (the old node-value rule says 25), E=25+25 if no cache.

Results:

| Run | Variant | Wall-clock | Timezone calls (history) | Predicted |
|-----|---------|------------|--------------------------|-----------|
| P9-A | `RETURN t` (relationship, carries TIMESTAMP) | 5.2s | 25 | 25 |
| P9-B | `RETURN t.amount, t.link_id` (non-temporal scalars) | 0.8s | 0 | 0 |
| P9-C | `RETURN t.transfer_timestamp` (bare TIMESTAMP scalar) | 4.8s | 25 | discriminator |
| P9-D | `RETURN a` (node, DATE only) | 1.1s | 0 | 0 (old rule: 25) |
| P9-E | P9-A rerun twice back to back | 5.2s / 5.0s | 50 total | 50 if no cache |

Notes from the runs, with the history pull:

- The history count bucketed every statement on the warehouse between 04:52:00 and 04:54:30 into the six run windows: 25, 0, 25, 0, 24, 26 timezone calls, with exactly one main statement per window and nothing outside the windows. The 24/26 split on the reruns is one call landing on the 04:53:14 window boundary; the total is exactly 50.
- P9-C is the verdict cell: it built no node and no relationship value, just a bare timestamp column projection, and still paid one round trip per row. The trigger is materializing a TIMESTAMP into a Cypher datetime, full stop.
- P9-D kills the old rule head on: a node value with a temporal property, the exact shape Phase 5 blamed, costs nothing because `opened_date` is a DATE and needs no timezone.
- P9-E kills the cache explanation: the second identical run paid the same 25 calls eight seconds after the first.
- Client wall-clocks line up with the call counts: the three 25-call runs took 4.8 to 5.2 seconds while the zero-call runs took about 1 second, putting the per-call cost at roughly 0.1 to 0.2 seconds, consistent with the ~5.5 to 5.9 calls per second observed in the Phase 5 and equivalence-rerun storms.
- Retroactive reconciliation, now exception-free: Phase 3 stormed 8 (returned `t` with `txn_timestamp`), P7-1 stormed 25 (same shape), P7-2/P7-3 quiet (nodes only, DATE), P5-2 stormed 3,331 (raw pull carried `transfer_timestamp` per row), P5-3 quiet (pull carried account columns only, DATE), EQ-2 stormed 839 at the same rate.

## Raw warehouse SQL by phase

This section records the exact SQL text the warehouse received for every test run above, pulled from `system.query.history` on warehouse `b0fffb8e3255bf85` via the async SQL statements API, per the operational note. Four pulls, all 2026-06-11: window 2026-06-10T23:30Z to 2026-06-11T02:15Z covering Phases 1, 2, 3, and 5; window 2026-06-11T04:05Z to 04:21Z covering Phase 4; window 2026-06-11T04:15Z to 04:45Z covering Phases 6 and 7; and window 2026-06-11T04:45Z onward covering Phase 9. Reading notes:

- Bound parameters appear as `?` placeholders in the history text; the values, such as the time-window cutoff, never appear in the recorded SQL.
- Aura emits two-part table names (`` `graph-enriched-schema`.`accounts` ``) because the session catalog is already set; the verification queries we sent ourselves use three-part names. That difference is how to tell Aura-generated SQL from our own checks at a glance.
- Excluded from the listings: roughly 450 `select 1` connection-pool keepalives and the `SELECT current_timezone()` storm statements (3,339 in Phase 5's window, 25 in Phase 7's, 100 in Phase 9's), which are summarized where they occurred.
- Durations here are `execution_duration_ms` from history, which is warehouse execution only; the work-log figures above quoted total duration, so the numbers differ. The 3 to 5 ms warm Phase 1 runs are the warehouse result cache serving the identical statement.

### Phase 1: top-N and order-by pushdown

The test: five variants of the seven-day fan-out pair aggregation, run 2026-06-10 23:39 to 23:40Z. Control with no sort or limit (P1-1), `LIMIT 10` only (P1-2), `ORDER BY pair_outflow DESC` only (P1-3), `ORDER BY sender` only (P1-4), and the top-N combining the alias sort with `LIMIT 10` (P1-5).

The generated SQL: all five Cypher variants produced **byte-identical SQL**. No `ORDER BY` and no `LIMIT` ever reached the warehouse; only the grouping and the time filter pushed down.

```sql
SELECT
  `src`.`account_id` AS `sender`,
  `dst`.`account_id` AS `recipient`,
  count(`t`.`src_account_id`) AS `pair_transfers`,
  sum(`t`.`amount`) AS `pair_outflow`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
GROUP BY `sender`, `recipient`
```

The five statements in history, in run order:

| Run | Start (Z) | Execution | Produced rows | Statement id |
|-----|-----------|-----------|---------------|--------------|
| P1-1 control | 23:39:16 | 2,596 ms | 22,096 | `01f16525-9823-164a-85d3-ff5780fd8695` |
| P1-2 limit only | 23:39:34 | 4 ms (cache) | 22,096 | `01f16525-a293-1542-8c8c-9cb7243494e6` |
| P1-3 order by alias | 23:39:45 | 5 ms (cache) | 22,096 | `01f16525-a92b-126b-8c8d-890b947a2611` |
| P1-4 order by column | 23:39:57 | 3 ms (cache) | 22,096 | `01f16525-b0b7-10fb-80c6-e1e2e9fc50cf` |
| P1-5 top-N | 23:40:11 | 4 ms (cache) | 22,096 | `01f16525-b8ba-1641-83e7-d6de6944a5f5` |

### Phase 2: threshold behavior

**P2-1 (isolated threshold repro) and P2-2 (textbook form): no SQL exists.** Both failed at Cypher parse time with `42NG0: Unsupported syntax`, so no statement was ever generated or sent; history confirms nothing arrived in their time slots.

**P2-3, the working fan-in form with no threshold.** The full per-recipient group set is produced on the warehouse; the threshold the demo applies client-side is nowhere in the SQL. Run 23:51:07Z, 329 ms, 9,847 rows produced, statement `01f16527-3ff9-1964-b811-54d6be1ce32c`.

```sql
SELECT
  `dst`.`account_id` AS `account_id`,
  count(`t`.`src_account_id`) AS `transfers`,
  sum(`t`.`amount`) AS `inflow`
FROM `graph-enriched-schema`.`account_links` AS `t`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
GROUP BY `account_id`
```

**Verification query (ours, not Aura-generated).** The full-group-set confirmation against the source table, run 23:51:20Z, producing the 9,847 versus 1,157 split quoted in the results.

```sql
SELECT count(DISTINCT dst_account_id) AS recipient_groups,
       count(DISTINCT CASE WHEN cnt >= 5 THEN dst_account_id END) AS groups_meeting_threshold
FROM (
  SELECT dst_account_id, count(*) AS cnt
  FROM `graph-on-databricks`.`graph-enriched-schema`.`account_links`
  WHERE transfer_timestamp >= '2024-03-23T23:58:00'
  GROUP BY dst_account_id
)
```

### Phase 3: anchored traversal

**P3-1, the Pattern 5 anchored visualization query (account 184, limit 25).** Both the anchor and the limit are in the SQL, as parameters: `WHERE (a.account_id = ?) ... LIMIT ?`. The engine asked for every property of all three graph elements, which is what a visualization query needs. Run 23:56:12Z, 794 ms, 8 rows produced, statement `01f16527-f5bd-19c9-a289-af04083aa154`. The 8 returned rows then each triggered one `SELECT current_timezone()` round trip, because the returned nodes carry the temporal `opened_date` property; those 8 statements are part of the 3,339-statement storm logged under Phase 5.

```sql
SELECT
  `a`.`account_id` AS `a_account_id`,
  `a`.`opened_date` AS `a_opened_date`,
  `a`.`holder_age` AS `a_holder_age`,
  `a`.`balance` AS `a_balance`,
  `a`.`account_hash` AS `a_account_hash`,
  `a`.`region` AS `a_region`,
  `a`.`account_type` AS `a_account_type`,
  `t`.`txn_id` AS `t_txn_id`,
  `t`.`account_id` AS `t_account_id`,
  `t`.`merchant_id` AS `t_merchant_id`,
  `t`.`txn_hour` AS `t_txn_hour`,
  `t`.`amount` AS `t_amount`,
  `t`.`txn_timestamp` AS `t_txn_timestamp`,
  `m`.`merchant_id` AS `m_merchant_id`,
  `m`.`category` AS `m_category`,
  `m`.`merchant_name` AS `m_merchant_name`,
  `m`.`region` AS `m_region`
FROM `graph-enriched-schema`.`accounts` AS `a`
JOIN `graph-enriched-schema`.`transactions` AS `t` ON `t`.`account_id` = `a`.`account_id`
JOIN `graph-enriched-schema`.`merchants` AS `m` ON `m`.`merchant_id` = `t`.`merchant_id`
WHERE (`a`.`account_id` = ? /*autoint0*/)
LIMIT ? /*param_0*/
```

**Verification query (ours).** The source-table check that account 184 has exactly 8 edges to 8 merchants, run 23:56:38Z.

```sql
SELECT count(*) AS edges_184, count(DISTINCT merchant_id) AS merchants_184
FROM `graph-on-databricks`.`graph-enriched-schema`.`transactions`
WHERE account_id = 184
```

### Phase 4: capture the slow path

Only one new statement: the phase's node-grouped and count-distinct captures were reused from Phase 5 and appear under that heading below.

**P4-1, the Pattern 1 structuring query with the amount-range filter.** The fast path working end to end: both halves of the amount range arrive as bound parameters, the grouping and both aggregates push down, and the warehouse ships only the 196 final groups. Two things from the Cypher are absent, matching earlier findings: the `ORDER BY near_threshold DESC` (Phase 1's rule, the engine sorts) and the `round(..., 2)`, so the warehouse returns the raw sum under the engine-assigned alias `unnamed1` and the engine rounds. Run 04:14:26Z, 290 ms, 196 rows produced, statement `01f1654c-08eb-1dcc-bab5-47853a8a379f`.

```sql
SELECT
  sum(`t`.`amount`) AS `unnamed1`,
  `src`.`account_id` AS `account_id`,
  count(`t`.`src_account_id`) AS `near_threshold`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
WHERE (`t`.`amount` >= ? /*autoint0*/) AND (`t`.`amount` < ? /*autoint1*/)
GROUP BY `account_id`
```

**Verification query (ours).** The source-table check behind the 196-senders-over-200-rows note, run 04:14:45Z.

```sql
SELECT count(DISTINCT src_account_id) AS senders, count(*) AS rows_in_band
FROM `graph-on-databricks`.`graph-enriched-schema`.account_links
WHERE amount >= 9000 AND amount < 10000
```

### Phase 5: developer questions (1-day window)

**P5-1, key-grouped aggregation.** Clean pushdown: the grouping, both aggregates, and the time filter all in SQL. Run 2026-06-11 00:00:07Z, 349 ms, 2,455 rows produced, statement `01f16528-81ee-193c-8787-2a05763aadbb`.

```sql
SELECT
  `src`.`account_id` AS `account_id`,
  count(`t`.`src_account_id`) AS `transfers`,
  sum(`t`.`amount`) AS `outflow`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
GROUP BY `account_id`
```

**P5-2, node-grouped aggregation.** The materialize smoking gun: a raw row pull of every source-node column plus every relationship column, with the time filter pushed down and **no GROUP BY and no aggregates**. The engine planned to aggregate the rows itself. Run 00:00:19Z, 426 ms, 3,331 rows produced (one per transfer in the window), statement `01f16528-88e8-1d33-970b-b7a9c54f826b`.

```sql
SELECT
  `src`.`account_id` AS `src_account_id`,
  `src`.`opened_date` AS `src_opened_date`,
  `src`.`holder_age` AS `src_holder_age`,
  `src`.`balance` AS `src_balance`,
  `src`.`account_hash` AS `src_account_hash`,
  `src`.`region` AS `src_region`,
  `src`.`account_type` AS `src_account_type`,
  `t`.`src_account_id` AS `t_src_account_id`,
  `t`.`dst_account_id` AS `t_dst_account_id`,
  `t`.`link_id` AS `t_link_id`,
  `t`.`transfer_timestamp` AS `t_transfer_timestamp`,
  `t`.`amount` AS `t_amount`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
```

After this pull, the engine issued the per-row storm: 3,339 separate copies of the statement below between 23:56:14 and 00:10:42Z, one per P5-2 raw row plus one per P3-1 returned row.

```sql
SELECT current_timezone()
```

**P5-3, count-distinct with a scalar group key.** Also materialized: a raw join pull of every column of both endpoint accounts, with only the time filter pushed down. No `DISTINCT`, no `GROUP BY`, no aggregates; the engine deduplicated and counted itself. Note the pull carries both `opened_date` columns yet triggered no timezone storm, the observation behind the storm-trigger refinement. Run 01:41:43Z, 384 ms, 3,331 rows produced, statement `01f16536-b388-1dd5-8ff3-9464481d10be`.

```sql
SELECT
  `src`.`account_id` AS `src_account_id`,
  `src`.`opened_date` AS `src_opened_date`,
  `src`.`holder_age` AS `src_holder_age`,
  `src`.`balance` AS `src_balance`,
  `src`.`account_hash` AS `src_account_hash`,
  `src`.`region` AS `src_region`,
  `src`.`account_type` AS `src_account_type`,
  `dst`.`account_id` AS `dst_account_id`,
  `dst`.`opened_date` AS `dst_opened_date`,
  `dst`.`holder_age` AS `dst_holder_age`,
  `dst`.`balance` AS `dst_balance`,
  `dst`.`account_hash` AS `dst_account_hash`,
  `dst`.`region` AS `dst_region`,
  `dst`.`account_type` AS `dst_account_type`
FROM `graph-enriched-schema`.`accounts` AS `src`
JOIN `graph-enriched-schema`.`account_links` AS `t` ON `t`.`src_account_id` = `src`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `dst` ON `dst`.`account_id` = `t`.`dst_account_id`
WHERE (`t`.`transfer_timestamp` >= ? /*param_0*/)
```

### Phase 5 equivalence rerun (6-hour window)

The rerun changed only the cutoff value, which is a bound parameter, so both forms generated **byte-identical SQL to their 1-day counterparts**; only the produced row counts differ.

**Window-sizing check (ours).** Run 01:53:05Z, confirming 839 raw rows and 764 senders before committing to the window.

```sql
SELECT count(*) AS raw_rows, count(DISTINCT src_account_id) AS senders, max(transfer_timestamp) AS max_ts
FROM `graph-on-databricks`.`graph-enriched-schema`.account_links
WHERE transfer_timestamp >= '2024-03-30 17:58:00'
```

**EQ-1, key-grouped.** Same SQL as P5-1. Run 01:53:55Z, 311 ms, 764 rows produced, statement `01f16538-674f-10a7-a8d6-979a20de0068`.

**EQ-2, node-grouped.** Same raw-pull SQL as P5-2. Run 01:54:22Z, 280 ms, 839 rows produced, statement `01f16538-77a5-1952-909e-7370b7c0b26c`. The 141-second client wall-clock is entirely the post-pull per-row timezone storm; the warehouse was done in 280 ms.

### Phase 6: union all for two-label counts

**P6-1, the chained two-label form: no SQL exists.** It failed at Cypher parse time with `42NG0: Unsupported syntax`, the same behavior as the Phase 2 threshold failures, so nothing was generated or sent.

**P6-2, the union-all form.** One Cypher statement became **two separate warehouse statements**, one per branch, submitted a second apart; the engine concatenated the two single-row results itself. The constant label arrives as a bound parameter and the engine adds a `GROUP BY label` over it, a harmless quirk. The probe rerun that printed both rows repeated the identical pair at 04:21:41Z, served from the result cache in 3 ms each.

Branch 1, accounts. Run 04:21:27Z, 44 ms, 1 row produced, statement `01f1654d-03b4-1d9c-83a2-3accbea6d0d9`:

```sql
SELECT
  ? /*autostring0*/ AS `label`,
  count(`a`.`account_id`) AS `n`
FROM `graph-enriched-schema`.`accounts` AS `a`
GROUP BY `label`
```

Branch 2, merchants. Run 04:21:28Z, 36 ms, 1 row produced, statement `01f1654d-0415-10e4-9f57-1481778e260e`:

```sql
SELECT
  ? /*autostring1*/ AS `label`,
  count(`m`.`merchant_id`) AS `n`
FROM `graph-enriched-schema`.`merchants` AS `m`
GROUP BY `label`
```

**Verification query (ours).** The source-table counts both P6-2 rows were checked against, run 04:21:50Z.

```sql
SELECT (SELECT count(*) FROM `graph-on-databricks`.`graph-enriched-schema`.accounts) AS accounts,
       (SELECT count(*) FROM `graph-on-databricks`.`graph-enriched-schema`.merchants) AS merchants
```

### Phase 7: limit on an unanchored join

All three runs confirm the headline: the limit reaches the warehouse on unanchored traversals at every depth tried, and each statement produced exactly 25 rows. None of the three has a time filter, because the Cypher had none; P7-1's SQL has no `WHERE` clause at all, just the join and the limit.

**P7-1, unanchored single-hop, limit 25.** Pattern 5's traversal with the anchor removed: the same SQL as P3-1 minus the `WHERE (a.account_id = ?)` line. Run 04:24:36Z, 293 ms, 25 rows produced, statement `01f1654d-7496-109d-b30b-d77d5667a38c`. This pull also confirms the 25 `SELECT current_timezone()` calls that followed, 04:24:37 to 04:24:41Z, one per returned row.

```sql
SELECT
  `a`.`account_id` AS `a_account_id`,
  `a`.`opened_date` AS `a_opened_date`,
  `a`.`holder_age` AS `a_holder_age`,
  `a`.`balance` AS `a_balance`,
  `a`.`account_hash` AS `a_account_hash`,
  `a`.`region` AS `a_region`,
  `a`.`account_type` AS `a_account_type`,
  `t`.`txn_id` AS `t_txn_id`,
  `t`.`account_id` AS `t_account_id`,
  `t`.`merchant_id` AS `t_merchant_id`,
  `t`.`txn_hour` AS `t_txn_hour`,
  `t`.`amount` AS `t_amount`,
  `t`.`txn_timestamp` AS `t_txn_timestamp`,
  `m`.`merchant_id` AS `m_merchant_id`,
  `m`.`category` AS `m_category`,
  `m`.`merchant_name` AS `m_merchant_name`,
  `m`.`region` AS `m_region`
FROM `graph-enriched-schema`.`accounts` AS `a`
JOIN `graph-enriched-schema`.`transactions` AS `t` ON `t`.`account_id` = `a`.`account_id`
JOIN `graph-enriched-schema`.`merchants` AS `m` ON `m`.`merchant_id` = `t`.`merchant_id`
LIMIT ? /*param_0*/
```

**P7-2, unanchored two-hop over TRANSFERRED_TO, limit 25.** All columns of the three account nodes, the five-table join chain, Cypher's relationship-uniqueness rule pushed as a `WHERE NOT (...)` predicate on the two relationship aliases, and the limit. Run 04:24:52Z, 678 ms, 25 rows produced, statement `01f1654d-7e07-14e9-af30-e00d76301090`.

```sql
SELECT
  `a`.`account_id` AS `a_account_id`,
  `a`.`opened_date` AS `a_opened_date`,
  `a`.`holder_age` AS `a_holder_age`,
  `a`.`balance` AS `a_balance`,
  `a`.`account_hash` AS `a_account_hash`,
  `a`.`region` AS `a_region`,
  `a`.`account_type` AS `a_account_type`,
  `b`.`account_id` AS `b_account_id`,
  `b`.`opened_date` AS `b_opened_date`,
  `b`.`holder_age` AS `b_holder_age`,
  `b`.`balance` AS `b_balance`,
  `b`.`account_hash` AS `b_account_hash`,
  `b`.`region` AS `b_region`,
  `b`.`account_type` AS `b_account_type`,
  `c`.`account_id` AS `c_account_id`,
  `c`.`opened_date` AS `c_opened_date`,
  `c`.`holder_age` AS `c_holder_age`,
  `c`.`balance` AS `c_balance`,
  `c`.`account_hash` AS `c_account_hash`,
  `c`.`region` AS `c_region`,
  `c`.`account_type` AS `c_account_type`
FROM `graph-enriched-schema`.`accounts` AS `a`
JOIN `graph-enriched-schema`.`account_links` AS `t1` ON `t1`.`src_account_id` = `a`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `b` ON `b`.`account_id` = `t1`.`dst_account_id`
JOIN `graph-enriched-schema`.`account_links` AS `t2` ON `t2`.`src_account_id` = `b`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `c` ON `c`.`account_id` = `t2`.`dst_account_id`
WHERE (NOT ((`t2`.`src_account_id` = `t1`.`src_account_id`) AND (`t2`.`dst_account_id` = `t1`.`dst_account_id`) AND (`t2`.`link_id` = `t1`.`link_id`)))
LIMIT ? /*param_0*/
```

**P7-3, unanchored four-hop, limit 25 (escalation probe).** The same shape deepened to a nine-table join: five account aliases, four relationship aliases, and the uniqueness rule expanded to all six pairwise `NOT (...)` predicates among `t1` through `t4`. The limit is still the last line. Run 04:26:36Z, 12,090 ms, 25 rows produced, statement `01f1654d-bbfa-1a8d-9fb2-19e0e17a4fa0`. The execution time is the depth cost the work log describes: the warehouse runs the join until it has 25 surviving rows, and that took 12 seconds at depth four against sub-second at depths one and two.

```sql
SELECT
  `a`.`account_id` AS `a_account_id`,
  `a`.`opened_date` AS `a_opened_date`,
  `a`.`holder_age` AS `a_holder_age`,
  `a`.`balance` AS `a_balance`,
  `a`.`account_hash` AS `a_account_hash`,
  `a`.`region` AS `a_region`,
  `a`.`account_type` AS `a_account_type`,
  `b`.`account_id` AS `b_account_id`,
  `b`.`opened_date` AS `b_opened_date`,
  `b`.`holder_age` AS `b_holder_age`,
  `b`.`balance` AS `b_balance`,
  `b`.`account_hash` AS `b_account_hash`,
  `b`.`region` AS `b_region`,
  `b`.`account_type` AS `b_account_type`,
  `c`.`account_id` AS `c_account_id`,
  `c`.`opened_date` AS `c_opened_date`,
  `c`.`holder_age` AS `c_holder_age`,
  `c`.`balance` AS `c_balance`,
  `c`.`account_hash` AS `c_account_hash`,
  `c`.`region` AS `c_region`,
  `c`.`account_type` AS `c_account_type`,
  `d`.`account_id` AS `d_account_id`,
  `d`.`opened_date` AS `d_opened_date`,
  `d`.`holder_age` AS `d_holder_age`,
  `d`.`balance` AS `d_balance`,
  `d`.`account_hash` AS `d_account_hash`,
  `d`.`region` AS `d_region`,
  `d`.`account_type` AS `d_account_type`,
  `e`.`account_id` AS `e_account_id`,
  `e`.`opened_date` AS `e_opened_date`,
  `e`.`holder_age` AS `e_holder_age`,
  `e`.`balance` AS `e_balance`,
  `e`.`account_hash` AS `e_account_hash`,
  `e`.`region` AS `e_region`,
  `e`.`account_type` AS `e_account_type`
FROM `graph-enriched-schema`.`accounts` AS `a`
JOIN `graph-enriched-schema`.`account_links` AS `t1` ON `t1`.`src_account_id` = `a`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `b` ON `b`.`account_id` = `t1`.`dst_account_id`
JOIN `graph-enriched-schema`.`account_links` AS `t2` ON `t2`.`src_account_id` = `b`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `c` ON `c`.`account_id` = `t2`.`dst_account_id`
JOIN `graph-enriched-schema`.`account_links` AS `t3` ON `t3`.`src_account_id` = `c`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `d` ON `d`.`account_id` = `t3`.`dst_account_id`
JOIN `graph-enriched-schema`.`account_links` AS `t4` ON `t4`.`src_account_id` = `d`.`account_id`
JOIN `graph-enriched-schema`.`accounts` AS `e` ON `e`.`account_id` = `t4`.`dst_account_id`
WHERE (NOT ((`t4`.`src_account_id` = `t3`.`src_account_id`) AND (`t4`.`dst_account_id` = `t3`.`dst_account_id`) AND (`t4`.`link_id` = `t3`.`link_id`))) AND (NOT ((`t4`.`src_account_id` = `t2`.`src_account_id`) AND (`t4`.`dst_account_id` = `t2`.`dst_account_id`) AND (`t4`.`link_id` = `t2`.`link_id`))) AND (NOT ((`t4`.`src_account_id` = `t1`.`src_account_id`) AND (`t4`.`dst_account_id` = `t1`.`dst_account_id`) AND (`t4`.`link_id` = `t1`.`link_id`))) AND (NOT ((`t3`.`src_account_id` = `t2`.`src_account_id`) AND (`t3`.`dst_account_id` = `t2`.`dst_account_id`) AND (`t3`.`link_id` = `t2`.`link_id`))) AND (NOT ((`t3`.`src_account_id` = `t1`.`src_account_id`) AND (`t3`.`dst_account_id` = `t1`.`dst_account_id`) AND (`t3`.`link_id` = `t1`.`link_id`))) AND (NOT ((`t2`.`src_account_id` = `t1`.`src_account_id`) AND (`t2`.`dst_account_id` = `t1`.`dst_account_id`) AND (`t2`.`link_id` = `t1`.`link_id`)))
LIMIT ? /*param_0*/
```

### Phase 9: timezone round-trip trigger

Five discriminating runs, all `LIMIT 25`, run 04:52:09Z to 04:53:14Z, each isolating one variable of the storm trigger. The SQL itself adds an observation the phase was not even after: **all five statements are single-table scans with the limit pushed and no joins at all**. When the Cypher matches `(src)-[t:TRANSFERRED_TO]->(dst)` but returns only the relationship or its properties, the engine never joins the endpoint `accounts` tables; it scans `account_links` alone. So the limit pushdown rule from Phase 7 extends to bare unanchored scans, and the engine prunes unused endpoint joins. The interesting differences between these runs are not in the SQL, which is uniformly cheap (86 to 125 ms, 25 rows each); they are in what followed each statement, counted from the same pull: 100 `SELECT current_timezone()` calls total, 04:52:12Z to 04:53:22Z, distributed exactly as the type hypothesis predicted.

**P9-A, `RETURN t`, the relationship value carrying a TIMESTAMP.** Followed by 25 timezone calls. Run 04:52:11Z, 88 ms, 25 rows produced, statement `01f16551-4ef1-17b7-9f32-1494d2d3bdfb`.

```sql
SELECT
  `t`.`src_account_id` AS `t_src_account_id`,
  `t`.`dst_account_id` AS `t_dst_account_id`,
  `t`.`link_id` AS `t_link_id`,
  `t`.`transfer_timestamp` AS `t_transfer_timestamp`,
  `t`.`amount` AS `t_amount`
FROM `graph-enriched-schema`.`account_links` AS `t`
LIMIT ? /*param_0*/
```

**P9-B, `RETURN t.amount, t.link_id`, non-temporal scalars.** Same match, same table, but the select list shrinks to exactly the two projected columns; `transfer_timestamp` is not pulled at all. Followed by 0 timezone calls. Run 04:52:27Z, 89 ms, 25 rows produced, statement `01f16551-5843-11f1-8751-63dbde111ab8`.

```sql
SELECT
  `t`.`amount` AS `amount`,
  `t`.`link_id` AS `link_id`
FROM `graph-enriched-schema`.`account_links` AS `t`
LIMIT ? /*param_0*/
```

**P9-C, `RETURN t.transfer_timestamp`, the bare TIMESTAMP scalar.** The verdict cell: a one-column projection, no node or relationship value built, and still 25 timezone calls followed. Run 04:52:38Z, 86 ms, 25 rows produced, statement `01f16551-5f2c-18ca-9b32-7697297ad5d7`.

```sql
SELECT
  `t`.`transfer_timestamp` AS `ts`
FROM `graph-enriched-schema`.`account_links` AS `t`
LIMIT ? /*param_0*/
```

**P9-D, `MATCH (a:Account) RETURN a`, a node value whose only temporal is a DATE.** All seven account columns including `opened_date`, and 0 timezone calls followed: the head-on disproof of the old node-value rule. Run 04:52:56Z, 125 ms, 25 rows produced, statement `01f16551-69d7-1a10-914d-9fe5b3b7483d`.

```sql
SELECT
  `a`.`account_id` AS `a_account_id`,
  `a`.`opened_date` AS `a_opened_date`,
  `a`.`holder_age` AS `a_holder_age`,
  `a`.`balance` AS `a_balance`,
  `a`.`account_hash` AS `a_account_hash`,
  `a`.`region` AS `a_region`,
  `a`.`account_type` AS `a_account_type`
FROM `graph-enriched-schema`.`accounts` AS `a`
LIMIT ? /*param_0*/
```

**P9-E, P9-A rerun twice back to back.** Both statements byte-identical to P9-A, served from the warehouse result cache in 2 and 3 ms, and each still followed by 25 timezone calls (50 total): the SQL was cached, the timezone lookups were not. Run 04:53:09Z, statement `01f16551-7191-1340-952e-429be3799cd0`, and 04:53:18Z, statement `01f16551-76a6-1344-a9ca-ded7f0244b41`, 25 rows produced each.
