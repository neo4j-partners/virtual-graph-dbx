"""Virtual-Graph-compatible Finance Genie fraud-signal queries.

Queries 1-10 are the forms from ``finding-fraud.md`` and all run by default. Each one
groups by scalar ids where it can, applies its threshold with a ``WHERE`` after the
aggregating ``WITH`` (the Cypher form of SQL ``HAVING``), and orders and limits
server-side, so Databricks does the aggregation and returns only the top rows. Query 11
(layering cycles) is kept for reference but is not run by default: the Virtual Graph
rejects its quantified path pattern (``42NG1``).

Two adaptations remain:

1. **Relative time windows become a ``$since`` parameter** anchored to the dataset's max
   timestamp, since temporal arithmetic inside a ``WHERE`` is unsupported (``42NG0``).
   The per-pair windows in Queries 8 and 10 cannot be a parameter, so they are dropped.
2. **Split + merge** (courier). ``OPTIONAL MATCH`` is rejected (``42NG1``), so two
   independent single-``MATCH`` aggregations are joined client-side with a default of
   zero for the missing side (``enrich_*``), which keeps the zero-merchant accounts the
   signal targets. The merchant-count threshold therefore runs client-side, after the
   merge (``client_filter``).

An aggregation over two ``Account`` variables names its key ``recipient``, ``sender`` or
``mule_id`` rather than ``account_id``: that alias can collide with the source columns
in the pushed-down SQL (``AMBIGUOUS_REFERENCE``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Row = dict[str, Any]


@dataclass(frozen=True)
class Query:
    number: int
    title: str
    cypher: str
    # False marks a query the Virtual Graph cannot translate at all; it is not run.
    vg_supported: bool = True
    # If set, ``since_param`` (helpers.py) computes ``$since`` = (data max for
    # ``since_source``) - N days.
    since_window_days: int | None = None
    since_source: str = "transfer"  # "transfer" (timestamp) or "opened" (account date)
    since_kind: str = "datetime"  # "datetime" or "date"
    # Client-side threshold, only for a column that exists after the enrich merge.
    client_filter: Callable[[Row], bool] | None = None
    # Client-side per-row conversion, applied in place before filtering and printing.
    row_transform: Callable[[Row], None] | None = None
    # OPTIONAL MATCH replacement: a second aggregation merged onto the main rows by
    # ``enrich_key``. Columns in ``enrich_columns`` are copied from the matching
    # enrich row, or set to the given default when the account has no enrich row.
    enrich_cypher: str | None = None
    enrich_key: str = "account_id"
    enrich_columns: dict[str, Any] = field(default_factory=dict)
    note: str = ""  # how this differs from the doc version


def avg_turnaround_to_hours(row: Row) -> None:
    """Replace Query 10's ``avg_turnaround`` Duration with ``avg_turnaround_hours``.

    Returning a Duration avoids ``.epochMillis`` on a relationship property, which the
    server flags as an unknown property key (``01N52``) even though the values are
    exact.
    """
    d = row.pop("avg_turnaround")
    seconds = d.days * 86400 + d.seconds + d.nanoseconds / 1e9
    row["avg_turnaround_hours"] = round(seconds / 3600, 1)


QUERIES: list[Query] = [
    Query(
        number=1,
        title="Structuring (just-under-threshold transfers)",
        note="Group by scalar account_id (pushes down). Ranked, no threshold.",
        cypher="""
MATCH (src:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE t.amount >= 9000 AND t.amount < 10000
WITH src.account_id AS account_id, count(t) AS near_threshold,
     round(sum(t.amount), 2) AS total
RETURN account_id, near_threshold, total
ORDER BY near_threshold DESC, account_id ASC
LIMIT 50
""",
    ),
    Query(
        number=2,
        title="New accounts moving large sums (new account, high velocity)",
        since_window_days=30,
        since_source="opened",
        since_kind="date",
        note=(
            "30-day opened window via $since; scalar group key carries "
            "opened_date/holder_age."
        ),
        cypher="""
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE a.opened_date >= $since
WITH a.account_id AS account_id, a.opened_date AS opened_date,
     a.holder_age AS holder_age, count(t) AS transfers,
     round(sum(t.amount), 2) AS outflow
RETURN account_id, opened_date, holder_age, transfers, outflow
ORDER BY outflow DESC, account_id ASC
LIMIT 50
""",
    ),
    Query(
        number=3,
        title="Round trips between two accounts (reciprocal transfers)",
        note=(
            "Single MATCH grouped on scalar a_id/b_id; the a<b filter bounds the "
            "pair (~3-4s). The pattern binds one row per (f, g) combination, so the "
            "legs are counted with count(DISTINCT ...) and each direction's sum is "
            "divided by the other direction's leg count to undo the cross-product."
        ),
        cypher="""
MATCH (a:Account)-[f:TRANSFERRED_TO]->(b:Account)-[g:TRANSFERRED_TO]->(a)
WHERE a.account_id < b.account_id
WITH a.account_id AS a_id, b.account_id AS b_id,
     count(DISTINCT f) AS n_ab, count(DISTINCT g) AS n_ba,
     sum(f.amount) AS sf, sum(g.amount) AS sg
RETURN a_id, b_id,
       round(sf / n_ba + sg / n_ab, 2) AS round_trip_volume,
       n_ab + n_ba                     AS leg_count
ORDER BY round_trip_volume DESC, a_id ASC, b_id ASC
LIMIT 50
""",
    ),
    Query(
        number=4,
        title="Velocity ratio (moves more than it holds)",
        note="balance>0 in a leading WHERE; scalar group key carries balance.",
        cypher="""
MATCH (a:Account)-[t:TRANSFERRED_TO]->(:Account)
WHERE a.balance > 0
WITH a.account_id AS account_id, a.balance AS balance, sum(t.amount) AS outflow
RETURN account_id,
       round(balance, 2)           AS balance,
       round(outflow, 2)           AS outflow_volume,
       round(outflow / balance, 1) AS velocity_ratio
ORDER BY velocity_ratio DESC, account_id ASC
LIMIT 50
""",
    ),
    Query(
        number=5,
        title="Collection accounts (fan-in by distinct senders)",
        since_window_days=7,
        since_source="transfer",
        note=(
            "7-day window via $since. count(DISTINCT sender) and the senders>=5 "
            "threshold both run server-side."
        ),
        cypher="""
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
WITH dst.account_id AS recipient, count(DISTINCT src.account_id) AS senders,
     count(t) AS transfers, round(sum(t.amount), 2) AS inflow
WHERE senders >= 5
RETURN recipient AS account_id, senders, transfers, inflow
ORDER BY senders DESC, account_id ASC
LIMIT 50
""",
    ),
    Query(
        number=6,
        title="Spray accounts (fan-out by distinct recipients)",
        since_window_days=7,
        since_source="transfer",
        note=(
            "7-day window via $since. Mirror of fan-in: count(DISTINCT recipient) and "
            "the recipients>=5 threshold both run server-side."
        ),
        cypher="""
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
WITH src.account_id AS sender, count(DISTINCT dst.account_id) AS recipients,
     count(t) AS transfers, round(sum(t.amount), 2) AS outflow
WHERE recipients >= 5
RETURN sender AS account_id, recipients, transfers, outflow
ORDER BY recipients DESC, account_id ASC
LIMIT 50
""",
    ),
    Query(
        number=7,
        title="Courier accounts (P2P-heavy, merchant-light)",
        # merchant_count exists only after the client-side merge (OPTIONAL MATCH is
        # unsupported), and a missing account must count as 0, so this threshold and
        # the top-N stay client-side.
        client_filter=lambda r: r["merchant_count"] < 20,
        note=(
            "Split into two pushdown halves instead of one OPTIONAL MATCH (rejected "
            "with 42NG1): transfer degree is the main aggregation, merchant count is "
            "merged client-side (missing => 0), which keeps the zero-merchant accounts "
            "the signal targets. transfer_count>=100 filtered server-side; "
            "merchant_count<20 filtered client-side after the merge. ~15s in total, "
            "~11s of it in the undirected transfer-degree query."
        ),
        cypher="""
MATCH (a:Account)-[tr:TRANSFERRED_TO]-(:Account)
WITH a.account_id AS account_id, count(tr) AS transfer_count
WHERE transfer_count >= 100
RETURN account_id, transfer_count
ORDER BY transfer_count DESC, account_id ASC
""",
        enrich_cypher="""
MATCH (a:Account)-[tw:TRANSACTED_WITH]->(:Merchant)
WITH a.account_id AS acct, count(tw) AS merchant_count
RETURN acct AS account_id, merchant_count
""",
        enrich_columns={"merchant_count": 0},
    ),
    Query(
        number=8,
        title="Pass-through mule (local betweenness proxy)",
        note=(
            "Two-hop join (~4-6s). 48h forward window dropped (temporal arithmetic "
            "in WHERE unsupported); forward-after-receive ordering and the same-value "
            "(5%) test are kept. passthroughs counts (in, out) pairs, so one incoming "
            "transfer can count several times; the WITH groups per incoming transfer "
            "so forwarded_in and volume count each one once. Aliased mule_id: "
            "account_id is ambiguous in the pushed-down SQL."
        ),
        cypher="""
MATCH (a:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(b:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND abs(t_out.amount - t_in.amount) <= 0.05 * t_in.amount
  AND a <> b
WITH mule.account_id AS mule_id, t_in, count(*) AS outs
RETURN mule_id,
       sum(outs)                  AS passthroughs,
       count(t_in)                AS forwarded_in,
       round(sum(t_in.amount), 2) AS volume
ORDER BY passthroughs DESC, mule_id ASC
LIMIT 50
""",
    ),
    Query(
        number=9,
        title="Shared-merchant burst (coordinated ring)",
        note=(
            "Groups by the merchant node and day with collect(DISTINCT ...) (~5s). "
            "account_count>=4 and txns<=200 filtered server-side. The txns<=200 cap is "
            "a safeguard against bulk merchant-days; it never applies to this sample, "
            "where the busiest merchant-day has 7 purchases."
        ),
        cypher="""
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
WITH m, date(t.txn_timestamp) AS day,
     collect(DISTINCT a.account_id) AS accounts,
     count(t)                       AS txns
WHERE size(accounts) >= 4 AND txns <= 200
RETURN m.merchant_id AS merchant_id, m.merchant_name AS merchant_name, day,
       size(accounts) AS account_count, txns, accounts
ORDER BY account_count DESC, merchant_id ASC, day ASC
LIMIT 50
""",
    ),
    Query(
        number=10,
        title="Rapid-turnover summary per account",
        note=(
            "Unbounded two-hop join, the slowest query (~210s; fits the 300s default "
            "timeout). 24h window dropped (temporal arithmetic in WHERE unsupported); "
            "average turnaround is computed over all forward-after-receive pairs as a "
            "Duration (duration.inSeconds) and converted to hours client-side. "
            "rapid_pairs>=50 filtered server-side."
        ),
        row_transform=avg_turnaround_to_hours,
        cypher="""
MATCH (src:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(dst:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND src <> dst
WITH mule,
     count(*) AS rapid_pairs,
     avg(duration.inSeconds(t_in.transfer_timestamp,
                            t_out.transfer_timestamp)) AS avg_turnaround
WHERE rapid_pairs >= 50
RETURN mule.account_id AS account_id, rapid_pairs, avg_turnaround
ORDER BY rapid_pairs DESC, account_id ASC
LIMIT 50
""",
    ),
    # ----------------------------------------------------------------------- #
    # Documented for reference only: unsupported on the Virtual Graph, never run.
    # ----------------------------------------------------------------------- #
    Query(
        number=11,
        title="Layering cycles (loaded graph only)",
        vg_supported=False,
        note=(
            "Unsupported on the Virtual Graph (42NG1: equijoin on the outer nodes of a "
            "quantified path pattern). Needs a loaded graph."
        ),
        cypher="""
MATCH path = (a:Account)-[:TRANSFERRED_TO]->{2,4}(a)
RETURN a.account_id AS ring_origin,
       length(path) AS hops,
       [n IN nodes(path) | n.account_id] AS cycle
LIMIT 50
""",
    ),
]


# --------------------------------------------------------------------------- #
# Basic exploration / visualization queries (``vg-demo --demo basic``)
# --------------------------------------------------------------------------- #
# These are the warm-up queries: simple counts and small, anchored traversals
# that demonstrate the value of the relationships without any fraud logic. They
# were verified live on the Virtual Graph and are documented in
# ``basic-graph-examples.md``.
#
# Two kinds:
#   - ``kind="table"``: returns scalar rows; the basic demo prints them as a table.
#   - ``kind="graph"``: returns node and relationship entities. The point is the
#     graph picture in the Aura Workspace Query tab, not the CLI output, so the
#     basic demo prints only the row count and timing and tells you to run it there.
#
# The anchored graph queries take ``$account_id`` and ``$merchant_id`` parameters.
# The basic demo picks an anchor account (the first one with an outgoing transfer)
# and a merchant at runtime and prints which ids it used, so the same query can be
# pasted into the Workspace.


@dataclass(frozen=True)
class BasicQuery:
    number: int
    title: str
    cypher: str
    kind: str = "table"  # "table" (print rows) or "graph" (for Aura visualization)
    note: str = ""


BASIC_QUERIES: list[BasicQuery] = [
    BasicQuery(
        number=1,
        title="How many accounts",
        kind="table",
        note=(
            "A single label count, sub-second. Counting two labels in one statement "
            "(MATCH ... WITH count ... MATCH ...) fails with 42NG1, so keep them "
            "separate."
        ),
        cypher="""
MATCH (a:Account) RETURN count(a) AS accounts
""",
    ),
    BasicQuery(
        number=2,
        title="How many merchants",
        kind="table",
        cypher="""
MATCH (m:Merchant) RETURN count(m) AS merchants
""",
    ),
    BasicQuery(
        number=3,
        title="Accounts by type",
        kind="table",
        note="Group-by on a scalar property. Good bar-chart widget.",
        cypher="""
MATCH (a:Account)
RETURN a.account_type AS account_type, count(*) AS accounts
ORDER BY accounts DESC
""",
    ),
    BasicQuery(
        number=4,
        title="Accounts by region",
        kind="table",
        cypher="""
MATCH (a:Account)
RETURN a.region AS region, count(*) AS accounts
ORDER BY accounts DESC
""",
    ),
    BasicQuery(
        number=5,
        title="Merchants by category",
        kind="table",
        cypher="""
MATCH (m:Merchant)
RETURN m.category AS category, count(*) AS merchants
ORDER BY merchants DESC, category ASC
""",
    ),
    BasicQuery(
        number=6,
        title="Top merchants by distinct customers",
        kind="table",
        note=(
            "Full TRANSACTED_WITH scan (under 1s). "
            "The first query that needs the edges."
        ),
        cypher="""
MATCH (a:Account)-[:TRANSACTED_WITH]->(m:Merchant)
RETURN m.merchant_name AS merchant, count(DISTINCT a) AS customers
ORDER BY customers DESC, merchant ASC
LIMIT 10
""",
    ),
    BasicQuery(
        number=7,
        title="Ego network: one account and the merchants it shops at",
        kind="graph",
        note="Anchored on $account_id, so it stays small and fast (~1s).",
        cypher="""
MATCH (a:Account {account_id: $account_id})-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
LIMIT 25
""",
    ),
    BasicQuery(
        number=8,
        title="Ego network: one account and its transfer partners",
        kind="graph",
        note=(
            "Undirected so it shows money in and out; the 25 most recent transfers "
            "(~1s)."
        ),
        cypher="""
MATCH (a:Account {account_id: $account_id})-[t:TRANSFERRED_TO]-(b:Account)
RETURN a, t, b
ORDER BY t.transfer_timestamp DESC
LIMIT 25
""",
    ),
    BasicQuery(
        number=9,
        title="Merchant star: one merchant and the accounts that use it",
        kind="graph",
        note="Anchored on $merchant_id (~1s).",
        cypher="""
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant {merchant_id: $merchant_id})
RETURN a, t, m
LIMIT 25
""",
    ),
    BasicQuery(
        number=10,
        title="2-hop: accounts linked to the anchor through a shared merchant",
        kind="graph",
        note=(
            "The 'value of the graph' shot: an indirect connection a table cannot "
            "show. Anchored, so even with the merchant fan-out it returns in ~1s."
        ),
        cypher="""
MATCH (a:Account {account_id: $account_id})-[t1:TRANSACTED_WITH]->(m:Merchant)
      <-[t2:TRANSACTED_WITH]-(b:Account)
WHERE a <> b
RETURN a, t1, m, t2, b
LIMIT 25
""",
    ),
    BasicQuery(
        number=11,
        title="2-hop: transfer chain (who does my counterparty pay)",
        kind="graph",
        note=(
            "Returns the paths, so the relationships draw too (~1s). c <> a "
            "drops chains that come straight back to the anchor. The chain shape is "
            "the point."
        ),
        cypher="""
MATCH p=(a:Account {account_id: $account_id})-[:TRANSFERRED_TO]->(b:Account)
        -[:TRANSFERRED_TO]->(c:Account)
WHERE c <> a
RETURN p
LIMIT 25
""",
    ),
    # ------------------------------------------------------------------- #
    # Pushdown demonstrations.
    # ------------------------------------------------------------------- #
    BasicQuery(
        number=12,
        title="Count accounts and merchants in one statement (UNION ALL)",
        kind="table",
        note=(
            "Counting two labels in one chained statement fails with 42NG1 (see B1); "
            "UNION ALL is the one-statement workaround. It runs as one Cypher "
            "statement but two pushed SQL statements, one count per branch, "
            "concatenated engine-side."
        ),
        cypher="""
MATCH (a:Account)  RETURN 'accounts'  AS label, count(a) AS n
UNION ALL
MATCH (m:Merchant) RETURN 'merchants' AS label, count(m) AS n
""",
    ),
    BasicQuery(
        number=13,
        title="Any 25 account-merchant edges (unanchored single-hop, LIMIT)",
        kind="graph",
        note=(
            "Unanchored, so there is no starting filter; LIMIT 25 still pushes into "
            "the SQL as LIMIT ?, so exactly 25 rows come back (~1s)."
        ),
        cypher="""
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
LIMIT 25
""",
    ),
    BasicQuery(
        number=14,
        title="Any 25 two-hop transfer chains (unanchored, LIMIT)",
        kind="graph",
        note=(
            "Unanchored two-hop over TRANSFERRED_TO; the limit still pushes down to "
            "25 rows (~1s). The pushed SQL also carries Cypher's "
            "relationship-uniqueness rule."
        ),
        cypher="""
MATCH (a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
RETURN a, b, c
LIMIT 25
""",
    ),
    BasicQuery(
        number=15,
        title="Any 25 four-hop transfer chains (unanchored, LIMIT)",
        kind="graph",
        note=(
            "Depth escalation: LIMIT 25 bounds the output, not the join work behind "
            "it. Exactly 25 rows come back (under 1.5s). An anchor is what makes a "
            "deep traversal cheap."
        ),
        cypher="""
MATCH (a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
      -[:TRANSFERRED_TO]->(d:Account)-[:TRANSFERRED_TO]->(e:Account)
RETURN a, b, c, d, e
LIMIT 25
""",
    ),
]
