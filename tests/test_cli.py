"""Unit tests for cli.py, connection.py and probe.py. None needs a live connection."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from neo4j.exceptions import Neo4jError

import cli
import connection
import probe


def quiet() -> contextlib.redirect_stdout:
    return contextlib.redirect_stdout(io.StringIO())


class CliParseTests(unittest.TestCase):
    def parse(self, *argv: str) -> argparse.Namespace:
        with patch.object(sys, "argv", ["vg-demo", *argv]):
            return cli.parse_args()

    def assert_exit_2(self, *argv: str) -> str:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            self.parse(*argv)
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_defaults_match_docs(self) -> None:
        a = self.parse()
        self.assertEqual(a.demo, "fraud")
        self.assertEqual((a.rows, a.timeout), (10, 300.0))
        self.assertEqual((a.since_days, a.since_hours, a.memory, a.limit),
                         (7, None, "2GB", 10))
        self.assertEqual((a.graph, a.count_only, a.keep), (None, False, False))
        self.assertEqual((a.history_lag_minutes, a.poll_minutes, a.max_wait_minutes),
                         (3.0, 3.0, 40.0))
        self.assertEqual((a.build, a.sizes, a.skip_history, a.profile, a.warehouse),
                         (False, None, False, None, None))

    def test_each_demo(self) -> None:
        for demo in ("fraud", "basic", "fast-gds", "gds-probe", "100m"):
            self.assertEqual(self.parse("--demo", demo).demo, demo)

    def test_fraud_flags(self) -> None:
        a = self.parse("--query", "5", "--rows", "3", "--timeout", "60")
        self.assertEqual((a.query, a.rows, a.timeout), (5, 3, 60.0))
        self.assertEqual(self.parse("--only", "5", "6").only, [5, 6])

    def test_gds_flags(self) -> None:
        a = self.parse("--demo", "fast-gds", "--since-hours", "2", "--count-only",
                       "--limit", "25", "--keep", "--graph", "g", "--memory", "4GB")
        self.assertEqual((a.since_hours, a.count_only, a.limit, a.keep, a.graph,
                          a.memory), (2.0, True, 25, True, "g", "4GB"))

    def test_spike_flags(self) -> None:
        a = self.parse("--demo", "100m", "--build", "--sizes", "1000000",
                       "100000000", "--skip-history")
        self.assertEqual((a.build, a.sizes, a.skip_history),
                         (True, [1000000, 100000000], True))

    def test_bad_values_exit_2(self) -> None:
        for argv in (("--demo", "x"), ("--rows", "a"), ("--only",)):
            with self.subTest(argv=argv):
                self.assert_exit_2(*argv)

    def test_query_and_only_are_exclusive(self) -> None:
        err = self.assert_exit_2("--query", "1", "--only", "2")
        self.assertIn("not allowed with argument", err)

    def warnings_for(self, *argv: str) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.parse(*argv)
        return err.getvalue()

    def test_ignored_flags_warn(self) -> None:
        cases = {
            ("--demo", "fast-gds", "--rows", "5"): "--rows has no effect with --demo "
                                                   "fast-gds",
            ("--demo", "gds-probe", "--keep"): "--keep has no effect",
            ("--demo", "gds-probe", "--limit", "5"): "--limit has no effect",
            ("--demo", "basic", "--query", "3"): "--query has no effect",
            ("--since-days", "3",): "--since-days has no effect with --demo fraud",
            ("--demo", "100m", "--sizes", "1000"): "--sizes has no effect without "
                                                  "--build",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertIn(expected, self.warnings_for(*argv))

    def test_flags_the_demo_reads_do_not_warn(self) -> None:
        for argv in ((), ("--rows", "3", "--only", "1", "2"),
                     ("--demo", "basic", "--rows", "3", "--timeout", "60"),
                     ("--demo", "fast-gds", "--keep", "--limit", "5",
                      "--since-hours", "2"),
                     ("--demo", "gds-probe", "--since-days", "3", "--count-only"),
                     ("--demo", "100m", "--build", "--sizes", "1000",
                      "--warehouse", "w")):
            with self.subTest(argv=argv):
                self.assertEqual(self.warnings_for(*argv), "")

    def test_every_flag_has_a_demo_mapping(self) -> None:
        dests = {a.dest for a in cli.build_parser()._actions
                 if a.dest not in ("help", "demo")}
        self.assertEqual(dests, set(cli.FLAG_DEMOS))

    def test_rows_zero_allowed(self) -> None:
        self.assertEqual(self.parse("--rows", "0").rows, 0)

    def test_out_of_range_numbers_exit_2(self) -> None:
        for argv in (("--rows", "-1"), ("--timeout", "0"), ("--timeout", "inf"),
                     ("--since-hours", "-2"), ("--since-days", "0"),
                     ("--limit", "0"), ("--poll-minutes", "0"),
                     ("--history-lag-minutes", "-1"), ("--max-wait-minutes", "0"),
                     ("--sizes", "0"), ("--query", "0"), ("--only", "3", "-1")):
            with self.subTest(argv=argv):
                self.assert_exit_2(*argv)

    def test_help_has_no_emdash_or_eg(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            self.parse("--help")
        for banned in ("—", "–", "e.g."):
            self.assertNotIn(banned, out.getvalue())


class CliMainTests(unittest.TestCase):
    def main(self, *argv: str) -> None:
        with patch.object(sys, "argv", ["vg-demo", *argv]), quiet():
            cli.main()

    def assert_exit_1(self, *argv: str) -> None:
        with self.assertRaises(SystemExit) as cm:
            self.main(*argv)
        self.assertEqual(cm.exception.code, 1)

    def test_fast_gds_failure_exits_1(self) -> None:
        with patch.object(cli, "run_gds", return_value=False):
            self.assert_exit_1("--demo", "fast-gds")

    def test_fast_gds_success(self) -> None:
        with patch.object(cli, "run_gds", return_value=True):
            self.main("--demo", "fast-gds")

    def test_gds_probe_default_graph(self) -> None:
        with patch.object(cli, "run_probe", return_value=True) as rp:
            self.main("--demo", "gds-probe")
        self.assertEqual(rp.call_args.args[0].graph, "account_transfers_recent")

    def test_gds_probe_failure_exits_1(self) -> None:
        with patch.object(cli, "run_probe", return_value=False):
            self.assert_exit_1("--demo", "gds-probe")

    def test_100m_never_opens_bolt(self) -> None:
        with patch.object(cli, "run_spike", return_value=True) as rs, \
                patch.object(cli, "load_connection") as lc:
            self.main("--demo", "100m")
        rs.assert_called_once()
        lc.assert_not_called()

    def test_100m_failure_exits_1(self) -> None:
        with patch.object(cli, "run_spike", return_value=False):
            self.assert_exit_1("--demo", "100m")

    def driver_patches(self) -> tuple[MagicMock, contextlib.ExitStack]:
        driver_cm = MagicMock()
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(cli, "load_connection",
                                         return_value=("u", ("a", "b"))))
        stack.enter_context(patch.object(cli.GraphDatabase, "driver",
                                         return_value=driver_cm))
        return driver_cm.__enter__.return_value, stack

    def test_basic_and_fraud_dispatch(self) -> None:
        drv, stack = self.driver_patches()
        with (
            stack,
            patch.object(cli, "run_basic", return_value=True) as rb,
            patch.object(cli, "run_fraud", return_value=True) as rf,
        ):
            self.main("--demo", "basic", "--rows", "4", "--timeout", "5")
            rb.assert_called_once_with(drv, 4, 5.0)
            self.main("--query", "3")
            rf.assert_called_once()
            self.assertEqual([q.number for q in rf.call_args.args[2]], [3])
        drv.verify_connectivity.assert_called()

    def test_basic_and_fraud_failure_exit_1(self) -> None:
        for demo, target in (("basic", "run_basic"), ("fraud", "run_fraud")):
            _, stack = self.driver_patches()
            with self.subTest(demo=demo), stack, \
                    patch.object(cli, target, return_value=False):
                self.assert_exit_1("--demo", demo)

    def test_bad_query_number_fails_before_connecting(self) -> None:
        with patch.object(cli, "load_connection") as lc, \
                self.assertRaises(SystemExit) as cm:
            self.main("--query", "99")
        self.assertIn("No query numbered 99", str(cm.exception.code))
        lc.assert_not_called()


class ConnectionTests(unittest.TestCase):
    def envfile(self, text: str) -> Path:
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
            tmp.write(text)
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def clean_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items()
                if not k.startswith(("DATABRICKS_", "CATALOG", "SCHEMA", "VG_"))}

    def test_missing_file_exits(self) -> None:
        with self.assertRaises(SystemExit):
            connection._resolve_env_file(Path("/nonexistent/.env"))

    def test_probe_env_override(self) -> None:
        f = self.envfile("X=1\n")
        with patch.dict(os.environ, {"PROBE_ENV": str(f)}):
            self.assertEqual(connection._resolve_env_file(None), f)

    def test_load_connection_ok_and_missing(self) -> None:
        keys = ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
        with patch.dict(os.environ, {}, clear=False):
            for k in keys:
                os.environ.pop(k, None)
            f = self.envfile(
                "NEO4J_URI=bolt://x\nNEO4J_USERNAME=u\nNEO4J_PASSWORD=p\n")
            self.assertEqual(connection.load_connection(f), ("bolt://x", ("u", "p")))
            for k in keys:
                os.environ.pop(k, None)
            f2 = self.envfile("NEO4J_URI=bolt://x\n")
            with self.assertRaises(SystemExit) as cm:
                connection.load_connection(f2)
            self.assertIn("NEO4J_USERNAME, NEO4J_PASSWORD", str(cm.exception.code))

    def test_databricks_config_precedence_and_no_export(self) -> None:
        f = self.envfile("DATABRICKS_PROFILE=p2\nSCHEMA=s2\nDATABRICKS_HOST=h\n")
        with patch.dict(os.environ, self.clean_env(), clear=True):
            cfg = connection.load_databricks_config(f)
            self.assertEqual((cfg.profile, cfg.catalog, cfg.schema),
                             ("p2", "virtual-graph-dbx", "s2"))
            self.assertNotIn("DATABRICKS_HOST", os.environ)
            os.environ["DATABRICKS_CONFIG_PROFILE"] = "shell"
            self.assertEqual(connection.load_databricks_config(f).profile, "shell")

    def test_databricks_defaults(self) -> None:
        f = self.envfile("")
        with patch.dict(os.environ, self.clean_env(), clear=True):
            cfg = connection.load_databricks_config(f)
        self.assertEqual(cfg, connection.DatabricksConfig(
            "DEFAULT", "virtual-graph-dbx", "vg-schema", "b0fffb8e3255bf85"))

    def test_warehouse_override_from_env_file_and_shell(self) -> None:
        f = self.envfile("VG_BACKING_WAREHOUSE_ID=fromfile\n"
                         "DATABRICKS_WAREHOUSE_ID=other\n")
        with patch.dict(os.environ, self.clean_env(), clear=True):
            self.assertEqual(connection.load_databricks_config(f).warehouse,
                             "fromfile")
            os.environ["VG_BACKING_WAREHOUSE_ID"] = "fromshell"
            self.assertEqual(
                connection.load_databricks_config(self.envfile("")).warehouse,
                "fromshell")

    def test_databricks_warehouse_id_is_ignored(self) -> None:
        f = self.envfile("DATABRICKS_WAREHOUSE_ID=other\n")
        with patch.dict(os.environ, self.clean_env(), clear=True):
            self.assertEqual(connection.load_databricks_config(f).warehouse,
                             connection.VG_BACKING_WAREHOUSE)


class ProbeTests(unittest.TestCase):
    def run_main(self, execute_query: MagicMock) -> str:
        driver_cm = MagicMock()
        driver_cm.__enter__.return_value.execute_query = execute_query
        buf = io.StringIO()
        with (
            patch.object(sys, "argv", ["vg-probe", "RETURN 1"]),
            patch.object(probe, "load_connection", return_value=("u", ("a", "b"))),
            patch.object(probe.GraphDatabase, "driver", return_value=driver_cm),
            contextlib.redirect_stdout(buf),
        ):
            probe.main()
        return buf.getvalue()

    def test_parse_args(self) -> None:
        with patch.object(sys, "argv", ["vg-probe", "RETURN 1"]):
            self.assertEqual(probe.parse_args().cypher, "RETURN 1")

    def test_main(self) -> None:
        rec = MagicMock()
        rec.data.return_value = {"ok": 1}
        out = self.run_main(MagicMock(return_value=([rec], 0, 0)))
        self.assertIn("rows=1 sample={'ok': 1}", out)

    def test_cypher_error_is_one_clean_line(self) -> None:
        exc = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError", message="bad")
        with self.assertRaises(SystemExit) as cm:
            self.run_main(MagicMock(side_effect=exc))
        self.assertIn("Neo.ClientError.Statement.SyntaxError", str(cm.exception.code))
        self.assertIn("bad", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
