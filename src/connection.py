"""Shared connection helpers for the Finance Genie Virtual Graph demos.

Every script here (``cli.py`` and the support scripts) reads the same Aura
credentials from the project ``.env`` at the repository root. Set ``PROBE_ENV`` to
point at a different dotenv. ``probe.py`` and ``viz_check.py`` use that for ad-hoc
targets.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"

# The SQL warehouse behind the Virtual Graph ("vg demo sql warehouse"), used by the
# 100m demo. It is the warehouse the warehouse-performance tests ran on, and the one
# the `account_links_large` table lives on. Override it with VG_BACKING_WAREHOUSE_ID
# in the shell or the project ``.env``. DATABRICKS_WAREHOUSE_ID is deliberately not
# read, because the project ``.env`` uses it for a different (app/analyst) warehouse.
VG_BACKING_WAREHOUSE = "b0fffb8e3255bf85"


def _resolve_env_file(env_file: Path | None) -> Path:
    """Resolve the dotenv to read: explicit arg, then ``PROBE_ENV``, then ``.env``."""
    if env_file is None:
        override = os.environ.get("PROBE_ENV")
        env_file = Path(override).expanduser() if override else PROJECT_ENV
    if not env_file.is_file():
        sys.exit(f"Could not find env file at {env_file}")
    return env_file


@dataclass(frozen=True)
class DatabricksConfig:
    """Databricks SQL settings for the 100m demo, read from the project ``.env``."""

    profile: str
    catalog: str
    schema: str
    warehouse: str = VG_BACKING_WAREHOUSE


def load_databricks_config(env_file: Path | None = None) -> DatabricksConfig:
    """Read the Databricks profile, Unity Catalog location and warehouse from ``.env``.

    The 100m demo talks SQL to the warehouse via the Databricks SDK
    (``WorkspaceClient``) rather than Bolt, so it needs the config profile, the
    catalog/schema that hold ``account_links_large``, and the warehouse to run on. The
    warehouse comes from ``VG_BACKING_WAREHOUSE_ID``, else ``VG_BACKING_WAREHOUSE``.
    ``DATABRICKS_WAREHOUSE_ID`` is not read, because the env file's warehouse points
    elsewhere.

    The dotenv is read without exporting it into ``os.environ``. The SDK resolves
    environment variables before the profile file, so an exported ``DATABRICKS_HOST``,
    ``DATABRICKS_CLUSTER_ID`` or ``DATABRICKS_WAREHOUSE_ID`` from ``.env`` would
    override the chosen profile's values. Values in the file still take precedence
    over the shell, as ``load_dotenv(override=True)`` did.
    """
    file_values = dotenv_values(_resolve_env_file(env_file))
    env = {**os.environ, **{k: v for k, v in file_values.items() if v is not None}}
    profile = (env.get("DATABRICKS_CONFIG_PROFILE")
               or env.get("DATABRICKS_PROFILE") or "DEFAULT")
    catalog = (env.get("DATABRICKS_CATALOG")
               or env.get("CATALOG") or "virtual-graph-dbx")
    schema = (env.get("DATABRICKS_SCHEMA")
              or env.get("SCHEMA") or "vg-schema")
    warehouse = env.get("VG_BACKING_WAREHOUSE_ID") or VG_BACKING_WAREHOUSE
    return DatabricksConfig(profile=profile, catalog=catalog, schema=schema,
                            warehouse=warehouse)


def load_connection(env_file: Path | None = None) -> tuple[str, tuple[str, str]]:
    """Read Neo4j credentials from a dotenv and return ``(uri, auth)``.

    Resolves the dotenv in this order: the explicit ``env_file`` argument, then the
    ``PROBE_ENV`` environment variable, then the project ``.env`` at the repository
    root. Exits with a clear message if the file is missing or any credential is
    unset.
    """
    env_file = _resolve_env_file(env_file)
    load_dotenv(env_file, override=True)

    uri = os.environ.get("NEO4J_URI")
    username = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")
    missing = [
        name
        for name, value in (
            ("NEO4J_URI", uri),
            ("NEO4J_USERNAME", username),
            ("NEO4J_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        sys.exit(f"Missing required variables in {env_file}: {', '.join(missing)}")
    return uri, (username, password)
