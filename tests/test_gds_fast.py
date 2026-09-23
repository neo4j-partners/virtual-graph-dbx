"""Behavior checks for Aura projection failures in the fast GDS demo."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from neo4j.exceptions import Neo4jError

from demos import gds_fast


class FastGdsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(
            graph=None, since_hours=2.0, since_days=7, memory="2GB",
            limit=10, count_only=False, keep=False,
        )

    def run_demo(self, statement) -> bool:
        driver = MagicMock()
        driver_context = MagicMock()
        driver_context.__enter__.return_value = driver
        max_date = dt.datetime(2024, 3, 30, 23, 58, tzinfo=dt.UTC)
        with (
            patch.object(gds_fast, "load_connection", return_value=("neo4j+s://test", ("u", "p"))),
            patch.object(gds_fast.GraphDatabase, "driver", return_value=driver_context),
            patch.object(gds_fast, "data_max_dates", return_value=(
                SimpleNamespace(to_native=lambda: max_date), None)),
            patch.object(gds_fast, "run_statement", side_effect=statement),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return gds_fast.run_gds(self.args)

    def test_transient_projection_errors_retry_with_new_name(self) -> None:
        errors = (
            "Session `SessionId[value=x]` not found",
            (
                "Request failed with status code 409. A graph mapping for graph "
                "`account_transfers_recent` already exists"
            ),
        )
        for message in errors:
            with self.subTest(message=message):
                projected = []
                dropped = []

                def statement(_driver, _label, cypher, params, *,
                              message=message, projected=projected,
                              dropped=dropped, **_kwargs):
                    if cypher == gds_fast.COUNT_WINDOW:
                        return [{"edges": 298}]
                    if cypher == gds_fast.PROJECT_WINDOW:
                        projected.append(params["graph"])
                        if len(projected) == 1:
                            raise Neo4jError._hydrate_neo4j(
                                code="Neo.ClientError.Procedure.ProcedureCallFailed",
                                message=message,
                            )
                        return [{"result": {"nodeCount": 556}}]
                    if cypher == gds_fast.PAGERANK_STREAM:
                        self.assertEqual(params["graph"], projected[1])
                        return [{"nodeId": 2, "score": 0.1}]
                    if cypher == gds_fast.DROP_GRAPH:
                        dropped.append(params["graph"])
                        return []
                    self.fail(f"unexpected statement: {cypher}")

                self.assertTrue(self.run_demo(statement))
                self.assertEqual(len(projected), 2)
                self.assertNotEqual(projected[0], projected[1])
                self.assertTrue(all(name.startswith("account_transfers_recent_")
                                    for name in projected))
                self.assertEqual(dropped, projected)

    def test_explicit_name_does_not_retry_mapping_conflict(self) -> None:
        self.args.graph = "chosen_graph"
        projected = []

        def statement(_driver, _label, cypher, params, **_kwargs):
            if cypher == gds_fast.COUNT_WINDOW:
                return [{"edges": 298}]
            if cypher == gds_fast.DROP_GRAPH:
                return []
            if cypher == gds_fast.PROJECT_WINDOW:
                projected.append(params["graph"])
                raise Neo4jError._hydrate_neo4j(
                    code="Neo.ClientError.Procedure.ProcedureCallFailed",
                    message="Request failed with status code 409. A graph mapping "
                    "for graph `chosen_graph` already exists",
                )
            self.fail(f"unexpected statement: {cypher}")

        self.assertFalse(self.run_demo(statement))
        self.assertEqual(projected, ["chosen_graph"])

    def test_repeated_session_failure_stops_after_one_retry(self) -> None:
        projected = []
        dropped = []

        def statement(_driver, _label, cypher, params, **_kwargs):
            if cypher == gds_fast.COUNT_WINDOW:
                return [{"edges": 298}]
            if cypher == gds_fast.PROJECT_WINDOW:
                projected.append(params["graph"])
                raise Neo4jError._hydrate_neo4j(
                    code="Neo.ClientError.Procedure.ProcedureCallFailed",
                    message="Session `SessionId[value=x]` not found",
                )
            if cypher == gds_fast.DROP_GRAPH:
                dropped.append(params["graph"])
                return []
            self.fail(f"unexpected statement: {cypher}")

        self.assertFalse(self.run_demo(statement))
        self.assertEqual(len(projected), 2)
        self.assertEqual(dropped, projected)

    def test_failed_pagerank_drops_projection(self) -> None:
        dropped = []
        projected = []

        def statement(_driver, _label, cypher, params, **_kwargs):
            if cypher == gds_fast.COUNT_WINDOW:
                return [{"edges": 298}]
            if cypher == gds_fast.PROJECT_WINDOW:
                projected.append(params["graph"])
                return [{"result": {"nodeCount": 556}}]
            if cypher == gds_fast.PAGERANK_STREAM:
                return None
            if cypher == gds_fast.DROP_GRAPH:
                dropped.append(params["graph"])
                return []
            self.fail(f"unexpected statement: {cypher}")

        self.assertFalse(self.run_demo(statement))
        self.assertEqual(dropped, projected)


if __name__ == "__main__":
    unittest.main()
