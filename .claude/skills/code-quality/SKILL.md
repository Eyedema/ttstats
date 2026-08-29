---
name: code-quality
description: Run code quality checks (ruff lint, ruff format, pyright, pytest) on a directory and report findings by severity. Use when the user wants to audit code quality, check for type errors, lint issues, or run automated checks on a path. Accepts a directory path as argument. Triggers on requests like "check code quality", "run quality checks", "/code-quality apps/".
---

# Code Quality Review

Review code quality in the directory provided by the user.

## Instructions

1. **Identify files to review**:
   - Find all `.py` files in the directory
   - Exclude migrations, `__pycache__`, and generated files

2. **Run automated checks**:
   ```bash
   uv run ruff check <directory>
   uv run ruff format --check <directory>
   uv run pyright <directory>
   uv run pytest <directory> -v
   ```

3. **Manual review checklist**:
   - [ ] No `Any` types without justification
   - [ ] Proper error handling (no silent exceptions)
   - [ ] N+1 queries avoided (select_related/prefetch_related)
   - [ ] Forms have proper validation
   - [ ] Views return correct HTTP status codes
   - [ ] HTMX partials handle HX-Request header
   - [ ] Celery tasks are idempotent
   - [ ] Tests use factories, not raw object creation

4. **Report findings** organized by severity:
   - Critical (must fix)
   - Warning (should fix)
   - Suggestion (could improve)
