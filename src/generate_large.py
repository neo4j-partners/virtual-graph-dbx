"""Grow the lakehouse with synthetic accounts, merchants, transactions and transfers.

WARNING: this appends rows to the existing ``accounts``, ``merchants``,
``transactions`` and ``account_links`` tables. Every demo reads those tables through
the Virtual Graph, so after a run all demo results change, including the fraud
queries and any Genie ground truth built from the original data. Each run appends
another batch, and there is no undo short of re-running the setup notebook.

The rows are generated on the SQL warehouse with ``INSERT INTO ... SELECT`` over
``range()``, so nothing is generated or uploaded locally. New IDs start above each
table's current maximum, so the primary keys stay unique. The new accounts are split
into communities of ``--community-size``; most transfers stay inside a community,
which gives WCC and Louvain in ``--demo gds-examples`` real structure to find. The new
rows link only to each other, so the generated subgraph has no edges to the original
data.
Timestamps fall in the same 2024-01-01 to 2024-03-30 23:58 range as the original
data, so the time windows of the other demos still apply.

Connection details (profile, catalog, schema, warehouse) come from the project
``.env``, the same as ``--demo 100m``. Writing needs the history extra
(``uv sync --extra history``). The plan-only run does not.

Usage:
    uv run vg-generate                 # print the plan only, write nothing
    uv run vg-generate --yes           # append 200K accounts, 60K merchants, ...
    uv run vg-generate --yes --transactions 3000000 --transfers 3000000
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from connection import MISSING_WAREHOUSE, load_databricks_config
from demos.sql_spike import SqlError, run_sql

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

# The original data's transfer and transaction window, in whole minutes. The 90 days
# from 2024-01-01 00:00 hold 129,600 minutes. floor(rand * N) yields offsets 0 to
# N - 1, so N = 129,599 puts the latest timestamp at 2024-03-30 23:58. That is the
# latest original timestamp, so the max stays put and the demos' fixed cutoffs, such
# as 2024-03-23T23:58:00Z, still sit 7 days before it.
_START = "2024-01-01 00:00:00"
_WINDOW_MINUTES = 129_599
_ACCOUNT_TYPES = "array('business', 'savings', 'checking')"
_REGIONS = "array('APAC', 'EU-East', 'US-West', 'EU-West', 'US-Central', 'US-East')"
_CATEGORIES = ("array('gaming', 'online', 'travel', 'retail', 'utilities', "
               "'grocery', 'restaurant', 'crypto')")


def _pick(options: str, count: int, seed: int) -> str:
    """SQL that picks one element of an ``array(...)`` literal at random."""
    return f"element_at({options}, cast(floor(rand({seed}) * {count}) AS int) + 1)"


def _timestamp(seed: int) -> str:
    """SQL for a random whole-minute timestamp inside the original data window."""
    return (f"timestamp'{_START}' + make_interval(0, 0, 0, 0, 0, "
            f"cast(floor(rand({seed}) * {_WINDOW_MINUTES}) AS int), 0)")


def accounts_sql(table: str, base: int, n: int) -> str:
    return f"""
INSERT INTO {table}
SELECT
  {base} + id AS account_id,
  substr(sha2(cast({base} + id AS string), 256), 1, 12) AS account_hash,
  concat('Account ', {base} + id) AS account_name,
  {_pick(_ACCOUNT_TYPES, 3, 11)} AS account_type,
  {_pick(_REGIONS, 6, 12)} AS region,
  round(100 + rand(13) * 499800, 2) AS balance,
  date_add(DATE'2018-01-01', cast(floor(rand(14) * 1800) AS int)) AS opened_date,
  18 + cast(floor(rand(15) * 63) AS int) AS holder_age
FROM range(1, {n + 1})
"""


def merchants_sql(table: str, base: int, n: int) -> str:
    return f"""
INSERT INTO {table}
SELECT
  {base} + id AS merchant_id,
  concat('Merchant ', {base} + id) AS merchant_name,
  {_pick(_CATEGORIES, 8, 31)} AS category,
  {_pick(_REGIONS, 6, 32)} AS region
FROM range(1, {n + 1})
"""


def transactions_sql(table: str, base: int, account_base: int, accounts: int,
                     merchant_base: int, merchants: int, n: int) -> str:
    return f"""
INSERT INTO {table}
SELECT txn_id, account_id, merchant_id, amount, txn_timestamp,
       hour(txn_timestamp) AS txn_hour
FROM (
  SELECT
    {base} + id AS txn_id,
    {account_base} + 1 + cast(floor(rand(41) * {accounts}) AS bigint) AS account_id,
    {merchant_base} + 1 + cast(floor(rand(42) * {merchants}) AS bigint)
      AS merchant_id,
    round(0.25 - ln(1 - rand(43)) * 110, 2) AS amount,
    {_timestamp(44)} AS txn_timestamp
  FROM range(1, {n + 1})
)
"""


def transfers_sql(table: str, base: int, account_base: int, accounts: int,
                  n: int, community_size: int, within: float) -> str:
    """Transfers between new accounts, mostly inside fixed-size communities.

    Account ``k`` (1-based among the new accounts) belongs to community
    ``(k - 1) div community_size``. With probability ``within`` the destination is
    drawn uniformly from the same community, otherwise from all new accounts. When
    ``accounts`` is not a multiple of ``community_size``, the last community is
    smaller, and its draw stays inside it. Draws that land on the source account are
    dropped, so no transfer goes from an account to itself. That drops about
    ``within / community_size`` of the rows, 1.6% with the defaults.
    """
    return f"""
INSERT INTO {table}
SELECT link_id, src_account_id, dst_account_id, amount, transfer_timestamp
FROM (
  SELECT
    link_id,
    {account_base} + src AS src_account_id,
    {account_base} + CASE
      WHEN rand(51) < {within} THEN community_start + 1 + cast(floor(
        rand(52) * least({community_size}, {accounts} - community_start)) AS bigint)
      ELSE 1 + cast(floor(rand(53) * {accounts}) AS bigint)
    END AS dst_account_id,
    amount,
    transfer_timestamp
  FROM (
    SELECT
      link_id,
      src,
      (src - 1) div {community_size} * {community_size} AS community_start,
      amount,
      transfer_timestamp
    FROM (
      SELECT
        {base} + id AS link_id,
        1 + cast(floor(rand(54) * {accounts}) AS bigint) AS src,
        round(0.15 - ln(1 - rand(55)) * 450, 2) AS amount,
        {_timestamp(56)} AS transfer_timestamp
      FROM range(1, {n + 1})
    )
  )
)
WHERE src_account_id <> dst_account_id
"""


def max_id(client: WorkspaceClient, warehouse: str, table: str, column: str) -> int:
    rows = run_sql(client, warehouse,
                   f"SELECT coalesce(max({column}), 0) FROM {table}").rows
    return int(rows[0][0])


def _parse(text: str, kind: type[int | float]) -> int | float:
    """Parse ``text`` as ``kind``, raising the argparse error type on failure."""
    try:
        return kind(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid {kind.__name__} value: {text!r}") from None


def positive_int(text: str) -> int:
    """argparse type: an integer >= 1."""
    value = _parse(text, int)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text}")
    return value


def community_size(text: str) -> int:
    """argparse type: an integer >= 2, so a community holds more than one account."""
    value = _parse(text, int)
    if value < 2:
        raise argparse.ArgumentTypeError(f"must be 2 or more, got {text}")
    return value


def probability(text: str) -> float:
    """argparse type: a float from 0 to 1 inclusive."""
    value = _parse(text, float)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", type=positive_int, default=200_000, metavar="N",
                        help="accounts to add (default: 200000)")
    parser.add_argument("--merchants", type=positive_int, default=60_000, metavar="N",
                        help="merchants to add (default: 60000)")
    parser.add_argument("--transactions", type=positive_int, default=2_500_000,
                        metavar="N",
                        help="account-to-merchant transactions to add "
                             "(default: 2500000)")
    parser.add_argument("--transfers", type=positive_int, default=2_500_000,
                        metavar="N",
                        help="account-to-account transfers to draw (default: "
                             "2500000). Self-transfers are dropped, about 1.6%% with "
                             "the defaults, so about 2.46M are added")
    parser.add_argument("--community-size", type=community_size, default=50,
                        metavar="N",
                        help="accounts per transfer community, at least 2 "
                             "(default: 50)")
    parser.add_argument("--within", type=probability, default=0.8, metavar="P",
                        help="share of transfers that stay inside a community, "
                             "from 0 to 1 (default: 0.8)")
    parser.add_argument("--profile", default=None, metavar="NAME",
                        help="Databricks config profile (default: from .env)")
    parser.add_argument("--warehouse", default=None, metavar="ID",
                        help="SQL warehouse id (default: VG_BACKING_WAREHOUSE_ID "
                             "from .env). One is required to write")
    parser.add_argument("--yes", action="store_true",
                        help="actually write. Without it, only print the plan")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_databricks_config()
    profile = args.profile or cfg.profile
    warehouse = args.warehouse or cfg.warehouse
    schema = f"`{cfg.catalog}`.`{cfg.schema}`"
    tables = {name: f"{schema}.`{name}`" for name in
              ("accounts", "merchants", "transactions", "account_links")}

    print(f"Target {schema} on warehouse {warehouse or '(not set)'} "
          f"(profile {profile}).")
    print(f"Append {args.accounts:,} accounts, "
          f"{args.merchants:,} merchants, {args.transactions:,} transactions, "
          f"{args.transfers:,} transfers.")
    print("WARNING: this changes the results of every demo. There is no undo.")
    if not args.yes:
        print("\nPlan only. Re-run with --yes to write.")
        return
    if not warehouse:
        print(MISSING_WAREHOUSE)
        raise SystemExit(1)

    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.errors import DatabricksError
    except ImportError:
        print("vg-generate needs the history extra: uv sync --extra history")
        raise SystemExit(1) from None
    try:
        client = WorkspaceClient(profile=profile)
    except (ValueError, OSError, DatabricksError) as exc:
        # A missing profile, or a config the SDK cannot authenticate, raises
        # ValueError when the client is built. CLI-based auth raises OSError.
        print(f"FAILED to connect with Databricks profile {profile}: {exc}")
        raise SystemExit(1) from exc

    appended: list[str] = []
    current = None
    writes_done = False
    try:
        account_base = max_id(client, warehouse, tables["accounts"], "account_id")
        merchant_base = max_id(client, warehouse, tables["merchants"], "merchant_id")
        txn_base = max_id(client, warehouse, tables["transactions"], "txn_id")
        link_base = max_id(client, warehouse, tables["account_links"], "link_id")
        steps = [
            ("accounts", accounts_sql(tables["accounts"], account_base,
                                      args.accounts)),
            ("merchants", merchants_sql(tables["merchants"], merchant_base,
                                        args.merchants)),
            ("transactions", transactions_sql(
                tables["transactions"], txn_base, account_base, args.accounts,
                merchant_base, args.merchants, args.transactions)),
            ("account_links", transfers_sql(
                tables["account_links"], link_base, account_base, args.accounts,
                args.transfers, args.community_size, args.within)),
        ]
        for name, sql in steps:
            print(f"\n--- insert into {name}")
            current = name
            result = run_sql(client, warehouse, sql, timeout=1800.0)
            appended.append(name)
            current = None
            print(f"  OK {result.wall_clock_s:.1f}s")
        writes_done = True
        print("\nRow counts now:")
        for name, table in tables.items():
            count = run_sql(client, warehouse, f"SELECT count(*) FROM {table}").rows
            print(f"  {name:<14} {int(count[0][0]):>12,}")
    except SqlError as exc:
        print(f"\nFAILED: {exc}")
        if writes_done:
            print("All inserts finished. Only the final row count failed.")
        elif appended:
            print(f"Already appended, with no undo: {', '.join(appended)}.")
        if current is not None:
            print(f"The {current} insert failed. A timed-out insert can still commit, "
                  "so check its row count.")
        if appended or current is not None:
            print("A re-run appends another batch to every table. To start over, "
                  "re-run the setup notebook.")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
