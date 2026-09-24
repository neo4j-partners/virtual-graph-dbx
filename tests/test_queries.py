"""Unit tests for queries.py definitions and the fraud and basic demos.

None needs a live connection.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import re
import unittest
from typing import ClassVar, get_args
from unittest.mock import MagicMock, patch

from neo4j.exceptions import Neo4jError, ServiceUnavailable
from neo4j.time import Date, DateTime

import queries
from demos import basic, fraud

PARAM_RE = re.compile(r"\$(\w+)")
MAX_TRANSFER = DateTime(2024, 3, 30, 23, 58, 0, tzinfo=dt.UTC)
MAX_OPENED = Date(2022, 12, 6)


def neo4j_error(message: str,
                code: str = "Neo.ClientError.Procedure.ProcedureCallFailed"
                ) -> Neo4jError:
    return Neo4jError._hydrate_neo4j(code=code, message=message)


def capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def params_of(cypher: str) -> set[str]:
    return set(PARAM_RE.findall(cypher))


class QueryDefinitionTests(unittest.TestCase):
    def test_numbers_unique_and_sequential(self) -> None:
        self.assertEqual([q.number for q in queries.QUERIES], list(range(1, 12)))
        self.assertEqual([q.number for q in queries.BASIC_QUERIES],
                         list(range(1, len(queries.BASIC_QUERIES) + 1)))

    def test_fraud_params_match_what_run_query_supplies(self) -> None:
        for q in queries.QUERIES:
            with self.subTest(q=q.number):
                supplied = {"since"} if q.since_window_days is not None else set()
                self.assertEqual(params_of(q.cypher), supplied)
                if q.enrich_cypher is not None:
                    self.assertEqual(params_of(q.enrich_cypher), set())

    def test_basic_params_subset_of_anchors(self) -> None:
        for q in queries.BASIC_QUERIES:
            with self.subTest(q=q.number):
                self.assertLessEqual(params_of(q.cypher),
                                     {"account_id", "merchant_id"})

    def test_enum_like_fields(self) -> None:
        for q in queries.QUERIES:
            self.assertIn(q.since_source, get_args(queries.SinceSource))
        for q in queries.BASIC_QUERIES:
            self.assertIn(q.kind, get_args(queries.QueryKind))

    def test_only_q11_unsupported(self) -> None:
        self.assertEqual(
            [q.number for q in queries.QUERIES if not q.vg_supported], [11])

    def test_no_sql_comments_or_trailing_semicolons(self) -> None:
        for q in [*queries.QUERIES, *queries.BASIC_QUERIES]:
            self.assertNotIn("--", q.cypher.replace("-->", "").replace("<--", ""))
            self.assertFalse(q.cypher.strip().endswith(";"))

    def test_client_side_hooks_only_where_expected(self) -> None:
        by = {q.number: q for q in queries.QUERIES}
        self.assertEqual([q.number for q in queries.QUERIES if q.enrich_cypher], [7])
        self.assertEqual([q.number for q in queries.QUERIES if q.client_filter], [7])
        self.assertEqual([q.number for q in queries.QUERIES if q.row_transform],
                         [10])
        self.assertTrue(by[7].client_filter({"merchant_count": 19}))
        self.assertFalse(by[7].client_filter({"merchant_count": 20}))
        self.assertIn("account_id", by[7].enrich_cypher)

    def test_avg_turnaround_to_hours(self) -> None:
        row = {"avg_turnaround_s": 88_200}
        queries.avg_turnaround_to_hours(row)
        self.assertEqual(row, {"avg_turnaround_hours": 24.5})
        row = {"avg_turnaround_s": 5400.4}
        queries.avg_turnaround_to_hours(row)
        self.assertEqual(row["avg_turnaround_hours"], 1.5)

    def test_notes_follow_the_style_rules(self) -> None:
        """Notes carry no em or en dashes, no "e.g." and no prose semicolons."""
        for q in [*queries.QUERIES, *queries.BASIC_QUERIES]:
            with self.subTest(q=q.number):
                for banned in ("—", "–", "e.g.", ";"):
                    self.assertNotIn(banned, q.note)


class MergeEnrichmentTests(unittest.TestCase):
    def test_merge_with_default(self) -> None:
        rows = [{"account_id": 1}, {"account_id": 2}]
        fraud.merge_enrichment(rows, [{"account_id": 1, "merchant_count": 7}],
                               "account_id", {"merchant_count": 0})
        self.assertEqual(rows, [{"account_id": 1, "merchant_count": 7},
                                {"account_id": 2, "merchant_count": 0}])

    def test_extra_enrich_rows_ignored(self) -> None:
        rows: list[dict] = []
        fraud.merge_enrichment(rows, [{"account_id": 9, "m": 1}], "account_id",
                               {"m": 0})
        self.assertEqual(rows, [])


class SelectQueriesTests(unittest.TestCase):
    def ns(self, **kw) -> argparse.Namespace:
        return argparse.Namespace(**{"only": None, "query": None, **kw})

    def test_default_excludes_unsupported(self) -> None:
        self.assertEqual([q.number for q in fraud.select_queries(self.ns())],
                         list(range(1, 11)))

    def test_only_preserves_order(self) -> None:
        got = fraud.select_queries(self.ns(only=[6, 5, 6]))
        self.assertEqual([q.number for q in got], [6, 5, 6])

    def test_query_11_selectable(self) -> None:
        self.assertEqual(fraud.select_queries(self.ns(query=11))[0].number, 11)

    def test_missing_exits(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            fraud.select_queries(self.ns(query=99))
        self.assertIn("valid: 1-11", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            fraud.select_queries(self.ns(only=[1, 98]))
        self.assertIn("No query numbered 98", str(cm.exception.code))


class RunQueryTests(unittest.TestCase):
    by: ClassVar[dict[int, queries.Query]] = {q.number: q for q in queries.QUERIES}

    def run_q(self, n: int, side_effect,
              rows: int = 10) -> tuple[str, MagicMock, bool]:
        result = {}

        def call() -> None:
            result["ok"] = fraud.run_query(MagicMock(), self.by[n], rows, 1.0,
                                           MAX_TRANSFER, MAX_OPENED)

        with patch.object(fraud, "run_cypher", side_effect=side_effect) as rc:
            out = capture(call)
        return out, rc, result["ok"]

    def test_unsupported_not_run(self) -> None:
        out, rc, ok = self.run_q(11, AssertionError)
        self.assertIn("Not run.", out)
        rc.assert_not_called()
        self.assertTrue(ok)

    def test_since_passed(self) -> None:
        out, rc, ok = self.run_q(5, lambda *a: [])
        self.assertEqual(rc.call_args.args[2]["since"],
                         dt.datetime(2024, 3, 23, 23, 58, tzinfo=dt.UTC))
        self.assertIn("$since = 2024-03-23 23:58:00+00:00", out)
        self.assertIn("(no rows)", out)
        self.assertTrue(ok)

    def test_courier_merge_and_filter(self) -> None:
        main = [{"account_id": 1, "transfer_count": 150},
                {"account_id": 2, "transfer_count": 120},
                {"account_id": 3, "transfer_count": 110}]
        enrich = [{"account_id": 1, "merchant_count": 25},
                  {"account_id": 2, "merchant_count": 3}]
        out, rc, _ = self.run_q(7, [main, enrich])
        self.assertEqual(rc.call_count, 2)
        self.assertIn("3 server row(s), 2 pass the client-side threshold", out)

    def test_row_transform(self) -> None:
        rows = [{"account_id": 1, "rapid_pairs": 60, "avg_turnaround_s": 7200}]
        out, _, _ = self.run_q(10, lambda *a: rows)
        self.assertIn("avg_turnaround_hours", out)
        self.assertIn("2.0", out)

    def test_rows_zero_reports_count(self) -> None:
        out, _, _ = self.run_q(1, lambda *a: [{"account_id": 1}] * 4, rows=0)
        self.assertIn("4 matching row(s) not shown", out)

    def test_errors_are_reported_and_return_false(self) -> None:
        out, _, ok = self.run_q(1, neo4j_error("boom", "Neo.ClientError.Statement.X"))
        self.assertIn("ERROR after", out)
        self.assertIn("Neo.ClientError.Statement.X", out)
        self.assertFalse(ok)
        out, _, ok = self.run_q(1, ServiceUnavailable("gone"))
        self.assertIn("ServiceUnavailable", out)
        self.assertFalse(ok)

    def run_fraud(self, results: list[bool], **kw) -> tuple[bool, MagicMock, str]:
        args = argparse.Namespace(only=[1, 11], query=None, rows=5, timeout=9.0)
        result = {}
        with (
            patch.object(fraud, "data_max_dates",
                         return_value=(MAX_TRANSFER, MAX_OPENED)),
            patch.object(fraud, "run_query", side_effect=results) as rq,
        ):
            out = capture(lambda: result.update(
                ok=fraud.run_fraud(MagicMock(), args, **kw)))
        return result["ok"], rq, out

    def test_run_fraud(self) -> None:
        ok, rq, _ = self.run_fraud([True, True])
        self.assertTrue(ok)
        self.assertEqual([c.args[1].number for c in rq.call_args_list], [1, 11])

    def test_run_fraud_uses_preselected(self) -> None:
        ok, rq, _ = self.run_fraud([True], selected=[self.by[4]])
        self.assertTrue(ok)
        self.assertEqual([c.args[1].number for c in rq.call_args_list], [4])

    def test_run_fraud_reports_failure(self) -> None:
        ok, _, out = self.run_fraud([False, True])
        self.assertFalse(ok)
        self.assertIn("1 query failed: 1", out)


class BasicTests(unittest.TestCase):
    def test_pick_anchors_defaults(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [([{"id": 17813}], 0, 0),
                                            ([{"id": 1}], 0, 0)]
        self.assertEqual(basic.pick_anchors(driver), (17813, 1))

    def test_pick_anchors_fallback(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [([], 0, 0), ([{"id": 5}], 0, 0),
                                            ([], 0, 0), ([{"id": 2}], 0, 0)]
        self.assertEqual(basic.pick_anchors(driver), (5, 2))
        self.assertEqual(driver.execute_query.call_count, 4)

    def test_pick_anchors_empty_graph_exits(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [([], 0, 0), ([], 0, 0)]
        with self.assertRaises(SystemExit) as cm:
            basic.pick_anchors(driver)
        self.assertIn("an anchor account", str(cm.exception.code))

    def test_run_basic_query_kinds(self) -> None:
        graph_q = next(q for q in queries.BASIC_QUERIES if q.kind == "graph")
        table_q = next(q for q in queries.BASIC_QUERIES if q.kind == "table")
        with patch.object(basic, "run_cypher", return_value=[{"x": 1}] * 3):
            out = capture(basic.run_basic_query, MagicMock(), graph_q, 2, 1.0, {})
            self.assertIn("3 path/row(s) returned", out)
            out = capture(basic.run_basic_query, MagicMock(), table_q, 2, 1.0, {})
            self.assertIn("... 1 more matching row(s)", out)
        with patch.object(basic, "run_cypher", side_effect=neo4j_error("x")):
            self.assertIn("ERROR", capture(basic.run_basic_query, MagicMock(),
                                           table_q, 2, 1.0, {}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(basic.run_basic_query(MagicMock(), table_q, 2, 1.0,
                                                       {}))

    def test_run_basic_passes_anchor_params(self) -> None:
        with (
            patch.object(basic, "pick_anchors", return_value=(7, 8)),
            patch.object(basic, "run_basic_query", return_value=True) as rbq,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(basic.run_basic(MagicMock(), 10, 1.0))
        self.assertEqual(rbq.call_count, len(queries.BASIC_QUERIES))
        self.assertEqual(rbq.call_args.args[4], {"account_id": 7, "merchant_id": 8})

    def test_run_basic_reports_failure(self) -> None:
        results = [True] * len(queries.BASIC_QUERIES)
        results[2] = False
        with (
            patch.object(basic, "pick_anchors", return_value=(7, 8)),
            patch.object(basic, "run_basic_query", side_effect=results),
        ):
            out = capture(lambda: self.assertFalse(
                basic.run_basic(MagicMock(), 10, 1.0)))
        self.assertIn("1 basic query failed: B3", out)


if __name__ == "__main__":
    unittest.main()
