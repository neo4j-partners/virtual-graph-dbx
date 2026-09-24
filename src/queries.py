"""Virtual-Graph-compatible Finance Genie fraud-signal queries.

Queries 1-10 are the forms from ``finding-fraud.md`` and all run by default. Each one
that aggregates groups by scalar properties and applies its threshold in a second
``WITH``, so Databricks runs the ``GROUP BY`` and returns one row per group. A node or
relationship group key, ``count(DISTINCT <relationship>)``, or a threshold ``WHERE`` on
the aggregating ``WITH`` itself keeps the ``GROUP BY`` out of the SQL, and the raw rows
come back instead. Thresholds, ``ORDER BY`` and ``LIMIT`` always run in the Neo4j
engine. Query 9 is the one exception: its ``date()`` group key cannot push down, and the
pushable epoch-day form is slower. Query 11 (layering cycles) is kept for reference but
is not run by default. The Virtual Graph rejects its quantified path pattern
(``42NG1``).

Two adaptations remain:

1. **Relative time windows become a ``$since`` parameter** anchored to the dataset's max
   timestamp, since ``datetime() - duration(...)`` inside a ``WHERE`` is unsupported
   (``42NG0``). The per-pair 48h and 24h windows in Queries 8 and 10 are dropped, so
   their results stay comparable with ``finding-fraud.md``.
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
from typing import Any, Literal

Row = dict[str, Any]
# "transfer" windows a timestamp and yields a datetime. "opened" windows the account
# opened_date and yields a date.
SinceSource = Literal["transfer", "opened"]
# "table" prints rows. "graph" returns entities for Aura visualization.
QueryKind = Literal["table", "graph"]


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
    since_source: SinceSource = "transfer"
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
    """Replace Query 10's ``avg_turnaround_s`` seconds with ``avg_turnaround_hours``.

    The seconds come from ``toInteger()`` timestamp arithmetic, which pushes down to SQL
    as epoch seconds. ``duration.inSeconds`` keeps the ``GROUP BY`` out of the SQL, and
    ``.epochMillis`` on a relationship property raises ``01N52``.
    """
    seconds = row.pop("avg_turnaround_s")
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
        note=(
            "30-day opened window via $since. The scalar group key carries "
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
            "Single MATCH grouped on scalar a_id/b_id. The a<b filter bounds the "
            "pair (~1s). The pattern binds one row per (f, g) combination, so the "
            "legs are counted with count(DISTINCT <link_id>) and each direction's sum "
            "is divided by the other direction's leg count to undo the cross-product. "
            "count(DISTINCT f) on the relationship itself does not push down."
        ),
        cypher="""
MATCH (a:Account)-[f:TRANSFERRED_TO]->(b:Account)-[g:TRANSFERRED_TO]->(a)
WHERE a.account_id < b.account_id
WITH a.account_id AS a_id, b.account_id AS b_id,
     count(DISTINCT f.link_id) AS n_ab, count(DISTINCT g.link_id) AS n_ba,
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
        note="balance>0 in a leading WHERE. The scalar group key carries balance.",
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
            "7-day window via $since. count(DISTINCT sender) pushes down. The "
            "senders>=5 threshold sits in a second WITH so the GROUP BY still does."
        ),
        cypher="""
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
WITH dst.account_id AS recipient, count(DISTINCT src.account_id) AS senders,
     count(t) AS transfers, round(sum(t.amount), 2) AS inflow
WITH recipient, senders, transfers, inflow
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
            "7-day window via $since. Mirror of fan-in: count(DISTINCT recipient) "
            "pushes down. The recipients>=5 threshold sits in a second WITH."
        ),
        cypher="""
MATCH (src:Account)-[t:TRANSFERRED_TO]->(dst:Account)
WHERE t.transfer_timestamp >= $since
WITH src.account_id AS sender, count(DISTINCT dst.account_id) AS recipients,
     count(t) AS transfers, round(sum(t.amount), 2) AS outflow
WITH sender, recipients, transfers, outflow
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
            "the signal targets. transfer_count>=100 sits in a second WITH so the "
            "GROUP BY pushes down. merchant_count<20 is filtered client-side after the "
            "merge. ~4s in total, most of it reading the 24,999 enrich rows."
        ),
        cypher="""
MATCH (a:Account)-[tr:TRANSFERRED_TO]-(:Account)
WITH a.account_id AS account_id, count(tr) AS transfer_count
WITH account_id, transfer_count
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
            "Two-hop join (~1s). 48h forward window dropped to match the doc. The "
            "forward-after-receive ordering and the same-value (5%) test are kept. "
            "passthroughs counts (in, out) pairs, so one incoming transfer can count "
            "several times. The WITH groups per incoming transfer on its unique "
            "link_id (a scalar key, so the GROUP BY pushes down) so forwarded_in and "
            "volume count each one once. Aliased mule_id: account_id is ambiguous in "
            "the pushed-down SQL."
        ),
        cypher="""
MATCH (a:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(b:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND abs(t_out.amount - t_in.amount) <= 0.05 * t_in.amount
  AND a <> b
WITH mule.account_id AS mule_id, t_in.link_id AS in_link, t_in.amount AS in_amount,
     count(*) AS outs
RETURN mule_id,
       sum(outs)                 AS passthroughs,
       count(in_link)            AS forwarded_in,
       round(sum(in_amount), 2)  AS volume
ORDER BY passthroughs DESC, mule_id ASC
LIMIT 50
""",
    ),
    Query(
        number=9,
        title="Shared-merchant burst (coordinated ring)",
        note=(
            "Groups by the merchant node and day with collect(DISTINCT ...) (~5s). "
            "The date() key keeps the GROUP BY out of the SQL. An epoch-day key "
            "pushes down but is slower (~35s), because of the collected arrays. "
            "account_count>=4 and txns<=200 filtered server-side. The txns<=200 cap is "
            "a safeguard against bulk merchant-days. It never applies to this sample, "
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
            "Unbounded two-hop join grouped on scalar mule_id (~1-2s). 24h window "
            "dropped to match the doc. Average turnaround is computed over all "
            "forward-after-receive pairs as toInteger() epoch-second differences, "
            "which push down, and converted to hours client-side. rapid_pairs>=50 "
            "sits in a second WITH so the GROUP BY pushes down."
        ),
        row_transform=avg_turnaround_to_hours,
        cypher="""
MATCH (src:Account)-[t_in:TRANSFERRED_TO]->(mule:Account)
      -[t_out:TRANSFERRED_TO]->(dst:Account)
WHERE t_out.transfer_timestamp >= t_in.transfer_timestamp
  AND src <> dst
WITH mule.account_id AS mule_id,
     count(*) AS rapid_pairs,
     avg(toInteger(t_out.transfer_timestamp)
         - toInteger(t_in.transfer_timestamp)) AS avg_turnaround_s
WITH mule_id, rapid_pairs, avg_turnaround_s
WHERE rapid_pairs >= 50
RETURN mule_id AS account_id, rapid_pairs, avg_turnaround_s
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
# The basic demo uses account 17813 and merchant 1 as anchors, falling back to the
# lowest ids if either is missing, and prints which ids it used, so the same query
# can be pasted into the Workspace.


@dataclass(frozen=True)
class BasicQuery:
    number: int
    title: str
    cypher: str
    kind: QueryKind = "table"
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
ORDER BY accounts DESC, account_type ASC
""",
    ),
    BasicQuery(
        number=4,
        title="Accounts by region",
        kind="table",
        cypher="""
MATCH (a:Account)
RETURN a.region AS region, count(*) AS accounts
ORDER BY accounts DESC, region ASC
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
            "Undirected so it shows money in and out. It returns the 25 most recent "
            "transfers (~1s)."
        ),
        cypher="""
MATCH (a:Account {account_id: $account_id})-[t:TRANSFERRED_TO]-(b:Account)
RETURN a, t, b
ORDER BY t.transfer_timestamp DESC, t.link_id ASC
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
ORDER BY a.account_id, t.txn_id
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
ORDER BY m.merchant_id, b.account_id, t1.txn_id, t2.txn_id
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
MATCH p=(a:Account {account_id: $account_id})-[t1:TRANSFERRED_TO]->(b:Account)
        -[t2:TRANSFERRED_TO]->(c:Account)
WHERE c <> a
RETURN p
ORDER BY b.account_id, c.account_id, t1.link_id, t2.link_id
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
            "Counting two labels in one chained statement fails with 42NG1 (see B1). "
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
            "Unanchored, so there is no starting filter. LIMIT 25 still pushes into "
            "the SQL as LIMIT ?, so exactly 25 rows come back (~1s)."
        ),
        cypher="""
MATCH (a:Account)-[t:TRANSACTED_WITH]->(m:Merchant)
RETURN a, t, m
ORDER BY a.account_id, m.merchant_id, t.txn_id
LIMIT 25
""",
    ),
    BasicQuery(
        number=14,
        title="Any 25 two-hop transfer chains (unanchored, LIMIT)",
        kind="graph",
        note=(
            "Unanchored two-hop over TRANSFERRED_TO, returned as paths so the "
            "relationships draw too. The limit still pushes down to 25 rows (~1s). "
            "The pushed SQL also carries Cypher's relationship-uniqueness rule."
        ),
        cypher="""
MATCH p=(a:Account)-[t1:TRANSFERRED_TO]->(b:Account)-[t2:TRANSFERRED_TO]->(c:Account)
RETURN p
ORDER BY a.account_id, b.account_id, c.account_id, t1.link_id, t2.link_id
LIMIT 25
""",
    ),
    BasicQuery(
        number=15,
        title="Any 25 four-hop transfer chains (unanchored, LIMIT)",
        kind="graph",
        note=(
            "Depth escalation: LIMIT 25 bounds the output, not the join work behind "
            "it. Exactly 25 paths come back (1 to 2s). No ORDER BY, so they are an "
            "arbitrary 25: a sort makes the warehouse build every four-hop chain "
            "before the limit applies. An anchor is what makes a deep traversal cheap."
        ),
        cypher="""
MATCH p=(a:Account)-[:TRANSFERRED_TO]->(b:Account)-[:TRANSFERRED_TO]->(c:Account)
      -[:TRANSFERRED_TO]->(d:Account)-[:TRANSFERRED_TO]->(e:Account)
RETURN p
LIMIT 25
""",
    ),
]
