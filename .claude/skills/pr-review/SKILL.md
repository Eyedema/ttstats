---
name: pr-review
description: Review a pull request using project standards. Use when the user wants to review a PR, check code quality, or get structured feedback on changes. Accepts a PR number or URL as argument. Triggers on requests like "review PR 123", "check this pull request", "/pr-review 123".
---

# PR Review

Review the pull request provided by the user (PR number or URL).

## Instructions

1. **Get PR information**:
   - Run `gh pr view <PR>` to get PR details
   - Run `gh pr diff <PR>` to see changes

2. **Read review standards**:
   - Read `.claude/agents/code-reviewer.md` for the review checklist

3. **Apply the checklist** to all changed files:
   - Type hint compliance (no `Any`)
   - Error handling patterns (no silent exceptions)
   - N+1 queries avoided (select_related/prefetch_related)
   - Loading/error/empty states in templates
   - Test coverage
   - Documentation updates

4. **Provide structured feedback**:
   - **Critical**: Must fix before merge
   - **Warning**: Should fix
   - **Suggestion**: Nice to have

5. **Post review comments** using `gh pr comment`
