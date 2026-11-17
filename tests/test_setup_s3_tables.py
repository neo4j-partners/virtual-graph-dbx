"""Unit tests for aws/setup_s3_tables.py that need no AWS access.

The script declares its own dependencies, which the project venv lacks, so these tests
skip in the default run. Run them with:

    uv run --with boto3 --with 'pyiceberg[pyarrow]>=0.9' \
        python -m unittest tests.test_setup_s3_tables -v
"""

from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "aws" / "setup_s3_tables.py"
HAS_DEPS = all(
    importlib.util.find_spec(name) is not None for name in ("boto3", "pyiceberg")
)

ACCOUNT = "123456789012"
SSO_ROLE = "AWSReservedSSO_Admin_0123abcd"
SSO_ROLE_ARN = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/{SSO_ROLE}"
)
SSO_CALLER = f"arn:aws:sts::{ACCOUNT}:assumed-role/{SSO_ROLE}/someone@example.com"
USER_CALLER = f"arn:aws:iam::{ACCOUNT}:user/deployer"
ROOT_CALLER = f"arn:aws:iam::{ACCOUNT}:root"


def load_script():
    spec = importlib.util.spec_from_file_location("setup_s3_tables", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIam:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get_role(self, RoleName: str) -> dict:
        self.requested.append(RoleName)
        return {"Role": {"Arn": SSO_ROLE_ARN}}


class FakeSession:
    def __init__(self) -> None:
        self.iam = FakeIam()

    def client(self, name: str, **_: object) -> FakeIam:
        assert name == "iam", name
        return self.iam


@unittest.skipUnless(HAS_DEPS, "needs boto3 and pyiceberg, see the module docstring")
class SetupS3TablesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_script()

    def settings(self, **overrides: object):
        values = {
            "session": FakeSession(),
            "region": "us-west-2",
            "account_id": ACCOUNT,
            "principal_arn": SSO_ROLE_ARN,
            "bucket": "finance-genie",
            "namespace": "finance_genie",
            "role_name": "aura-virtual-graph-read",
            "data_source_name": "finance-genie-s3tables",
            "output": REPO_ROOT / ".env.aws",
        }
        values.update(overrides)
        return self.mod.Settings(**values)

    def test_caller_role_name(self) -> None:
        self.assertEqual(self.mod.caller_role_name(SSO_CALLER), SSO_ROLE)
        self.assertIsNone(self.mod.caller_role_name(USER_CALLER))
        self.assertIsNone(self.mod.caller_role_name(ROOT_CALLER))

    def test_principal_arn_reads_the_role_path_from_iam(self) -> None:
        session = FakeSession()
        self.assertEqual(self.mod.principal_arn(session, SSO_CALLER), SSO_ROLE_ARN)
        self.assertEqual(session.iam.requested, [SSO_ROLE])

    def test_principal_arn_keeps_a_user_arn(self) -> None:
        session = FakeSession()
        self.assertEqual(self.mod.principal_arn(session, USER_CALLER), USER_CALLER)
        self.assertEqual(session.iam.requested, [])

    def test_root_credentials_exit_early(self) -> None:
        sts = mock.MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT, "Arn": ROOT_CALLER}
        session = mock.MagicMock(region_name="us-west-2")
        session.client.return_value = sts
        args = mock.MagicMock(profile=None, region=None,
                              output=REPO_ROOT / ".env.aws")
        with mock.patch.object(self.mod.boto3, "Session", return_value=session), \
                self.assertRaises(SystemExit) as cm:
            self.mod.build_settings(args)
        self.assertIn("root", str(cm.exception.code))

    def test_verify_retries_a_denied_read(self) -> None:
        key = self.mod.AccessKey("AKIAEXAMPLE", "secret", "token")
        denied = OSError("AWS Error ACCESS_DENIED during GetObject operation")
        unmapped = OSError("AWS Error UNKNOWN (HTTP status 403) during GetObject")
        with mock.patch.object(self.mod, "read_with_key",
                               side_effect=[denied, unmapped, None]) as read, \
                mock.patch.object(self.mod.time, "sleep"), \
                mock.patch("builtins.print"):
            self.mod.verify_read_only_key(self.settings(), key)
        self.assertEqual(read.call_count, 3)

    def test_verify_raises_other_errors_at_once(self) -> None:
        key = self.mod.AccessKey("AKIAEXAMPLE", "secret", "token")
        with mock.patch.object(self.mod, "read_with_key",
                               side_effect=OSError("connection reset")) as read, \
                self.assertRaises(OSError):
            self.mod.verify_read_only_key(self.settings(), key)
        self.assertEqual(read.call_count, 1)

    def test_trust_policy_allows_only_the_caller(self) -> None:
        policy = json.loads(self.mod.trust_policy(self.settings()))
        (statement,) = policy["Statement"]
        self.assertEqual(statement["Action"], "sts:AssumeRole")
        self.assertEqual(
            statement["Condition"],
            {"ArnEquals": {"aws:PrincipalArn": SSO_ROLE_ARN}},
        )

    def test_catalog_sets_client_region_with_and_without_a_key(self) -> None:
        key = self.mod.AccessKey("AKIAEXAMPLE", "secret", "token")
        for passed in (None, key):
            with self.subTest(key=passed), mock.patch.object(
                self.mod, "load_catalog"
            ) as load:
                self.mod.s3tables_catalog(self.settings(), passed)
                properties = load.call_args.kwargs
                self.assertEqual(properties["client.region"], "us-west-2")
                self.assertEqual(properties["rest.signing-region"], "us-west-2")

    def test_write_aura_values_is_owner_only(self) -> None:
        key = self.mod.AccessKey(
            "AKIAEXAMPLE", "secret", "token", datetime(2026, 1, 1, tzinfo=UTC)
        )
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / ".env.aws"
            self.mod.write_aura_values(self.settings(output=fresh), key)
            self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)
            self.assertIn("AURA_AWS_SECRET=secret\n", fresh.read_text())

            stale = Path(tmp) / ".env.old"
            stale.write_text(
                "# Credentials expire 2025-01-01 00:00 UTC.\n"
                "AURA_AWS_SECRET=STALE-SECRET\n"
                "NEO4J_URI=neo4j+s://example.invalid\n"
            )
            stale.chmod(0o644)
            self.mod.write_aura_values(self.settings(output=stale), key)
            self.assertEqual(stat.S_IMODE(stale.stat().st_mode), 0o600)
            text = stale.read_text()
            self.assertNotIn("STALE-SECRET", text)
            self.assertNotIn("2025-01-01", text)
            self.assertEqual(text.count("AURA_AWS_SECRET="), 1)
            self.assertTrue(text.endswith("NEO4J_URI=neo4j+s://example.invalid\n"))

    def test_check_output_ignored(self) -> None:
        self.mod.check_output_ignored(REPO_ROOT / ".env.aws")
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.check_output_ignored(Path(tmp) / "aura.env")
        with self.assertRaises(SystemExit):
            self.mod.check_output_ignored(REPO_ROOT / "aura-values.txt")

    def test_add_epoch_seconds_appends_whole_seconds(self) -> None:
        pa = self.mod.pa
        stamps = [datetime(2024, 1, 26, 10, 20, tzinfo=UTC), None]
        links = pa.table({
            "link_id": pa.array([1, 2], pa.int64()),
            "transfer_timestamp": pa.array(stamps, pa.timestamp("us", tz="UTC")),
        })
        out = self.mod.add_epoch_seconds(links)
        self.assertEqual(out.column_names[-1], "transfer_epoch_seconds")
        self.assertEqual(out.schema.field("transfer_epoch_seconds").type, pa.int64())
        self.assertEqual(out.column("transfer_epoch_seconds").to_pylist(),
                         [1706264400, None])
        accounts = pa.table({"account_id": pa.array([1], pa.int64())})
        self.assertEqual(self.mod.add_epoch_seconds(accounts).column_names,
                         ["account_id"])


if __name__ == "__main__":
    unittest.main()
