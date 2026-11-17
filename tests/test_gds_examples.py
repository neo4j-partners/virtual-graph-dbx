"""Behavior checks for the GDS examples demo. None needs a live Aura instance."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from neo4j.exceptions import Neo4jError

from demos import gds_common, gds_examples

ENCODED_ID = 42


class FakeStatements:
    """Answer each demo statement, and record which graphs were dropped."""

    def __init__(self, fail: frozenset[str] = frozenset(), edges: int = 9) -> None:
        self.fail = fail
        self.edges = edges
        self.dropped: list[str] = []
        self.ran: list[str] = []

    def __call__(self, _driver, _label, cypher, params, *,
                 raise_on_error: bool = False):
        self.ran.append(cypher)
        if cypher in self.fail:
            if raise_on_error:
                raise Neo4jError._hydrate_neo4j(
                    code="Neo.ClientError.Procedure.ProcedureCallFailed",
                    message="out of memory")
            return None
        answers = {
            gds_examples.COUNT_ALL: [{"edges": self.edges}],
            gds_examples.PROJECT_ALL: [{"result": {"nodeCount": 3}}],
            gds_examples.PAGERANK_STREAM: [{"nodeId": ENCODED_ID, "score": 0.5}],
            gds_examples.WCC_STATS: [
                {"componentCount": 1, "componentDistribution": {"max": 3}}],
            gds_examples.LOUVAIN_STATS: [
                {"communityCount": 1, "modularity": 0.1, "ranLevels": 1,
                 "communityDistribution": {"max": 3}}],
            gds_examples.LOUVAIN_TOP: [{"communityId": 7, "size": 3}],
        }
        if cypher == gds_examples.DROP_GRAPH:
            self.dropped.append(params["graph"])
            return []
        return answers[cypher]


class RunExamplesTests(unittest.TestCase):
    def args(self, **overrides: object) -> argparse.Namespace:
        values = {"graph": None, "memory": "4GB", "limit": 5,
                  "count_only": False, "keep": False}
        return argparse.Namespace(**{**values, **overrides})

    def run_demo(self, args: argparse.Namespace,
                 statements: FakeStatements) -> tuple[bool, str]:
        driver_context = MagicMock()
        buf = io.StringIO()
        with (
            patch.object(gds_examples, "load_connection",
                         return_value=("neo4j+s://test", ("u", "p"))),
            patch.object(gds_examples.GraphDatabase, "driver",
                         return_value=driver_context),
            patch.object(gds_examples, "run_statement", side_effect=statements),
            patch.object(gds_common, "run_statement", side_effect=statements),
            patch.object(gds_examples, "decode_account_id", return_value="A1"),
            contextlib.redirect_stdout(buf),
        ):
            return gds_examples.run_examples(args), buf.getvalue()

    def test_success_runs_every_algorithm_and_drops(self) -> None:
        statements = FakeStatements()
        ok, out = self.run_demo(self.args(), statements)
        self.assertTrue(ok)
        for cypher in (gds_examples.PAGERANK_STREAM, gds_examples.WCC_STATS,
                       gds_examples.LOUVAIN_STATS, gds_examples.LOUVAIN_TOP):
            self.assertIn(cypher, statements.ran)
        self.assertEqual(len(statements.dropped), 1)
        self.assertIn("A1", out)

    def test_keep_leaves_the_projection(self) -> None:
        statements = FakeStatements()
        ok, out = self.run_demo(self.args(keep=True), statements)
        self.assertTrue(ok)
        self.assertEqual(statements.dropped, [])
        self.assertIn("--keep set", out)

    def test_failed_algorithm_still_drops_even_with_keep(self) -> None:
        statements = FakeStatements(fail=frozenset({gds_examples.WCC_STATS}))
        ok, _ = self.run_demo(self.args(keep=True), statements)
        self.assertFalse(ok)
        self.assertEqual(len(statements.dropped), 1)
        self.assertIn(gds_examples.LOUVAIN_TOP, statements.ran)

    def test_failed_drop_is_a_failure(self) -> None:
        statements = FakeStatements(fail=frozenset({gds_examples.DROP_GRAPH}))
        ok, _ = self.run_demo(self.args(), statements)
        self.assertFalse(ok)

    def test_failed_projection_skips_the_algorithms(self) -> None:
        statements = FakeStatements(fail=frozenset({gds_examples.PROJECT_ALL}))
        ok, _ = self.run_demo(self.args(), statements)
        self.assertFalse(ok)
        self.assertNotIn(gds_examples.PAGERANK_STREAM, statements.ran)
        self.assertEqual(len(statements.dropped), 1)

    def test_named_graph_drops_a_stale_projection_first(self) -> None:
        statements = FakeStatements()
        ok, _ = self.run_demo(self.args(graph="mine"), statements)
        self.assertTrue(ok)
        self.assertEqual(statements.dropped, ["mine", "mine"])

    def test_count_only_and_empty_graph_do_not_project(self) -> None:
        for args, statements in (
            (self.args(count_only=True), FakeStatements()),
            (self.args(), FakeStatements(edges=0)),
        ):
            with self.subTest(count_only=args.count_only, edges=statements.edges):
                ok, _ = self.run_demo(args, statements)
                self.assertTrue(ok)
                self.assertEqual(statements.ran, [gds_examples.COUNT_ALL])

    def test_failed_count_is_a_failure(self) -> None:
        statements = FakeStatements(fail=frozenset({gds_examples.COUNT_ALL}))
        ok, _ = self.run_demo(self.args(), statements)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
