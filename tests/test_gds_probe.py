"""Behavior checks for projection cleanup and property picks in the GDS probe."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import unittest
from unittest.mock import MagicMock, patch

from neo4j.exceptions import DriverError, Neo4jError

from demos import gds_common, gds_probe

SINCE = dt.datetime(2024, 3, 23, 23, 58, tzinfo=dt.UTC)


def procedure_error(message: str) -> Neo4jError:
    """Build a server-side procedure failure carrying ``message``."""
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Procedure.ProcedureCallFailed", message=message)


class PickPropsTests(unittest.TestCase):
    def test_skips_identifier_keys(self) -> None:
        props = {"account_id": 7, "account_name": "x", "balance": 12.5,
                 "holder_age": 40}
        self.assertEqual(gds_probe._pick_props(props), ("balance", "account_name"))

    def test_returns_none_when_only_identifiers_are_numeric(self) -> None:
        props = {"account_id": 7, "id": 1, "flag": True, "name": "x"}
        self.assertEqual(gds_probe._pick_props(props), (None, "name"))


class RunScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(graph="account_transfers_recent", memory="2GB")
        self.projected: list[str] = []
        self.dropped: list[str] = []

    def run_scenario(self, scenario: gds_probe.Scenario,
                     project_errors: list[Exception]) -> tuple[str, str, str]:
        """Run one scenario; each projection raises the next queued error, if any."""
        errors = list(project_errors)

        def statement(_driver, _label, cypher, params, **_kwargs):
            if cypher == gds_probe.DROP_GRAPH:
                self.dropped.append(params["graph"])
                return []
            if "gds.graph.project" in cypher:
                self.projected.append(params["graph"])
                if errors:
                    raise errors.pop(0)
                return [{"result": {"nodeCount": 5, "relationshipCount": 4,
                                    "projectMillis": 1}}]
            if "gds.pageRank.stream" in cypher:
                return [{"nodeId": 2, "score": 0.1}]
            self.fail(f"unexpected statement: {cypher}")

        with (
            patch.object(gds_probe, "run_statement", side_effect=statement),
            patch.object(gds_common, "run_statement", side_effect=statement),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return gds_probe._run_scenario(MagicMock(), self.args, SINCE, scenario)

    def control(self) -> gds_probe.Scenario:
        return gds_probe._build_scenarios(None, None)[0]

    def test_success_uses_unique_name_and_drops_it(self) -> None:
        _, status, _ = self.run_scenario(self.control(), [])
        self.assertEqual(status, "OK")
        self.assertEqual(len(self.projected), 1)
        self.assertTrue(self.projected[0].startswith("account_transfers_recent_"))
        self.assertEqual(self.dropped, self.projected)

    def test_scenarios_never_share_a_name(self) -> None:
        self.run_scenario(self.control(), [])
        self.run_scenario(self.control(), [])
        self.assertNotEqual(self.projected[0], self.projected[1])

    def test_failed_projection_drops_graph(self) -> None:
        for exc in (DriverError("read timed out"), procedure_error("bad config")):
            with self.subTest(exc=type(exc).__name__):
                self.projected.clear()
                self.dropped.clear()
                _, status, _ = self.run_scenario(self.control(), [exc])
                self.assertEqual(status, "FAIL")
                self.assertEqual(len(self.projected), 1)
                self.assertEqual(self.dropped, self.projected)

    def test_session_failure_retries_once_under_new_name(self) -> None:
        error = procedure_error("Session `SessionId[value=x]` not found")
        _, status, _ = self.run_scenario(self.control(), [error])
        self.assertEqual(status, "OK")
        self.assertEqual(len(self.projected), 2)
        self.assertNotEqual(self.projected[0], self.projected[1])
        self.assertEqual(self.dropped, self.projected)

    def test_repeated_session_failure_stops_after_one_retry(self) -> None:
        error = procedure_error("Session `SessionId[value=x]` not found")
        _, status, _ = self.run_scenario(self.control(), [error, error])
        self.assertEqual(status, "FAIL")
        self.assertEqual(len(self.projected), 2)
        self.assertEqual(self.dropped, self.projected)

    def test_scenario_without_config_raises(self) -> None:
        scenario = gds_probe.Scenario("X", "no config", None)
        with self.assertRaises(ValueError):
            self.run_scenario(scenario, [])


if __name__ == "__main__":
    unittest.main()
