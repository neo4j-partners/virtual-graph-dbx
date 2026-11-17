"""Check that every relative link in the docs points at a file and heading that exist.

The check covers Markdown files outside hidden folders, plus the markdown cells of the
notebooks. A link with a ``#fragment`` into a Markdown file must match one of that
file's headings, slugged the way GitHub does it. Web links are not fetched.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
EXTERNAL = ("http://", "https://", "mailto:")

# Links that are known to be broken and are being fixed elsewhere. Each entry is a
# (file, link) pair, so a new broken link in the same file still fails. Remove an
# entry once its link is fixed.
KNOWN_BROKEN: set[tuple[str, str]] = set()


def skipped(path: Path) -> bool:
    """True for files under hidden folders such as ``.venv`` or ``.git``."""
    parts = path.relative_to(REPO_ROOT).parts
    return any(part.startswith(".") or part == "node_modules" for part in parts)


def doc_files() -> list[Path]:
    """Every Markdown file and notebook in the repo outside hidden folders."""
    files = [*REPO_ROOT.rglob("*.md"), *REPO_ROOT.rglob("*.ipynb")]
    return sorted(path for path in files if not skipped(path))


def doc_text(path: Path) -> str:
    """The Markdown text of ``path``. For a notebook, its markdown cells joined."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".ipynb":
        return text
    cells = json.loads(text)["cells"]
    return "\n".join("".join(cell["source"]) for cell in cells
                     if cell["cell_type"] == "markdown")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, no punctuation, dashes for spaces."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every heading anchor in ``path``. Repeats get ``-1``, ``-2`` suffixes."""
    text = FENCE.sub("", doc_text(path))
    found: set[str] = set()
    seen: dict[str, int] = {}
    for match in HEADING.finditer(text):
        base = slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def broken_links(path: Path) -> list[str]:
    """Each relative link in ``path`` whose target file or heading does not exist."""
    text = FENCE.sub("", doc_text(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    problems = []
    for match in LINK.finditer(text):
        link = match.group(1)
        if link.startswith(EXTERNAL) or (rel, link) in KNOWN_BROKEN:
            continue
        target_name, _, fragment = link.partition("#")
        target = (path.parent / target_name).resolve() if target_name else path
        if not target.exists():
            problems.append(f"{rel}: {link} (no such file)")
        elif fragment and target.suffix in (".md", ".ipynb") \
                and fragment not in anchors(target):
            problems.append(f"{rel}: {link} (no such heading)")
    return problems


class SlugTests(unittest.TestCase):
    def test_slug_matches_github(self) -> None:
        cases = {
            "Configure `.env`": "configure-env",
            "1. Create the Virtual Graph in Aura": "1-create-the-virtual-graph-in-aura",
            "Step 1: Set up a data source": "step-1-set-up-a-data-source",
            "Generate a larger graph and run `gds-examples` (optional)":
                "generate-a-larger-graph-and-run-gds-examples-optional",
        }
        for heading, expected in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(slug(heading), expected)


class DocLinkTests(unittest.TestCase):
    def test_docs_found(self) -> None:
        docs = doc_files()
        self.assertIn(REPO_ROOT / "README.md", docs)
        self.assertIn(REPO_ROOT / "docs" / "vg-model.md", docs)
        self.assertIn(REPO_ROOT / "notebooks" / "01_setup_lakehouse.ipynb", docs)

    def test_relative_links_resolve(self) -> None:
        problems = [problem for path in doc_files() for problem in broken_links(path)]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_known_broken_links_are_still_broken(self) -> None:
        """Fail once a known-broken link is fixed, so its entry gets removed."""
        for rel, link in KNOWN_BROKEN:
            with self.subTest(file=rel, link=link):
                path = REPO_ROOT / rel
                target = (path.parent / link.partition("#")[0]).resolve()
                self.assertIn(f"]({link})", doc_text(path))
                self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
