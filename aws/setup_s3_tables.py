# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3", "pyiceberg[pyarrow]>=0.9"]
# ///
"""Set up Finance Genie on Amazon S3 Tables for a Neo4j Virtual Graph.

Runs steps 1 to 3 of docs/setup-aws.md:

1. Create the S3 table bucket and namespace.
2. Load the Finance Genie CSVs as Iceberg tables.
3. Create a read-only IAM role and assume it for temporary Aura credentials.

It then checks that the role's credentials can list and read the tables, and writes
the values for the Aura data source form to a gitignored dotenv file (.env.aws).

Step 3 uses a role, not an IAM user, because some AWS organizations block
iam:CreateUser. Role credentials are temporary. Assumed from an SSO session they
last at most one hour. Refresh them with --creds-only, then paste the new key,
secret, and session token into the Aura data source.

Every step is safe to re-run. The tables are purged and reloaded on each full run.

    uv run aws/setup_s3_tables.py --profile default
    uv run aws/setup_s3_tables.py --profile default --creds-only
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.exceptions import ForbiddenError, NoSuchTableError

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = (
    "https://raw.githubusercontent.com/neo4j-partners/"
    "graph-on-databricks/main/finance-genie/data"
)
POLICY_NAME = "aura-s3tables-read"
POLICY_FILE = Path(__file__).resolve().parent / f"{POLICY_NAME}.json"
SESSION_SECONDS = 3600  # the cap for a role assumed from another role, such as SSO

TS = pa.timestamp("us")
SCHEMAS = {
    "accounts": pa.schema(
        [
            ("account_id", pa.int64()),
            ("account_hash", pa.string()),
            ("account_name", pa.string()),
            ("account_type", pa.string()),
            ("region", pa.string()),
            ("balance", pa.float64()),
            ("opened_date", pa.date32()),
            ("holder_age", pa.int32()),
        ]
    ),
    "merchants": pa.schema(
        [
            ("merchant_id", pa.int64()),
            ("merchant_name", pa.string()),
            ("category", pa.string()),
            ("region", pa.string()),
        ]
    ),
    "transactions": pa.schema(
        [
            ("txn_id", pa.int64()),
            ("account_id", pa.int64()),
            ("merchant_id", pa.int64()),
            ("amount", pa.float64()),
            ("txn_timestamp", TS),
            ("txn_hour", pa.int32()),
        ]
    ),
    "account_links": pa.schema(
        [
            ("link_id", pa.int64()),
            ("src_account_id", pa.int64()),
            ("dst_account_id", pa.int64()),
            ("amount", pa.float64()),
            ("transfer_timestamp", TS),
        ]
    ),
    "account_labels": pa.schema(
        [
            ("account_id", pa.int64()),
            ("is_fraud", pa.string()),
        ]
    ),
}


@dataclass
class Settings:
    session: boto3.Session
    region: str
    account_id: str
    principal_arn: str
    bucket: str
    namespace: str
    role_name: str
    data_source_name: str
    output: Path

    @property
    def bucket_arn(self) -> str:
        return f"arn:aws:s3tables:{self.region}:{self.account_id}:bucket/{self.bucket}"


@dataclass
class AccessKey:
    key_id: str
    secret: str
    session_token: str | None = None
    expiration: datetime | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="AWS CLI profile (default: default chain)")
    parser.add_argument("--region", help="AWS region (default: profile region)")
    parser.add_argument("--bucket", default="finance-genie", help="table bucket name")
    parser.add_argument("--namespace", default="finance_genie", help="namespace")
    parser.add_argument(
        "--role-name",
        default="aura-virtual-graph-read",
        help="read-only IAM role assumed for the Aura credentials",
    )
    parser.add_argument(
        "--creds-only",
        action="store_true",
        help="skip steps 1 and 2, only refresh the Aura credentials",
    )
    parser.add_argument(
        "--data-source-name",
        default="finance-genie-s3tables",
        help="name to enter for the Aura data source",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".env.aws",
        help="dotenv file for the Aura form values (default: .env.aws)",
    )
    return parser.parse_args()


def caller_role_name(caller_arn: str) -> str | None:
    """Return the role name when ``caller_arn`` is an assumed-role session.

    An SSO or other role session looks like
    ``arn:aws:sts::<account>:assumed-role/<role>/<session>``. IAM users and the root
    user return None, because their caller ARN is already the principal ARN.
    """
    resource = caller_arn.split(":", 5)[5]
    kind, _, rest = resource.partition("/")
    return rest.split("/", 1)[0] if kind == "assumed-role" else None


def principal_arn(session: boto3.Session, caller_arn: str) -> str:
    """Return the IAM ARN that ``aws:PrincipalArn`` holds for the caller.

    For a role session that is the role ARN with its path, such as
    ``role/aws-reserved/sso.amazonaws.com/...`` for SSO, so it is read from IAM.
    """
    role_name = caller_role_name(caller_arn)
    if role_name is None:
        return caller_arn
    return session.client("iam").get_role(RoleName=role_name)["Role"]["Arn"]


def check_output_ignored(output: Path) -> None:
    """Refuse to write the secret to a file inside the repo that git would track."""
    path = output.resolve()
    if not path.is_relative_to(REPO_ROOT):
        return
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(path)],
            check=False,
        )
    except FileNotFoundError:
        print("  git not found, cannot check that --output is gitignored")
        return
    if result.returncode == 1:
        raise SystemExit(
            f"--output {output} is inside the repo and not gitignored. "
            "Use a .env.* name or a path outside the repo."
        )


def build_settings(args: argparse.Namespace) -> Settings:
    check_output_ignored(args.output)
    if args.profile:
        # pyarrow's S3 filesystem reads the profile from the environment.
        os.environ["AWS_PROFILE"] = args.profile
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = session.region_name or "us-east-1"
    identity = session.client("sts").get_caller_identity()
    if identity["Arn"].endswith(":root"):
        sys.exit("These are root account credentials. AWS does not let the root "
                 "account assume a role, so run this as an IAM user or an SSO role.")
    return Settings(
        session=session,
        region=region,
        account_id=identity["Account"],
        principal_arn=principal_arn(session, identity["Arn"]),
        bucket=args.bucket,
        namespace=args.namespace,
        role_name=args.role_name,
        data_source_name=args.data_source_name,
        output=args.output,
    )


# ── Step 1: table bucket and namespace ──────────────────────────────────────


def ensure_bucket_and_namespace(s: Settings) -> None:
    client = s.session.client("s3tables", region_name=s.region)

    buckets = client.list_table_buckets(prefix=s.bucket)["tableBuckets"]
    if any(b["name"] == s.bucket for b in buckets):
        print(f"  table bucket exists: {s.bucket_arn}")
    else:
        client.create_table_bucket(name=s.bucket)
        print(f"  created table bucket: {s.bucket_arn}")

    try:
        client.get_namespace(tableBucketARN=s.bucket_arn, namespace=s.namespace)
        print(f"  namespace exists: {s.namespace}")
    except client.exceptions.NotFoundException:
        client.create_namespace(tableBucketARN=s.bucket_arn, namespace=[s.namespace])
        print(f"  created namespace: {s.namespace}")


# ── Step 2: load the tables ─────────────────────────────────────────────────


def s3tables_catalog(s: Settings, key: AccessKey | None = None) -> Catalog:
    properties = {
        "type": "rest",
        "warehouse": s.bucket_arn,
        "uri": f"https://s3tables.{s.region}.amazonaws.com/iceberg",
        "rest.sigv4-enabled": "true",
        "rest.signing-name": "s3tables",
        "rest.signing-region": s.region,
        # The data files live in the table bucket's region, whatever the profile says.
        "client.region": s.region,
    }
    if key is not None:
        # client.* keys sign the REST calls and read the data files.
        properties |= {
            "client.access-key-id": key.key_id,
            "client.secret-access-key": key.secret,
        }
        if key.session_token:
            properties["client.session-token"] = key.session_token
    return load_catalog("s3tables", **properties)


def read_csv(name: str, schema: pa.Schema) -> pa.Table:
    with urllib.request.urlopen(f"{BASE_URL}/{name}.csv") as response:
        raw = response.read()
    options = pacsv.ConvertOptions(column_types=schema)
    table = pacsv.read_csv(io.BytesIO(raw), convert_options=options)
    return table.select(schema.names)


def to_utc(table: pa.Table) -> pa.Table:
    """Store timestamps as Iceberg timestamptz, matching Databricks TIMESTAMP."""
    for i, field in enumerate(table.schema):
        if pa.types.is_timestamp(field.type):
            utc = table.column(i).cast(pa.timestamp("us", tz="UTC"))
            table = table.set_column(i, field.name, utc)
    return table


def fix_labels(table: pa.Table) -> pa.Table:
    """Turn the CSV's True/False strings into a boolean column."""
    flag = pc.equal(pc.utf8_lower(table.column("is_fraud")), "true")
    return table.set_column(1, "is_fraud", flag)


def load_tables(s: Settings) -> None:
    catalog = s3tables_catalog(s)
    for name, schema in SCHEMAS.items():
        table = to_utc(read_csv(name, schema))
        if name == "account_labels":
            table = fix_labels(table)

        identifier = f"{s.namespace}.{name}"
        try:
            catalog.purge_table(identifier)
        except NoSuchTableError:
            print(f"  {name}: new table")
        iceberg_table = catalog.create_table(identifier, schema=table.schema)
        iceberg_table.append(table)
        print(f"  {name:<16} {table.num_rows:>8,} rows")


# ── Step 3: read-only IAM role and temporary credentials ───────────────────


def read_policy(s: Settings) -> str:
    """Fill the placeholders in aura-s3tables-read.json for this table bucket."""
    text = (
        POLICY_FILE.read_text()
        .replace("<region>", s.region)
        .replace("<account-id>", s.account_id)
        .replace("<table-bucket>", s.bucket)
    )
    return json.dumps(json.loads(text))


def trust_policy(s: Settings) -> str:
    """Let only the identity that runs this script assume the role.

    The account is the principal, and the condition narrows it to one ARN. Naming the
    ARN in a condition, not as the principal, keeps working when AWS re-creates an
    SSO role with the same name.
    """
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{s.account_id}:root"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"ArnEquals": {"aws:PrincipalArn": s.principal_arn}},
                }
            ],
        }
    )


def ensure_read_role(s: Settings) -> str:
    iam = s.session.client("iam")
    try:
        role_arn = iam.get_role(RoleName=s.role_name)["Role"]["Arn"]
        print(f"  IAM role exists: {role_arn}")
        iam.update_assume_role_policy(
            RoleName=s.role_name, PolicyDocument=trust_policy(s)
        )
        print(f"  updated trust policy: only {s.principal_arn} can assume it")
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=s.role_name,
            AssumeRolePolicyDocument=trust_policy(s),
            Description="Aura read-only access to the Finance Genie S3 table bucket",
        )["Role"]["Arn"]
        print(f"  created IAM role: {role_arn}")
        print(f"  trust policy: only {s.principal_arn} can assume it")

    iam.put_role_policy(
        RoleName=s.role_name, PolicyName=POLICY_NAME, PolicyDocument=read_policy(s)
    )
    print(f"  wrote inline policy {POLICY_NAME}, replacing any earlier version")
    return role_arn


def assume_read_role(s: Settings, role_arn: str, attempts: int = 12) -> AccessKey:
    """Assume the role. A new role or trust policy takes a few seconds to apply."""
    sts = s.session.client("sts")
    for attempt in range(1, attempts + 1):
        try:
            creds = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName="aura-virtual-graph",
                DurationSeconds=SESSION_SECONDS,
            )["Credentials"]
        except sts.exceptions.ClientError as error:
            if attempt == attempts or error.response["Error"]["Code"] != "AccessDenied":
                raise
            print("  role not assumable yet, retrying in 5s")
            time.sleep(5)
            continue
        expires = f"{creds['Expiration']:%Y-%m-%d %H:%M %Z}"
        print(f"  assumed role, credentials expire {expires}")
        return AccessKey(
            key_id=creds["AccessKeyId"],
            secret=creds["SecretAccessKey"],
            session_token=creds["SessionToken"],
            expiration=creds["Expiration"],
        )
    raise RuntimeError("unreachable")


# ── Verify and write the Aura values ────────────────────────────────────────


def verify_read_only_key(s: Settings, key: AccessKey, attempts: int = 6) -> None:
    """List and read the tables with the credentials Aura will use.

    A new or changed inline policy can take a few seconds to apply after the role is
    assumable, so a denied read is retried.
    """
    for attempt in range(1, attempts + 1):
        try:
            read_with_key(s, key)
            return
        except (ForbiddenError, OSError) as error:
            if attempt == attempts or not is_denied(error):
                raise
            print("  read denied, the role policy may still be applying. "
                  "Retrying in 5s")
            time.sleep(5)


def is_denied(error: Exception) -> bool:
    """True for a 403 from the S3 Tables catalog or from the S3 data read.

    Arrow names a mapped S3 denial ``ACCESS_DENIED`` and reports an unmapped one by
    its HTTP status.
    """
    return isinstance(error, ForbiddenError) or any(
        marker in str(error) for marker in ("ACCESS_DENIED", "HTTP status 403"))


def read_with_key(s: Settings, key: AccessKey) -> None:
    catalog = s3tables_catalog(s, key)
    names = sorted(name for _, name in catalog.list_tables(s.namespace))
    sample = catalog.load_table(f"{s.namespace}.accounts").scan(limit=1)
    rows = sample.to_arrow().num_rows
    print(f"  read-only credentials list {len(names)} tables: {', '.join(names)}")
    print(f"  read-only credentials read {rows} row from accounts")


def write_aura_values(s: Settings, key: AccessKey) -> None:
    expires = f"{key.expiration:%Y-%m-%d %H:%M %Z}" if key.expiration else "never"
    # Create the file owner-only before the secret goes in. The mode argument applies
    # only to a new file, so fchmod also tightens a file left by an earlier run.
    fd = os.open(s.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(
            "# Values for the Aura Virtual Graph data source form (S3 table bucket).\n"
            "# Written by aws/setup_s3_tables.py. Holds a secret: do not commit.\n"
            f"# Credentials expire {expires}. Refresh with --creds-only.\n"
            f"AURA_DATA_SOURCE_NAME={s.data_source_name}\n"
            f"AURA_TABLE_BUCKET_ARN={s.bucket_arn}\n"
            f"AURA_NAMESPACE={s.namespace}\n"
            f"AURA_AWS_KEY={key.key_id}\n"
            f"AURA_AWS_SECRET={key.secret}\n"
            f"AURA_AWS_SESSION_TOKEN={key.session_token or ''}\n"
            f"AURA_AWS_REGION={s.region}\n"
        )


def main() -> None:
    args = parse_args()
    settings = build_settings(args)
    print(f"Account {settings.account_id}, region {settings.region}")

    if not args.creds_only:
        print("Step 1: table bucket and namespace")
        ensure_bucket_and_namespace(settings)

        print("Step 2: load Finance Genie tables")
        load_tables(settings)

    print("Step 3: read-only IAM role and temporary credentials")
    role_arn = ensure_read_role(settings)
    key = assume_read_role(settings, role_arn)
    write_aura_values(settings, key)

    print("Verify: read the tables with the read-only credentials")
    verify_read_only_key(settings, key)

    print(f"\nAura form values written to {settings.output}")
    print(f"  Name              {settings.data_source_name}")
    print(f"  Table Bucket ARN  {settings.bucket_arn}")
    print(f"  Namespace         {settings.namespace}")
    print(f"  AWS Key           {key.key_id}")
    print(f"  AWS Secret        (in {settings.output.name})")
    print(f"  AWS Session Token (in {settings.output.name})")
    print(f"  AWS Region        {settings.region}")


if __name__ == "__main__":
    main()
