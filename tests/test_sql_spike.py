"""Unit tests for the 100m demo (sql_spike.py). None needs a live warehouse."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from databricks.sdk.errors import DatabricksError
from databricks.sdk.service.sql import StatementState

from connection import DatabricksConfig
from demos import sql_spike


def quiet() -> contextlib.redirect_stdout:
    return contextlib.redirect_stdout(io.StringIO())


def status(state: StatementState) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(state=state, error=None),
                           statement_id="s1", manifest=None, result=None)


class RunSqlTests(unittest.TestCase):
    def test_timeout_cancels_the_statement(self) -> None:
        client = MagicMock()
        client.statement_execution.execute_statement.return_value = status(
            StatementState.RUNNING)
        with patch.object(sql_spike.time, "perf_counter", side_effect=[0.0, 10.0]), \
                self.assertRaises(sql_spike.SqlError) as cm:
            sql_spike.run_sql(client, "wh", "SELECT 1", timeout=5.0)
        client.statement_execution.cancel_execution.assert_called_once_with("s1")
        self.assertIn("cancel requested", str(cm.exception))

    def test_sdk_error_becomes_sql_error(self) -> None:
        client = MagicMock()
        client.statement_execution.execute_statement.side_effect = DatabricksError(
            "PERMISSION_DENIED")
        with self.assertRaises(sql_spike.SqlError) as cm:
            sql_spike.run_sql(client, "wh", "SELECT 1")
        self.assertIn("submit failed (DatabricksError", str(cm.exception))

    def test_succeeded_returns_rows(self) -> None:
        resp = status(StatementState.SUCCEEDED)
        resp.result = SimpleNamespace(data_array=[["5"]])
        client = MagicMock()
        client.statement_execution.execute_statement.return_value = resp
        res = sql_spike.run_sql(client, "wh", "SELECT 1")
        self.assertEqual((res.statement_id, res.rows), ("s1", [["5"]]))

    def test_failed_state_raises(self) -> None:
        resp = status(StatementState.FAILED)
        resp.status.error = SimpleNamespace(message="bad table")
        client = MagicMock()
        client.statement_execution.execute_statement.return_value = resp
        with self.assertRaises(sql_spike.SqlError) as cm:
            sql_spike.run_sql(client, "wh", "SELECT 1")
        self.assertIn("bad table", str(cm.exception))


class CQueryTests(unittest.TestCase):
    def test_c1_sums_the_per_account_average(self) -> None:
        c1 = dict(sql_spike.c_queries("t"))["C1 full-table group-by"]
        self.assertIn("sum(avg_amount)", c1)
        self.assertNotIn("avg(avg_amount)", c1)


class PullHistoryTests(unittest.TestCase):
    def info(self, qid: str, spill: int) -> SimpleNamespace:
        metrics = SimpleNamespace(execution_time_ms=10, rows_read_count=100,
                                  spill_to_disk_bytes=spill, result_from_cache=False)
        return SimpleNamespace(query_id=qid, is_final=True, metrics=metrics)

    def test_follows_pages(self) -> None:
        client = MagicMock()
        client.query_history.list.side_effect = [
            SimpleNamespace(res=[self.info("a", 0)], has_next_page=True,
                            next_page_token="p2"),
            SimpleNamespace(res=[self.info("b", 7)], has_next_page=False,
                            next_page_token=None),
        ]
        found = sql_spike.pull_history(client, "wh", 0, 1, {"a", "b"})
        self.assertEqual(set(found), {"a", "b"})
        self.assertEqual(found["b"].spill_bytes, 7)
        self.assertEqual(client.query_history.list.call_count, 2)
        self.assertEqual(
            client.query_history.list.call_args_list[1].kwargs["page_token"], "p2")


class SpillReportTests(unittest.TestCase):
    def rec(self, sid: str) -> sql_spike.RunRecord:
        return sql_spike.RunRecord("C1", 1000, sid, 1.0, "10")

    def report(self, found: dict) -> tuple[bool, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = sql_spike.print_spill_report([self.rec("a")], found)
        return ok, buf.getvalue()

    def test_zero_spill(self) -> None:
        ok, out = self.report({"a": sql_spike.HistRow(10, 100, 0)})
        self.assertTrue(ok)
        self.assertIn("The warehouse builds its hash aggregate", out)
        self.assertNotIn("2X-Small", out)

    def test_spill_pending_or_cached_is_false(self) -> None:
        self.assertFalse(self.report({"a": sql_spike.HistRow(10, 100, 5)})[0])
        self.assertFalse(self.report({})[0])
        self.assertFalse(self.report({"a": sql_spike.HistRow(0, 0, 0, True)})[0])


class RunSpikeTests(unittest.TestCase):
    def args(self, **kw) -> argparse.Namespace:
        base = {"profile": None, "warehouse": None, "build": False, "sizes": None,
                "skip_history": True, "poll_minutes": 3.0,
                "history_lag_minutes": 3.0, "max_wait_minutes": 40.0}
        return argparse.Namespace(**{**base, **kw})

    def spike(self, args: argparse.Namespace, **patches) -> tuple[bool, MagicMock]:
        cfg = DatabricksConfig("p", "c", "s", "cfgwh")
        rec = sql_spike.RunRecord("C1", 5, "a", 1.0, "1")
        with (
            patch.object(sql_spike, "load_databricks_config", return_value=cfg),
            patch("databricks.sdk.WorkspaceClient"),
            patch.object(sql_spike, "table_row_count", return_value=5),
            patch.object(sql_spike, "run_c_queries",
                         **patches.get("rcq", {"return_value": [rec]})) as rcq,
            quiet(),
        ):
            return sql_spike.run_spike(args), rcq

    def test_uses_config_warehouse(self) -> None:
        ok, rcq = self.spike(self.args())
        self.assertTrue(ok)
        self.assertEqual(rcq.call_args.args[1], "cfgwh")

    def test_flag_overrides_config_warehouse(self) -> None:
        _, rcq = self.spike(self.args(warehouse="flagwh"))
        self.assertEqual(rcq.call_args.args[1], "flagwh")

    def test_no_records_is_false(self) -> None:
        ok, _ = self.spike(self.args(), rcq={"return_value": []})
        self.assertFalse(ok)

    def test_history_result_is_returned(self) -> None:
        with patch.object(sql_spike, "await_history", return_value={}), \
                patch.object(sql_spike, "print_spill_report", return_value=False):
            ok, _ = self.spike(self.args(skip_history=False))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
