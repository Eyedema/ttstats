---
name: onboard
description: Onboard Claude to a new task by exploring the codebase, building context, and preparing to implement. Use when starting a new task, feature, or bug fix that requires understanding the codebase first. Triggers on requests like "onboard me", "get ready for this task", "explore and prepare", "/onboard".
---

# Onboard

The user has provided context about the task as an argument. Use it to guide exploration.

## Instructions

> "AI models are geniuses who start from scratch on every task." – Noam Brown

Onboard to the current task by:

- Exploring the codebase thoroughly
- Asking the user clarifying questions if needed

The goal is to get fully prepared to start working on the task.

Take as long as needed. Overdoing it is better than underdoing it.

Record everything in a `.claude/tasks/[TASK_ID]/onboarding.md` file. This file will be used to onboard in a new session if needed, so make it comprehensive.
