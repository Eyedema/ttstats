---
name: ticket
description: Work on a JIRA/Linear ticket end-to-end: read ticket details, explore codebase, create a branch, implement with TDD, run quality checks, update ticket status, and create a PR. Use when the user provides a ticket ID to implement. Triggers on requests like "/ticket PROJ-123", "work on this ticket", "implement PROJ-123".
---

# Ticket Workflow

Work on the ticket ID provided by the user.

## Instructions

### 1. Read the Ticket

Fetch and understand the ticket using JIRA/Linear MCP tools:
- Get ticket details (title, description, acceptance criteria)
- Check linked tickets or epics
- Review any comments or attachments

Summarize: what needs to be done, acceptance criteria, blockers or dependencies.

### 2. Explore the Codebase

Before coding:
- Search for related code
- Understand the current implementation
- Identify files that need changes

### 3. Create a Branch

```bash
git checkout -b {initials}/{ticket-id}-{brief-description}
```

### 4. Implement the Changes

- Follow project patterns (check relevant skills)
- Write tests first (TDD)
- Make incremental commits

### 5. Run Quality Checks

```bash
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```

### 6. Update the Ticket

As you work:
- Add comments with progress updates
- Update status (In Progress → In Review)
- Log any blockers or questions

### 7. Create PR and Link

When ready:
- Create PR with `gh pr create`
- Link the PR to the ticket
- Add ticket ID to PR title: `feat(PROJ-123): description`

### 8. If You Find a Bug

If you discover an unrelated bug while working:
1. Create a new ticket with details
2. Link it to the current ticket if related
3. Note it in the PR description
4. Continue with original task
