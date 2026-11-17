"""Unit tests for vg-generate (generate_large.py). None needs a live warehouse."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import generate_large as gen
from connection import DatabricksConfig, load_databricks_config


def _has_sdk() -> bool:
    # find_spec on the parent alone is not enough: other packages, such as
    # databricks-sql-connector, install a bare ``databricks`` namespace.
    try:
        return importlib.util.find_spec("databricks.sdk") is not None
    except ModuleNotFoundError:
        return False


HAS_SDK = _has_sdk()


def quiet() -> contextlib.redirect_stdout:
    return contextlib.redirect_stdout(io.StringIO())


class ValidatorTests(unittest.TestCase):
    def test_positive_int(self) -> None:
        self.assertEqual(gen.positive_int("1"), 1)
        for bad in ("0", "-5", "1.5", "x"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                gen.positive_int(bad)

    def test_community_size(self) -> None:
        self.assertEqual(gen.community_size("2"), 2)
        for bad in ("1", "0", "-2", "x"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                gen.community_size(bad)

    def test_probability(self) -> None:
        for good, want in (("0", 0.0), ("1", 1.0), ("0.8", 0.8)):
            with self.subTest(good=good):
                self.assertEqual(gen.probability(good), want)
        for bad in ("-0.1", "1.01", "nan", "inf", "x"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                gen.probability(bad)


class ParserTests(unittest.TestCase):
    def parse(self, *argv: str) -> argparse.Namespace:
        return gen.build_parser().parse_args(list(argv))

    def test_defaults(self) -> None:
        a = self.parse()
        self.assertEqual((a.accounts, a.merchants, a.transactions, a.transfers,
                          a.community_size, a.within, a.yes),
                         (200_000, 60_000, 2_500_000, 2_500_000, 50, 0.8, False))

    def test_bad_values_exit_2(self) -> None:
        for argv in (("--accounts", "0"), ("--merchants", "0"),
                     ("--transactions", "-1"), ("--transfers", "0"),
                     ("--community-size", "0"), ("--community-size", "1"),
                     ("--within", "1.5"), ("--within", "-0.5")):
            with self.subTest(argv=argv), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as cm:
                self.parse(*argv)
            self.assertEqual(cm.exception.code, 2)

    def test_help_does_not_mention_customers(self) -> None:
        self.assertNotIn("customer", gen.build_parser().format_help().lower())


class SqlBuilderTests(unittest.TestCase):
    def test_timestamp_stays_inside_the_original_window(self) -> None:
        # Offsets run 0 to N - 1 minutes, and 2024-03-30 23:58 is minute 129,598.
        self.assertEqual(gen._WINDOW_MINUTES, 129_599)
        self.assertIn("floor(rand(7) * 129599)", gen._timestamp(7))
        self.assertIn("timestamp'2024-01-01 00:00:00'", gen._timestamp(7))

    def test_accounts_ids_start_above_the_base(self) -> None:
        sql = gen.accounts_sql("acc", 1000, 5)
        self.assertIn("INSERT INTO acc", sql)
        self.assertIn("1000 + id AS account_id", sql)
        self.assertIn("FROM range(1, 6)", sql)

    def test_merchants_ids_start_above_the_base(self) -> None:
        sql = gen.merchants_sql("m", 7500, 3)
        self.assertIn("7500 + id AS merchant_id", sql)
        self.assertIn("FROM range(1, 4)", sql)

    def test_transactions_link_only_new_rows(self) -> None:
        sql = gen.transactions_sql("t", 10, 100, 7, 200, 4, 5)
        self.assertIn("10 + id AS txn_id", sql)
        self.assertIn("100 + 1 + cast(floor(rand(41) * 7) AS bigint) AS account_id",
                      sql)
        self.assertIn("200 + 1 + cast(floor(rand(42) * 4) AS bigint)", sql)
        self.assertIn("hour(txn_timestamp) AS txn_hour", sql)
        self.assertIn("FROM range(1, 6)", sql)

    def test_transfers_draw_inside_a_partial_last_community(self) -> None:
        sql = gen.transfers_sql("l", 10, 100, 7, 5, 3, 0.8)
        self.assertIn("(src - 1) div 3 * 3 AS community_start", sql)
        self.assertIn("least(3, 7 - community_start)", sql)
        self.assertIn("rand(51) < 0.8", sql)
        self.assertIn("ELSE 1 + cast(floor(rand(53) * 7) AS bigint)", sql)
        self.assertIn("1 + cast(floor(rand(54) * 7) AS bigint) AS src", sql)
        self.assertIn("WHERE src_account_id <> dst_account_id", sql)

    def test_builders_no_longer_include_customers(self) -> None:
        self.assertFalse(hasattr(gen, "customers_sql"))


class MainTests(unittest.TestCase):
    cfg = DatabricksConfig(profile="p", warehouse="wh", catalog="c", schema="s")

    def run_main(self, *argv: str, run_sql: MagicMock | None = None,
                 client: MagicMock | None = None) -> str:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["vg-generate", *argv]))
            stack.enter_context(patch.object(gen, "load_databricks_config",
                                             return_value=self.cfg))
            stack.enter_context(patch.object(gen, "run_sql", run_sql or MagicMock()))
            if client is not None:
                stack.enter_context(patch("databricks.sdk.WorkspaceClient", client))
            stack.enter_context(contextlib.redirect_stdout(out))
            try:
                gen.main()
            except SystemExit as exc:
                out.write(f"\n<exit {exc.code}>")
        return out.getvalue()

    def test_plan_only_writes_nothing_and_needs_no_sdk(self) -> None:
        run_sql = MagicMock()
        with patch.dict(sys.modules, {"databricks.sdk": None}):
            out = self.run_main(run_sql=run_sql)
        run_sql.assert_not_called()
        self.assertIn("Plan only", out)
        self.assertNotIn("customer", out.lower())

    def test_plan_only_runs_without_a_warehouse(self) -> None:
        run_sql = MagicMock()
        with patch.object(self, "cfg", replace(self.cfg, warehouse=None)):
            out = self.run_main(run_sql=run_sql)
        run_sql.assert_not_called()
        self.assertIn("warehouse (not set)", out)
        self.assertIn("Plan only", out)
        self.assertNotIn("<exit", out)

    def test_yes_without_a_warehouse_exits_1_before_connecting(self) -> None:
        run_sql, client = MagicMock(), MagicMock()
        with patch.object(self, "cfg", replace(self.cfg, warehouse=None)), \
                patch.dict(sys.modules, {"databricks.sdk": None}):
            out = self.run_main("--yes", run_sql=run_sql)
        run_sql.assert_not_called()
        client.assert_not_called()
        self.assertIn(gen.MISSING_WAREHOUSE, out)
        self.assertNotIn("uv sync --extra history", out)
        self.assertIn("<exit 1>", out)

    def test_empty_warehouse_in_the_env_file_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("VG_BACKING_WAREHOUSE_ID=\n")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VG_BACKING_WAREHOUSE_ID", None)
                cfg = load_databricks_config(env_file)
        self.assertIsNone(cfg.warehouse)
        with patch.object(self, "cfg", cfg), \
                patch.dict(sys.modules, {"databricks.sdk": None}):
            out = self.run_main("--yes")
        self.assertIn(gen.MISSING_WAREHOUSE, out)
        self.assertIn("<exit 1>", out)

    def test_warehouse_flag_satisfies_the_requirement(self) -> None:
        with patch.object(self, "cfg", replace(self.cfg, warehouse=None)), \
                patch.dict(sys.modules, {"databricks.sdk": None}):
            out = self.run_main("--yes", "--warehouse", "wh2")
        self.assertIn("on warehouse wh2", out)
        self.assertNotIn(gen.MISSING_WAREHOUSE, out)

    def test_yes_without_the_sdk_names_the_extra(self) -> None:
        with patch.dict(sys.modules, {"databricks.sdk": None}):
            out = self.run_main("--yes")
        self.assertIn("uv sync --extra history", out)
        self.assertIn("<exit 1>", out)

    @unittest.skipUnless(HAS_SDK, "needs the history extra (databricks-sdk)")
    def test_bad_profile_exits_1_without_a_traceback(self) -> None:
        for exc in (ValueError("no p profile configured"),
                    OSError("cannot configure CLI token source")):
            with self.subTest(exc=type(exc).__name__):
                out = self.run_main("--yes", client=MagicMock(side_effect=exc))
                self.assertIn("FAILED to connect with Databricks profile p", out)
                self.assertIn("<exit 1>", out)

    @unittest.skipUnless(HAS_SDK, "needs the history extra (databricks-sdk)")
    def test_yes_inserts_four_tables_in_order(self) -> None:
        run_sql = MagicMock(return_value=SimpleNamespace(rows=[["0"]],
                                                         wall_clock_s=1.0))
        self.run_main("--yes", "--accounts", "5", "--merchants", "2",
                      "--transactions", "3", "--transfers", "4", run_sql=run_sql,
                      client=MagicMock())
        statements = [c.args[2] for c in run_sql.call_args_list]
        inserts = [s.split()[2] for s in statements if "INSERT INTO" in s]
        self.assertEqual(inserts, ["`c`.`s`.`accounts`", "`c`.`s`.`merchants`",
                                   "`c`.`s`.`transactions`",
                                   "`c`.`s`.`account_links`"])
        self.assertFalse(any("customer" in s for s in statements))

    @unittest.skipUnless(HAS_SDK, "needs the history extra (databricks-sdk)")
    def test_failed_insert_names_the_appended_tables(self) -> None:
        ok = SimpleNamespace(rows=[["0"]], wall_clock_s=1.0)
        # Four max_id reads, the accounts insert, then the merchants insert fails.
        run_sql = MagicMock(side_effect=[ok] * 5 + [gen.SqlError("boom")])
        out = self.run_main("--yes", run_sql=run_sql, client=MagicMock())
        self.assertIn("FAILED: boom", out)
        self.assertIn("Already appended, with no undo: accounts.", out)
        self.assertIn("The merchants insert failed.", out)
        self.assertIn("A re-run appends another batch", out)
        self.assertIn("<exit 1>", out)

    @unittest.skipUnless(HAS_SDK, "needs the history extra (databricks-sdk)")
    def test_failed_row_count_after_the_writes_says_so(self) -> None:
        ok = SimpleNamespace(rows=[["0"]], wall_clock_s=1.0)
        # Four max_id reads and four inserts succeed, then the first count fails.
        run_sql = MagicMock(side_effect=[ok] * 8 + [gen.SqlError("count failed")])
        out = self.run_main("--yes", run_sql=run_sql, client=MagicMock())
        self.assertIn("All inserts finished. Only the final row count failed.", out)
        self.assertNotIn("Already appended", out)
        self.assertNotIn("insert failed", out)
        self.assertIn("A re-run appends another batch", out)

    @unittest.skipUnless(HAS_SDK, "needs the history extra (databricks-sdk)")
    def test_failed_read_before_any_insert_has_no_append_warning(self) -> None:
        run_sql = MagicMock(side_effect=gen.SqlError("no table"))
        out = self.run_main("--yes", run_sql=run_sql, client=MagicMock())
        self.assertIn("FAILED: no table", out)
        self.assertNotIn("appended", out)
        self.assertNotIn("re-run", out.lower())


if __name__ == "__main__":
    unittest.main()
