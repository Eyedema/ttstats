---
name: docs-sync
description: Check if documentation is in sync with code. Use when the user wants to verify that documentation matches current code, find outdated docs, or audit documentation accuracy. Covers CLAUDE.md, the path-scoped rules in .claude/rules/, the skills, and docs/. Triggers on requests like "check docs", "sync documentation", "are the docs up to date", "/docs-sync".
---

# Documentation Sync

Check that the documentation still describes the code. Report only what is
**wrong**, never what is merely missing.

## Where the docs live

| Location | Holds |
| --- | --- |
| `CLAUDE.md` | Commands, testing rules, gotchas, core rules. Loaded every session. 1,500-word budget. |
| `.claude/rules/*.md` | One subsystem each; `paths:` frontmatter decides when it loads. |
| `.claude/skills/*/SKILL.md` | Workflows (e.g. `frontend-verification`). |
| `docs/` | Runbooks (`tenancy-rollout.md`) and decision records (`docs/decisions/`). |

## Instructions

1. **Run the mechanical guard first:**
   ```bash
   .venv/bin/python -m pytest ttstats/pingpong/tests/test_claude_docs.py -q
   ```
   It checks the word budget, the CLAUDE.md rule index, referenced doc paths,
   and that every `paths:` glob still matches a file. Fix any failure before
   going further.

2. **Find recent code changes:**
   ```bash
   git log --since="30 days ago" --name-only --pretty=format: -- "*.py" "*.html" "*.js" "*.css" | sort -u
   ```

3. **Map each changed file to its docs.** For each file, find the rules whose
   `paths:` match it (`grep -l` the filename in `.claude/rules/`), plus any
   mention in `CLAUDE.md`, the skills or `docs/`.

4. **Verify every concrete claim** in those docs against the code: function,
   class, setting, flag, URL and file names exist; described behaviour still
   holds (read the code, don't infer from names); numbers (`MIN_SAMPLE_POINTS`,
   rate limits, budgets) match the constants.

5. **Check coverage of new modules.** A new `pingpong/*.py`, template or e2e
   spec that falls under no rule's `paths:` is worth flagging if a rule clearly
   governs it.

6. **Check placement.** Flag subsystem detail that crept into `CLAUDE.md`,
   rules duplicated across files, and incident narratives or measurements
   that belong in commit messages.

7. **Output a checklist**: file, the wrong claim, what the code actually does.
   Offer to apply the fixes; don't apply them unasked.
