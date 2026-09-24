"""Unit tests for helpers.py. None needs a live connection."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import unittest
from unittest.mock import MagicMock

from neo4j.exceptions import Neo4jError, ServiceUnavailable
from neo4j.time import Date, DateTime

import helpers
import queries

MAX_TRANSFER = DateTime(2024, 3, 30, 23, 58, 0, tzinfo=dt.UTC)
MAX_OPENED = Date(2022, 12, 6)


def capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class SinceParamTests(unittest.TestCase):
    def q(self, **kw) -> queries.Query:
        return queries.Query(number=0, title="t", cypher="", **kw)

    def test_transfer_datetime(self) -> None:
        got = helpers.since_param(self.q(since_window_days=7), MAX_TRANSFER,
                                  MAX_OPENED)
        self.assertEqual(got, dt.datetime(2024, 3, 23, 23, 58, tzinfo=dt.UTC))
        self.assertIsInstance(got, dt.datetime)

    def test_opened_source_is_date(self) -> None:
        got = helpers.since_param(
            self.q(since_window_days=30, since_source="opened"),
            MAX_TRANSFER, MAX_OPENED)
        self.assertEqual(got, dt.date(2022, 11, 6))
        self.assertNotIsInstance(got, dt.datetime)

    def test_real_queries_match_doc_literals(self) -> None:
        """Q2/Q5/Q6 $since values equal the literals in docs/finding-fraud.md."""
        by = {q.number: q for q in queries.QUERIES}
        self.assertEqual(helpers.since_param(by[2], MAX_TRANSFER, MAX_OPENED),
                         dt.date(2022, 11, 6))
        for n in (5, 6):
            self.assertEqual(
                helpers.since_param(by[n], MAX_TRANSFER, MAX_OPENED),
                dt.datetime(2024, 3, 23, 23, 58, tzinfo=dt.UTC))

    def test_no_window_raises(self) -> None:
        with self.assertRaises(TypeError):
            helpers.since_param(self.q(), MAX_TRANSFER, MAX_OPENED)


class FmtAndPrintTableTests(unittest.TestCase):
    def test_fmt_scalars(self) -> None:
        self.assertEqual(helpers._fmt(5), "5")
        self.assertEqual(helpers._fmt(None), "None")
        self.assertEqual(helpers._fmt("x" * 100), "x" * 100)

    def test_fmt_short_list(self) -> None:
        self.assertEqual(helpers._fmt([1, 2, 3]), "1, 2, 3")

    def test_fmt_list_boundary(self) -> None:
        self.assertEqual(helpers._fmt(["a" * 60]), "a" * 60)
        out = helpers._fmt(["a" * 61])
        self.assertEqual(len(out), 60)
        self.assertTrue(out.endswith("..."))

    def test_empty(self) -> None:
        out = capture(helpers.print_table, [], 10, 0)
        self.assertEqual(out, "  (no rows)\n")
        self.assertNotIn("threshold", out)

    def test_alignment_and_more_line(self) -> None:
        rows = [{"id": 1, "name": "abc"}, {"id": 22, "name": "d"}]
        out = capture(helpers.print_table, rows, 2, 5).splitlines()
        self.assertEqual(out[0], "  id  name")
        self.assertEqual(out[1], "  --  ----")
        self.assertEqual(out[2], "  1   abc")
        self.assertEqual(out[-1], "  ... 3 more matching row(s)")

    def test_no_trailing_padding(self) -> None:
        out = capture(helpers.print_table, [{"a": 1, "b": "x"}, {"a": 2, "b": "yyy"}],
                      10, 2)
        for line in out.splitlines():
            self.assertEqual(line, line.rstrip())

    def test_no_more_line_when_all_shown(self) -> None:
        out = capture(helpers.print_table, [{"a": 1}], 10, 1)
        self.assertNotIn("more matching", out)

    def test_missing_column_in_later_row(self) -> None:
        out = capture(helpers.print_table, [{"a": 1, "b": 2}, {"a": 3}], 10, 2)
        self.assertIn("None", out)

    def test_rows_zero_reports_the_match_count(self) -> None:
        """--rows 0 with 5 matches reports them, not "no rows"."""
        out = capture(helpers.print_table, [], 0, 5)
        self.assertIn("5 matching row(s) not shown (--rows 0)", out)
        self.assertNotIn("no rows", out)


class DriverErrorTests(unittest.TestCase):
    def test_timeout_cause_adds_hint(self) -> None:
        exc = ServiceUnavailable("lost")
        exc.__cause__ = TimeoutError()
        msg = helpers.driver_error(exc)
        self.assertIn("ServiceUnavailable: lost", msg)
        self.assertIn("(cause: TimeoutError)", msg)
        self.assertIn("60s Bolt read timeout", msg)
        self.assertNotIn(";", msg)

    def test_other_cause_has_no_timeout_hint(self) -> None:
        exc = ServiceUnavailable("dns")
        exc.__cause__ = OSError("name resolution")
        msg = helpers.driver_error(exc)
        self.assertIn("(cause: OSError)", msg)
        self.assertNotIn("Bolt read timeout", msg)

    def test_without_cause(self) -> None:
        msg = helpers.driver_error(ServiceUnavailable("x"))
        self.assertEqual(msg, "ServiceUnavailable: x")


class QueryErrorTests(unittest.TestCase):
    def test_neo4j_error_prints_code_then_indented_message(self) -> None:
        exc = Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError",
                                        message="bad")
        self.assertEqual(helpers.query_error(exc),
                         "Neo.ClientError.Statement.SyntaxError\n  bad")
        self.assertEqual(helpers.query_error(exc, indent=""),
                         "Neo.ClientError.Statement.SyntaxError\nbad")

    def test_driver_error_uses_driver_summary(self) -> None:
        exc = ServiceUnavailable("x")
        self.assertEqual(helpers.query_error(exc), helpers.driver_error(exc))


class RunCypherAndMaxDatesTests(unittest.TestCase):
    def test_run_cypher_passes_timeout_and_params(self) -> None:
        record = MagicMock()
        record.data.return_value = {"x": 1}
        tx = MagicMock()
        tx.run.return_value = [record]
        session = MagicMock()
        session.begin_transaction.return_value.__enter__.return_value = tx
        driver = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        rows = helpers.run_cypher(driver, "RETURN $a", {"a": 5}, 12.5)
        self.assertEqual(rows, [{"x": 1}])
        session.begin_transaction.assert_called_once_with(timeout=12.5)
        tx.run.assert_called_once_with("RETURN $a", a=5)

    def test_data_max_dates(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [([{"mx": MAX_TRANSFER}], None, None),
                                            ([{"mx": MAX_OPENED}], None, None)]
        self.assertEqual(helpers.data_max_dates(driver), (MAX_TRANSFER, MAX_OPENED))

    def test_data_max_dates_empty_graph_exits(self) -> None:
        for first, second in (([{"mx": None}], [{"mx": MAX_OPENED}]),
                              ([], [{"mx": MAX_OPENED}])):
            driver = MagicMock()
            driver.execute_query.side_effect = [(first, None, None),
                                                (second, None, None)]
            with self.subTest(first=first), self.assertRaises(SystemExit) as cm:
                helpers.data_max_dates(driver)
            self.assertIsInstance(cm.exception.code, str)

    def test_first_row(self) -> None:
        self.assertEqual(helpers.first_row([{"a": 1}, {"a": 2}], "x"), {"a": 1})
        with self.assertRaises(SystemExit) as cm:
            helpers.first_row([], "the finder")
        self.assertIn("No rows came back for the finder", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
