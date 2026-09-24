"""Unit tests for viz_check.py. None needs a live connection."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

from neo4j.exceptions import Neo4jError
from neo4j.time import Date, DateTime

import viz_check

PARAM_RE = re.compile(r"\$(\w+)")
MAX_TRANSFER = DateTime(2024, 3, 30, 23, 58, 0, tzinfo=dt.UTC)
MAX_OPENED = Date(2022, 12, 6)
MAX_DATES = [([{"mx": MAX_TRANSFER}], 0, 0), ([{"mx": MAX_OPENED}], 0, 0)]


class VizCheckTests(unittest.TestCase):
    def run_main(self, driver: MagicMock) -> str:
        driver_cm = MagicMock()
        driver_cm.__enter__.return_value = driver
        buf = io.StringIO()
        with (
            patch.object(sys, "argv", ["vg-viz"]),
            patch.object(viz_check, "load_connection",
                         return_value=("u", ("a", "b"))),
            patch.object(viz_check.GraphDatabase, "driver", return_value=driver_cm),
            contextlib.redirect_stdout(buf),
        ):
            viz_check.main()
        return buf.getvalue()

    def test_cutoff_derived_from_data(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = MAX_DATES
        self.assertEqual(viz_check.cutoff(driver), "2024-03-23T23:58:00Z")

    def test_main_flow_with_mock_driver(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [
            *MAX_DATES,
            ([{"recipient": 184, "senders": 24}], 0, 0),
            ([{"sender": 16570, "recipients": 21}], 0, 0),
            ([{"a_id": 7855, "b_id": 13727, "vol": 1.0}], 0, 0),
            ([], 0, 0), ([], 0, 0), ([], 0, 0),
        ]
        out = self.run_main(driver)
        self.assertIn("transfer_timestamp >= 2024-03-23T23:58:00Z", out)
        self.assertIn("VIZ round-trip @ 7855 <-> 13727", out)
        for call in driver.execute_query.call_args_list[2:]:
            cypher, kwargs = call.args[0], call.kwargs
            self.assertEqual(set(PARAM_RE.findall(cypher)), set(kwargs))
            if "since" in kwargs:
                self.assertEqual(kwargs["since"], "2024-03-23T23:58:00Z")

    def test_round_trip_finder_counts_scalar_link_ids(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [
            *MAX_DATES,
            ([{"recipient": 1, "senders": 2}], 0, 0),
            ([{"sender": 3, "recipients": 4}], 0, 0),
            ([{"a_id": 5, "b_id": 6, "vol": 1.0}], 0, 0),
            ([], 0, 0), ([], 0, 0), ([], 0, 0),
        ]
        self.run_main(driver)
        finder = driver.execute_query.call_args_list[4].args[0]
        self.assertIn("count(DISTINCT f.link_id) AS n_ab", finder)
        self.assertIn("count(DISTINCT g.link_id) AS n_ba", finder)
        self.assertNotIn("count(DISTINCT f)", finder)

    def test_empty_finder_exits_cleanly(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [*MAX_DATES, ([], 0, 0)]
        with self.assertRaises(SystemExit) as cm:
            self.run_main(driver)
        self.assertIn("the collection-account finder", str(cm.exception.code))

    def test_cypher_error_exits_cleanly(self) -> None:
        driver = MagicMock()
        driver.execute_query.side_effect = [
            *MAX_DATES,
            Neo4jError._hydrate_neo4j(
                code="Neo.ClientError.Statement.X", message="nope"),
        ]
        with self.assertRaises(SystemExit) as cm:
            self.run_main(driver)
        self.assertIn("Neo.ClientError.Statement.X", str(cm.exception.code))

    def test_help_does_not_hit_the_database(self) -> None:
        out = io.StringIO()
        with (
            patch.object(sys, "argv", ["vg-viz", "--help"]),
            patch.object(viz_check, "load_connection") as lc,
            self.assertRaises(SystemExit) as cm,
            contextlib.redirect_stdout(out),
        ):
            viz_check.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("usage: vg-viz", out.getvalue())
        lc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
