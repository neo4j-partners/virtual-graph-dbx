# Virtual Graph on AWS S3 Tables

This guide runs the Finance Genie Virtual Graph on AWS instead of Databricks. The data
lives in Apache Iceberg tables inside an Amazon S3 table bucket. Aura connects to the
table bucket directly, so no Databricks workspace or SQL warehouse is involved.

The graph model and every Cypher demo stay the same as the Databricks setup. Only the
data source changes. Steps 1 to 3 replace sections 1 to 3 of
[`setup-databricks.md`](setup-databricks.md). The Aura steps and the graph model are
shared with the Databricks setup in [`vg-model.md`](vg-model.md).

> **Status.** The Aura console offers an S3 table bucket data source with the fields
> shown in step 4. The [Virtual Graph data sources](https://neo4j.com/docs/virtual-graph/aura/data-sources/)
> page lists only BigQuery, Databricks, and Snowflake today. The
> [Aura Import AWS S3](https://neo4j.com/docs/aura/import/data-sources/#_aws_s3) section
> describes a different connector. That connector reads CSV and Parquet files from a
> regular S3 bucket for import. Treat this guide as a preview path, and check the
> official docs for updates.

## Files

| File | Purpose |
|---|---|
| [`aws/setup_s3_tables.py`](../aws/setup_s3_tables.py) | Runs steps 1 to 3 and writes the Aura form values to `.env.aws` |
| [`aws/aura-s3tables-read.json`](../aws/aura-s3tables-read.json) | The read-only IAM policy that Aura's credentials use |

## Prerequisites

- An AWS account with permission to create S3 table buckets and IAM roles.
- An IAM user or role, not the root account. AWS does not let the root account assume
  a role, so the script exits when it runs with root credentials.
- Permission for your own identity to call `sts:AssumeRole` on the read role. The
  trust policy names the account root, so your identity policy must also allow the
  call. Admin-style permission sets already do.
- A named AWS profile with credentials for that account. The setup script needs only
  the profile.
- AWS CLI v2, only if you run the manual commands in steps 1 to 3 and the cleanup.
- `uv` installed. It runs the setup script and the demos.
- An Aura account with access to Virtual Graph.
- A project `.env` with the `NEO4J_*` values, as described in [`running-demos.md`](running-demos.md#configure-env).

Pick one AWS region and use it everywhere. When you create the Virtual Graph in Aura,
choose AWS as the cloud provider and the same region.

A Virtual Graph in a different region still works. On 2026-11-17, a Virtual Graph in
us-west-2 read a table bucket in us-east-1. Every query then crosses regions, which adds
latency and AWS data transfer charges.

S3 Tables namespace and table names allow only lowercase letters, numbers, and
underscores. That is why the namespace is `finance_genie`.

## Quick path: run the setup script

From the project root:

```bash
uv run aws/setup_s3_tables.py --profile default
```

The script runs steps 1 to 3 below, checks that the read-only credentials can read the
tables, and writes every value for the Aura form to `.env.aws`. That file is gitignored.
Every step is safe to re-run. Run `--help` to change the region, bucket, namespace, or
role name.

Then skip to [step 4](#step-4-connect-the-s3-table-bucket-in-aura).

The sections below describe what the script does. The AWS CLI commands are examples for
doing the same work by hand. They assume these shell variables:

```bash
export AWS_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export TABLE_BUCKET=finance-genie
export NAMESPACE=finance_genie
export TABLE_BUCKET_ARN=arn:aws:s3tables:${AWS_REGION}:${AWS_ACCOUNT_ID}:bucket/${TABLE_BUCKET}
```

### Renew the credentials

The temporary credentials last at most one hour. To get new ones without reloading the
tables, run:

```bash
uv run aws/setup_s3_tables.py --profile default --creds-only
```

The script writes the new key, secret, and session token to `.env.aws`. Paste them into
the Aura data source, as [Refresh the credentials in Aura](#refresh-the-credentials-in-aura)
describes.

## Step 1: Create the table bucket and namespace

A table bucket is an S3 bucket type built for Iceberg tables. A namespace groups tables
inside it, like a schema in a database.

```bash
aws s3tables create-table-bucket --region "$AWS_REGION" --name "$TABLE_BUCKET"

aws s3tables create-namespace --region "$AWS_REGION" \
  --table-bucket-arn "$TABLE_BUCKET_ARN" --namespace "$NAMESPACE"
```

## Step 2: Load the Finance Genie tables

The script downloads the Finance Genie CSVs from the public
[graph-on-databricks](https://github.com/neo4j-partners/graph-on-databricks) repo. It
types each column to match the Databricks setup notebook, then writes each file as an
Iceberg table with PyIceberg. PyIceberg talks to the
[S3 Tables Iceberg REST endpoint](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html).
See `load_tables` in [`setup_s3_tables.py`](../aws/setup_s3_tables.py) for the schemas and the
catalog settings.

This step runs as your own AWS identity, which needs write access to the table bucket.
Timestamps are stored as Iceberg `timestamptz` in UTC, matching Databricks `TIMESTAMP`.
The script also appends a BIGINT epoch-seconds copy of each timestamp.
`transactions` gets `txn_epoch_seconds` after `txn_hour`. `account_links` gets
`transfer_epoch_seconds` after `transfer_timestamp`.

Confirm the tables exist:

```bash
aws s3tables list-tables --region "$AWS_REGION" \
  --table-bucket-arn "$TABLE_BUCKET_ARN" --namespace "$NAMESPACE" \
  --query 'tables[].name'
```

The output lists `accounts`, `merchants`, `transactions`, `account_links`, and
`account_labels`.

## Step 3: Create read-only credentials for Aura

Virtual Graph needs only read access. Aura runs every generated query with the
credentials you give it, so do not give it your own.

[`aura-s3tables-read.json`](../aws/aura-s3tables-read.json) grants the read actions behind the
Iceberg REST operations that list namespaces, list tables, and load a table. It grants
no write, create, or delete actions. Its resources hold the placeholders `<region>`,
`<account-id>`, and `<table-bucket>`. The script fills them in for you. By hand, fill
them into a temporary copy, as the commands below do. Do not edit the tracked file,
because that would put your account ID into git.

The script attaches this policy to an IAM role, not an IAM user. Some AWS organizations
block `iam:CreateUser`. It then assumes the role to get a temporary key, secret, and
session token.

Only the identity that runs the script can assume the role. The trust policy allows the
account root, with an `aws:PrincipalArn` condition that limits it to your role or user
ARN. Each run rewrites the trust policy for whoever runs it. For an SSO session, the ARN
is the permission-set role ARN with its IAM path, such as
`arn:aws:iam::<account-id>:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_...`. It
is not the `sts` assumed-role ARN. The commands below look up that ARN first.

The script writes the Aura form values to `.env.aws`. It refuses an `--output` path
inside the repo unless git ignores that path.

```bash
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
case "$CALLER_ARN" in
  arn:aws:sts::*:assumed-role/*)
    CALLER_ROLE=$(echo "$CALLER_ARN" | cut -d/ -f2)
    CALLER_ARN=$(aws iam get-role --role-name "$CALLER_ROLE" \
      --query Role.Arn --output text) ;;
esac

aws iam create-role --role-name aura-virtual-graph-read \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::'"$AWS_ACCOUNT_ID"':root"},"Action":"sts:AssumeRole","Condition":{"ArnEquals":{"aws:PrincipalArn":"'"$CALLER_ARN"'"}}}]}'

POLICY_FILE=$(mktemp)
sed -e "s/<region>/${AWS_REGION}/g" \
    -e "s/<account-id>/${AWS_ACCOUNT_ID}/g" \
    -e "s/<table-bucket>/${TABLE_BUCKET}/g" \
    aws/aura-s3tables-read.json > "$POLICY_FILE"

aws iam put-role-policy --role-name aura-virtual-graph-read \
  --policy-name aura-s3tables-read \
  --policy-document "file://${POLICY_FILE}"
rm "$POLICY_FILE"

aws sts assume-role --duration-seconds 3600 \
  --role-arn arn:aws:iam::${AWS_ACCOUNT_ID}:role/aura-virtual-graph-read \
  --role-session-name aura-virtual-graph
```

The temporary credentials expire. Assumed from an SSO session, they last at most one
hour. After that, the Virtual Graph loses access to the data.
[Renew the credentials](#renew-the-credentials) shows how to get new ones.

A refresh rewrites the role's trust policy and inline policy, so it needs the same IAM
write permissions as the first run. After a refresh, only the identity that ran it can
assume the role.

A read right after setup can fail with access denied while the role policy applies.
The script retries the verify step for about 30 seconds.

If your organization allows IAM users, a user with the same policy and a long-lived
access key avoids the refresh. Leave the session token blank in that case.

### Check whether your organization blocks IAM users

These commands are read-only. Run them with the same profile as the setup script.

Show who you are signed in as:

```bash
aws sts get-caller-identity --profile default
```

Check that the account is in an organization with service control policies enabled:

```bash
aws organizations describe-organization --profile default \
  --query 'Organization.AvailablePolicyTypes'
```

Get the full ARN of your SSO role, including its IAM path:

```bash
aws iam get-role --profile default \
  --role-name AWSReservedSSO_AdministratorAccess_<id> --query Role.Arn --output text
```

Simulate the IAM actions for that role. `AllowedByOrganizations` is `False` when a
service control policy denies the action:

```bash
aws iam simulate-principal-policy --profile default \
  --policy-source-arn <sso-role-arn> \
  --action-names iam:CreateUser iam:CreateRole iam:PutRolePolicy sts:AssumeRole \
  --query 'EvaluationResults[].{action:EvalActionName,decision:EvalDecision,org:OrganizationsDecisionDetail.AllowedByOrganizations}' \
  --output table
```

Find earlier `CreateUser` attempts. IAM events are recorded in `us-east-1`:

```bash
aws cloudtrail lookup-events --profile default --region us-east-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateUser \
  --max-results 5
```

Check the role's maximum session duration:

```bash
aws iam get-role --profile default --role-name aura-virtual-graph-read \
  --query Role.MaxSessionDuration
```

Raising it does not help when you run the script from an SSO session. Assuming a role
from another role's session is role chaining, and AWS caps a chained session at one
hour.

## Step 4: Connect the S3 table bucket in Aura

Create the Virtual Graph as
[section 1 of `vg-model.md`](vg-model.md#1-create-the-virtual-graph-in-aura) describes.
Choose AWS as the cloud provider and your region. When you select **Add new data
source**, choose the S3 table bucket source.

Fill in the **Connect** form from `.env.aws`:

| Field | `.env.aws` value |
|---|---|
| **Name** | `AURA_DATA_SOURCE_NAME` |
| **Table Bucket ARN** | `AURA_TABLE_BUCKET_ARN` |
| **Namespace** | `AURA_NAMESPACE` |
| **AWS Key** | `AURA_AWS_KEY` |
| **AWS Secret** | `AURA_AWS_SECRET` |
| **AWS Session Token** | `AURA_AWS_SESSION_TOKEN` |
| **AWS Region** | `AURA_AWS_REGION` |

Select **Next**. The **Connection** and **Verify** steps check the credentials and
discover the tables. A successful setup lists all five tables.

If verification fails with an access error, find the denied `s3tables` action in
CloudTrail. Calls such as `ListTables`, `GetTable`, and `GetTableMetadataLocation` are
management events. They appear in **CloudTrail → Event history** in the same region.
Filter by user name `aura-virtual-graph` to find them. Reads of table data, such as
`GetTableData`, are data events. Event history does not show data events. A denied data
read appears only in a trail that logs S3 Tables data events. Add the denied action to
the policy and run the script again.

### Refresh the credentials in Aura

The credentials in the data source are temporary. When they expire, the Virtual Graph
loses access to the tables. Run the command in
[Renew the credentials](#renew-the-credentials) to get new values. Then update the data
source in Aura:

1. Go to **Import → Data sources**.
2. Select the S3 table bucket data source to open its **View details** panel.
3. Select **Edit data source**.
4. Replace **AWS Key**, **AWS Secret**, and **AWS Session Token** with the new values
   from `.env.aws`, then save.

> **Possible bug:** The edit may not save the new credentials. After **Confirm
> changes**, open **View details** again and check that **AWS Key** shows the new key
> ID. If it still shows the old one, the Virtual Graph keeps the expired credentials.

## Step 5: Build the graph model

Follow sections 2 and 3 of [`vg-model.md`](vg-model.md#2-select-a-graph-model).
The target model is the same:

- `:Account` nodes from `accounts`
- `:Merchant` nodes from `merchants`
- `TRANSACTED_WITH` relationships from `:Account` to `:Merchant`, built from `transactions`
- `TRANSFERRED_TO` relationships from `:Account` to `:Account`, built from `account_links`

The model uses the same camelCase property names, such as `accountId` and
`transferTimestamp`. The Iceberg columns keep their snake_case names. Step 6 of
[section 3](vg-model.md#3-define-your-schema) renames the properties and lists
every column and its property name.

Map the two epoch-seconds columns as integer properties. Map `txn_epoch_seconds` to
`txnEpochSeconds` on `TRANSACTED_WITH`. Map `transfer_epoch_seconds` to
`transferEpochSeconds` on `TRANSFERRED_TO`. Query 10 and the `gds-probe` timestamp
scenarios read them. `toInteger()` on a timestamp fails on S3 Tables, because DuckDB
rejects the timestamp-to-`BIGINT` cast.

Download the credentials file when Aura prompts you after **Create Virtual Graph**. Put
the connection URI and password in `.env`.

## Step 6: Run the demos

The demos talk only to the Virtual Graph. Run them from the project root as usual:

```bash
uv run vg-demo                          # fraud demo (default): queries 1-10
uv run vg-demo --demo basic             # exploration / visualization queries
uv run vg-demo --demo fast-gds          # working PageRank path (7-day window)
uv run vg-demo --demo gds-probe         # sweep property projections (7-day window)
uv run vg-demo --demo gds-examples      # PageRank, WCC and Louvain on every transfer
```

If every query fails with `Unable to retrieve routing information`, see
[If the connection fails with a certificate error](running-demos.md#if-the-connection-fails-with-a-certificate-error).

Three parts of this project are Databricks only and do not apply on AWS:

- **The `100m` demo.** It sends SQL straight to a Databricks SQL warehouse.
- **`vg-generate`.** It appends rows through a Databricks SQL warehouse.
- **The `gds-examples` demo at full scale.** It expects the larger graph that
  `vg-generate` builds. On the base dataset it still runs, on fewer edges.

## Cost and cleanup

S3 Tables bills for storage, requests, and automatic table maintenance. The Finance
Genie tables are small, so storage costs little. Each Cypher query becomes reads against
the table bucket, so heavy demo runs add request charges. The Virtual Graph instance
bills Aura Credits while it runs.

When you are done, delete the Virtual Graph in the Aura console. Then remove the AWS
resources:

```bash
for t in accounts merchants transactions account_links account_labels; do
  aws s3tables delete-table --region "$AWS_REGION" \
    --table-bucket-arn "$TABLE_BUCKET_ARN" --namespace "$NAMESPACE" --name "$t"
done

aws s3tables delete-namespace --region "$AWS_REGION" \
  --table-bucket-arn "$TABLE_BUCKET_ARN" --namespace "$NAMESPACE"

aws s3tables delete-table-bucket --region "$AWS_REGION" \
  --table-bucket-arn "$TABLE_BUCKET_ARN"

aws iam delete-role-policy --role-name aura-virtual-graph-read --policy-name aura-s3tables-read
aws iam delete-role --role-name aura-virtual-graph-read
rm .env.aws
```

## Keep learning

- [Aura Import data sources](https://neo4j.com/docs/aura/import/data-sources/) for the S3 connector used by Aura Import.
- [Virtual Graph data sources](https://neo4j.com/docs/virtual-graph/aura/data-sources/) for the officially listed Virtual Graph sources.
- [Amazon S3 Tables Iceberg REST endpoint](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-open-source.html) for PyIceberg and Spark client setup.
- [Access management for S3 Tables](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-setting-up.html) for IAM policy options.
