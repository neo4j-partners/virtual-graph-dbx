"""Guard the graph naming convention: Cypher uses camelCase, SQL keeps snake_case.

The tables and columns stay snake_case. The Virtual Graph model maps them to camelCase
property keys, so a snake_case name in a Cypher position means a query that the model
cannot answer. The checks read source text rather than running queries, so they need
no live connection.

Python is checked through its string constants, found with ``ast``, so Python names
such as ``account_id`` or ``except X as exc_info`` never count. Markdown is checked
through its ```cypher fences only. Known limits: Cypher in an untagged fence or in
inline backticks in prose is not checked, and a snake_case name used only as a bare
reference is caught only where it is defined.

Result-key reads are checked only where the test can pair them with a query. See
``ResultKeyTests`` for which reads count.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

from queries import QUERIES

# Modules that send SQL to a warehouse, and their tests. Their snake_case column names
# are correct. This file holds snake_case Cypher on purpose, to prove the patterns
# catch it.
SKIPPED = {
    SRC / "demos" / "sql_spike.py",
    SRC / "generate_large.py",
    TESTS / "test_generate_large.py",
    TESTS / "test_naming.py",
    TESTS / "test_sql_spike.py",
}

# Every Markdown file is checked through its ```cypher fences only, because the
# prose, mapping tables, and SQL blocks name the snake_case columns on purpose. The
# plan quotes the old names as before-and-after examples.
SKIPPED_DOCS = {
    REPO_ROOT / "worklog" / "fix-it.md",
    REPO_ROOT / "worklog" / "work-audit-log.md",
}
CYPHER_FENCE = re.compile(r"```cypher[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# The column names that the model renames to camelCase property keys.
OLD_PROPERTIES = (
    "account_id", "account_hash", "account_name", "account_type", "opened_date",
    "holder_age", "merchant_id", "merchant_name", "txn_id", "txn_timestamp",
    "txn_hour", "link_id", "transfer_timestamp",
)
_PROPS = "|".join(OLD_PROPERTIES)
_SNAKE = r"[a-z][a-z0-9]*_[a-z0-9_]+"

# Cypher positions where a name is a property key, parameter, alias, or variable.
# Keywords are case-insensitive in Cypher, so the keywords match in any case. The
# names must stay lowercase, so a SCREAMING_CASE label or relationship type never
# counts.
CYPHER_PATTERNS = tuple(re.compile(pattern) for pattern in (
    rf"\.({_PROPS})\b",  # a.account_id
    rf"[{{,]\s*({_SNAKE})\s*:",  # {region: 'EU', account_id: 1}
    rf"\$({_SNAKE})\b",  # $account_id
    rf"\b(?i:AS)\s+({_SNAKE})\b",  # AS near_threshold, as near_threshold
    rf"[\[(]\s*({_SNAKE})\s*[:)\]|*{{]",  # (mule_acct:Account), [t_in*1..3]
    rf"\[\s*({_SNAKE})\s+(?i:IN)\b",  # [snake_var IN list | ...]
    rf"\b({_SNAKE})\s*=\s*\(",  # hop_path = (a)-->(b)
))

# A string constant holding any of these, in any case, is treated as Cypher.
CYPHER_KEYWORD = re.compile(r"\b(MATCH|RETURN|CALL|UNWIND|WITH)\b", re.IGNORECASE)


def snake_case_hits(text: str) -> list[str]:
    """Return every snake_case name found in a Cypher position in ``text``."""
    return [m.group(0) for pattern in CYPHER_PATTERNS for m in pattern.finditer(text)]


def cypher_constants(path: Path) -> Iterator[str]:
    """Yield each string constant in ``path`` that looks like Cypher."""
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and CYPHER_KEYWORD.search(node.value)):
            yield node.value


def split_top_level(text: str) -> list[str]:
    """Split ``text`` on commas that sit outside brackets."""
    parts, depth, start = [], 0, 0
    for i, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def return_columns(cypher: str) -> set[str]:
    """Return the column names of the last ``RETURN`` clause in ``cypher``."""
    clause = re.split(r"\bRETURN\b", cypher, flags=re.IGNORECASE)[-1]
    clause = re.sub(r"^\s*DISTINCT\b", "", clause, flags=re.IGNORECASE)
    clause = re.split(r"\b(?:ORDER\s+BY|SKIP|LIMIT|UNION)\b", clause,
                      flags=re.IGNORECASE)[0]
    columns = set()
    for item in split_top_level(clause):
        alias = re.search(r"\bAS\s+(`[^`]+`|\w+)\s*$", item, re.IGNORECASE)
        columns.add(alias.group(1).strip("`") if alias else item)
    return columns


def python_files() -> list[Path]:
    files = sorted([*SRC.rglob("*.py"), *TESTS.rglob("*.py")])
    return [path for path in files if path not in SKIPPED]


def doc_files() -> list[Path]:
    """Every Markdown file in the repo outside hidden folders and ``SKIPPED_DOCS``."""
    return sorted(
        path for path in REPO_ROOT.rglob("*.md")
        if path not in SKIPPED_DOCS
        and not any(part.startswith(".") or part == "node_modules"
                    for part in path.relative_to(REPO_ROOT).parts)
    )


class PatternTests(unittest.TestCase):
    def test_patterns_catch_snake_case(self) -> None:
        cases = {
            "MATCH (a:Account {account_id: $account_id})": 2,
            "match (a) where a.accountId = $acctId return a.holderAge as holder_age": 1,
            "MATCH (mule_acct {accountId: 1})": 1,
            "MATCH ()-[t_in {amount: 1}]->()": 1,
            "MATCH ()-[t_in*1..3]->()": 1,
            "MATCH hop_path = (a)-->(b)": 1,
            "MATCH (a:Account {region: 'EU', account_id: 1})": 1,
            "RETURN a.opened_date AS opened_date": 2,
            "return a.accountId as mule_id": 1,
            "MATCH ()-[t_in:TRANSFERRED_TO]->()": 1,
            "MATCH ()-[t_in :TRANSFERRED_TO]->()": 1,
            "MATCH ()-[t_in]->()": 1,
            "MATCH (mule_acct:Account)": 1,
            "RETURN [hop_id IN ids | hop_id]": 1,
            "{ amount: t.amount, transfer_timestamp_ms: 1 }": 1,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(len(snake_case_hits(text)), expected)

    def test_patterns_pass_camel_case(self) -> None:
        good = ("MATCH (a:Account {accountId: $accountId})-[tIn:TRANSFERRED_TO]->"
                "(m:Account) WITH a.openedDate AS openedDate, [x IN ids | x] AS ids "
                "RETURN openedDate, { amount: tIn.amount, transferTimestampMs: 1 }")
        self.assertEqual(snake_case_hits(good), [])
        self.assertEqual(snake_case_hits("RETURN type(r) AS REL_TYPE"), [])
        self.assertEqual(snake_case_hits("MATCH p = (a)-[*1..3]->(b)"), [])

    def test_lowercase_cypher_is_scanned(self) -> None:
        self.assertTrue(CYPHER_KEYWORD.search("match (a) return a"))

    def test_return_columns(self) -> None:
        cypher = ("MATCH (a) WITH a.x AS x RETURN x, round(sum(a.y), 2) AS total, "
                  "a.z ORDER BY total DESC LIMIT 5")
        self.assertEqual(return_columns(cypher), {"x", "total", "a.z"})
        self.assertEqual(return_columns("match (a) return distinct a.x as `x y`"),
                         {"x y"})


class NamingTests(unittest.TestCase):
    def test_python_cypher_uses_camel_case(self) -> None:
        for path in python_files():
            hits = [hit for text in cypher_constants(path)
                    for hit in snake_case_hits(text)]
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertEqual(hits, [])

    def test_doc_cypher_blocks_use_camel_case(self) -> None:
        docs = doc_files()
        self.assertIn(REPO_ROOT / "README.md", docs)
        for doc in docs:
            blocks = CYPHER_FENCE.findall(doc.read_text())
            hits = [hit for block in blocks for hit in snake_case_hits(block)]
            with self.subTest(doc=doc.relative_to(REPO_ROOT)):
                self.assertEqual(hits, [])


class ResultKeyTests(unittest.TestCase):
    """Python reads of result keys must match the aliases that the Cypher returns.

    The unit tests mock the driver and hand back whatever keys they write, so a
    renamed alias with a stale Python read would pass them and fail only live.
    """

    def test_query_hooks_read_returned_columns(self) -> None:
        for query in QUERIES:
            with self.subTest(query=query.number):
                row = dict.fromkeys(return_columns(query.cypher), 1)
                if query.enrich_cypher is not None:
                    enrich = return_columns(query.enrich_cypher)
                    self.assertIn(query.enrich_key, row)
                    self.assertIn(query.enrich_key, enrich)
                    self.assertLessEqual(set(query.enrich_columns), enrich)
                    row |= query.enrich_columns
                if query.row_transform is not None:
                    query.row_transform(row)
                if query.client_filter is not None:
                    query.client_filter(row)

    def test_inline_queries_read_returned_columns(self) -> None:
        """Each result read names a column of the query just before it.

        A read is a string subscript in ``src``, such as ``top["recipient"]``,
        ``row["nodeId"]``, ``sized[0]["edges"]`` or ``first_row(...)["mx"]``. Its
        query is the nearest Cypher with ``RETURN`` that comes before it in the same
        function, either inline or a module constant used by name, including one
        imported from another module in ``src``. Reads with no query before them are
        skipped. Known limits:

        - A ``.get("key")`` read is not checked.
        - A read with no query before it in the same function is skipped. That covers
          a helper that receives rows, and a read above its query in a loop.
        - A module constant built with ``+``, an f-string or ``.format()`` is not a
          known query, so a read after it pairs with an earlier query or is skipped.
        - A ``module.QUERY`` attribute is not resolved. Only ``from x import QUERY``
          is.
        """
        for path in python_files():
            if SRC not in path.parents:
                continue
            tree = ast.parse(path.read_text())
            known = _imported_queries(tree) | _module_queries(tree)
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef):
                    continue
                sites = sorted(_query_sites(func, known))
                for position, key in _result_reads(func):
                    before = [cypher for at, cypher in sites if at < position]
                    if not before:
                        continue
                    with self.subTest(path=path.relative_to(REPO_ROOT),
                                      func=func.name, line=position[0], key=key):
                        self.assertIn(key, return_columns(before[-1]))


QUERY_SITE = re.compile(r"\bRETURN\b")


def _module_queries(tree: ast.Module) -> dict[str, str]:
    """Map each module-level ``NAME = "...RETURN..."`` constant to its Cypher."""
    queries = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and QUERY_SITE.search(node.value.value)):
            queries |= {target.id: node.value.value for target in node.targets
                        if isinstance(target, ast.Name)}
    return queries


def _imported_queries(tree: ast.Module) -> dict[str, str]:
    """Map query constants imported by name from another module in ``src``."""
    queries = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        module = SRC.joinpath(*node.module.split(".")).with_suffix(".py")
        if module.exists():
            theirs = _module_queries(ast.parse(module.read_text()))
            queries |= {alias.name: theirs[alias.name] for alias in node.names
                        if alias.name in theirs}
    return queries


def _query_sites(func: ast.FunctionDef,
                 known: dict[str, str]) -> Iterator[tuple[tuple[int, int], str]]:
    """Yield the position and Cypher of each query written or named in ``func``."""
    for node in ast.walk(func):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and QUERY_SITE.search(node.value)):
            yield (node.lineno, node.col_offset), node.value
        elif isinstance(node, ast.Name) and node.id in known:
            yield (node.lineno, node.col_offset), known[node.id]


def _result_reads(func: ast.FunctionDef) -> Iterator[tuple[tuple[int, int], str]]:
    """Yield the position and key of each string subscript in ``func``."""
    for node in ast.walk(func):
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            yield (node.slice.lineno, node.slice.col_offset), node.slice.value


if __name__ == "__main__":
    unittest.main()
