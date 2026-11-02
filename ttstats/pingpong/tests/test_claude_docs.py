"""Guards for CLAUDE.md and the path-scoped rules in `.claude/rules/`.

Two failures here are silent otherwise: a `paths:` glob that no longer
matches any file means its rule simply stops loading, and CLAUDE.md growing
back into a document nobody reads end to end. No DB, no Django.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
RULES_DIR = REPO_ROOT / ".claude" / "rules"

CLAUDE_MD_WORD_BUDGET = 1500
IGNORED_DIRS = {"node_modules", ".git", ".venv", "__pycache__"}


def _rule_files():
    return sorted(RULES_DIR.glob("*.md"))


def _paths_from_frontmatter(text):
    """Read the `paths:` list from a rule's frontmatter without needing PyYAML."""
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    paths, in_paths = [], False
    for line in match.group(1).splitlines():
        if line.strip() == "paths:":
            in_paths = True
        elif in_paths and line.lstrip().startswith("- "):
            paths.append(line.split("- ", 1)[1].strip().strip("\"'"))
        elif in_paths and line.strip():
            in_paths = False
    return paths


def _glob_matches(pattern):
    return any(
        not IGNORED_DIRS.intersection(p.relative_to(REPO_ROOT).parts)
        for p in REPO_ROOT.glob(pattern)
    )


class TestClaudeMd:
    def test_stays_within_word_budget(self):
        words = len(CLAUDE_MD.read_text().split())
        assert words <= CLAUDE_MD_WORD_BUDGET, (
            f"CLAUDE.md is {words} words (budget {CLAUDE_MD_WORD_BUDGET}). "
            "Move subsystem detail into .claude/rules/."
        )

    def test_index_lists_every_rule_file_and_nothing_else(self):
        text = CLAUDE_MD.read_text()
        index = text.split("## Topic rules", 1)[1]
        listed = set(re.findall(r"`([a-z0-9-]+)`", index))
        on_disk = {f.stem for f in _rule_files()}
        assert on_disk - listed == set(), "rule files missing from the CLAUDE.md index"
        assert listed - on_disk == set(), "CLAUDE.md index names rules that don't exist"

    def test_referenced_docs_exist(self):
        text = CLAUDE_MD.read_text()
        refs = set(re.findall(r"`((?:docs|\.claude)/[\w./-]+\.md)`", text))
        missing = sorted(r for r in refs if not (REPO_ROOT / r).exists())
        assert missing == []


class TestRuleFiles:
    def test_rules_exist(self):
        assert _rule_files()

    @pytest.mark.parametrize("rule", _rule_files(), ids=lambda p: p.name)
    def test_has_paths_frontmatter(self, rule):
        paths = _paths_from_frontmatter(rule.read_text())
        assert paths, f"{rule.name} needs a non-empty `paths:` list in its frontmatter"

    @pytest.mark.parametrize("rule", _rule_files(), ids=lambda p: p.name)
    def test_every_path_glob_matches_a_file(self, rule):
        paths = _paths_from_frontmatter(rule.read_text()) or []
        dead = [p for p in paths if not _glob_matches(p)]
        assert dead == [], f"{rule.name}: these globs match nothing (renamed file?)"
