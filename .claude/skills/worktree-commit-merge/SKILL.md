---
name: worktree-commit-merge
description: Commit worktree changes, merge into master/main, then sync the worktree branch with master. Trigger when the user says things like "commit and merge to master", "we're done with this worktree, commit and merge", or any similar phrasing. Don't wait for the user to spell out "worktree" — if there are changes and they mention merging to master/main, use this skill.
---

## Overview

You're in a git worktree on a feature branch with uncommitted changes. This skill:
1. Commits those changes to the current branch
2. Merges the branch into master/main — fast-forward if possible, otherwise a regular merge commit

## Step 1: Gather context

Run these in parallel:

```bash
git status
git diff HEAD
git branch --show-current
git worktree list
git log --oneline -10
```

This gives you:
- What files changed and what's staged vs unstaged
- The current branch name (the worktree branch)
- The main worktree path and which branch it has checked out
- Recent commit style so your message fits the project

## Step 2: Commit the changes

Stage specific files rather than `git add -A`, which can accidentally include unintended files like `.env` or build artifacts:

```bash
git add <specific relevant files>
```

Write a commit message that follows the style from the recent log. Always pass via heredoc to preserve formatting:

```bash
git commit -m "$(cat <<'EOF'
<type>(<scope>): <description>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

## Step 3: Identify the merge target

From `git worktree list` output, the **first entry is always the main worktree**. Identify:
- `MAIN_PATH` — filesystem path of the main worktree
- `MAIN_BRANCH` — branch checked out there (typically `master` or `main`)
- `CURRENT_BRANCH` — the branch you just committed to

## Step 4: Merge into master/main

Try fast-forward first. If the histories have diverged, fall back to a regular merge commit:

```bash
git -C <MAIN_PATH> merge --ff-only <CURRENT_BRANCH> \
  || git -C <MAIN_PATH> merge <CURRENT_BRANCH> --no-edit
```

Tell the user which path was taken — fast-forward or merge commit — so they know what happened to the history.

## Step 5: Sync the worktree branch with master/main

After merging, bring the worktree branch up-to-date so it includes any commits that were already on master/main (e.g., from other merges):

```bash
git merge <MAIN_BRANCH>
```

This runs in the worktree directory (current directory), pulling in the full state of master/main.

## Step 6: Confirm

```bash
git -C <MAIN_PATH> log --oneline -5
```

Report what was committed (files + message), how it was merged, and that the worktree branch is now in sync with master/main.
