"""Shared connection helpers for the Finance Genie Virtual Graph demos.

Every script here (``cli.py`` and the support scripts) reads the same Aura
credentials from the project ``.env`` at the repository root. Set ``PROBE_ENV`` to
point at a different dotenv; ``probe.py`` and ``viz_check.py`` use that for ad-hoc
targets.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"

# The SQL warehouse behind the Virtual Graph ("vg demo sql warehouse"), used by the
# 100m demo. It is the warehouse the warehouse-performance tests ran on, and the one
# the `account_links_large` table lives on. The project ``.env`` carries a different
# (app/analyst) warehouse id, so this is the default rather than the env value.
VG_BACKING_WAREHOUSE = "b0fffb8e3255bf85"


def _resolve_env_file(env_file: Path | None) -> Path:
    """Resolve which dotenv to read: explicit arg, then ``PROBE_ENV``, then project ``.env``."""
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


def load_databricks_config(env_file: Path | None = None) -> DatabricksConfig:
    """Read the Databricks profile and Unity Catalog defaults from the project ``.env``.

    The 100m demo talks SQL to the warehouse via the ``databricks`` CLI rather than
    Bolt, so it needs the CLI profile and the catalog/schema that hold
    ``account_links_large``. The warehouse id is not read from here on purpose: the
    backing VG warehouse (``VG_BACKING_WAREHOUSE``) is the one that runs the test, and
    the env file's warehouse points elsewhere.
    """
    load_dotenv(_resolve_env_file(env_file), override=True)
    profile = (os.environ.get("DATABRICKS_CONFIG_PROFILE")
               or os.environ.get("DATABRICKS_PROFILE") or "DEFAULT")
    catalog = (os.environ.get("DATABRICKS_CATALOG")
               or os.environ.get("CATALOG") or "graph-on-databricks")
    schema = (os.environ.get("DATABRICKS_SCHEMA")
              or os.environ.get("SCHEMA") or "graph-enriched-schema")
    return DatabricksConfig(profile=profile, catalog=catalog, schema=schema)


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
