---
name: docs-sync
description: Check if documentation is in sync with code. Use when the user wants to verify that documentation matches current code, find outdated docs, or audit documentation accuracy. Triggers on requests like "check docs", "sync documentation", "are the docs up to date", "/docs-sync".
---

# Documentation Sync

Check if documentation matches the current code state.

## Instructions

1. **Find recent code changes**:
   ```bash
   git log --since="30 days ago" --name-only --pretty=format: -- "*.py" "*.ts" "*.tsx" | sort -u
   ```

2. **Find related documentation**:
   - Search `/docs/` for files mentioning changed code
   - Check README files near changed code
   - Look for docstrings in changed files

3. **Verify documentation accuracy**:
   - Do code examples still work?
   - Are API signatures correct?
   - Are field types up to date?

4. **Report only actual problems**:
   - Only flag things that are WRONG, not missing
   - Don't suggest documentation for documentation's sake

5. **Output a checklist** of documentation that needs updating
