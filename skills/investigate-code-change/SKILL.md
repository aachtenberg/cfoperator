---
name: investigate-code-change
description: >
  Investigate code changes correlated with infrastructure alerts.
  Use when an alert may be caused by a recent deployment or code change.
---

# Investigate Code Change

## When to Use

Use this skill when:
- An infrastructure alert fires shortly after a deployment
- A service is crashing or misbehaving in a way that suggests a code regression
- You need to answer "what changed recently?" for a host or service
- Alert context includes `recent_changes` from the git context provider

## Investigation Steps

### 1. Check Recent Changes

If `recent_changes` is already in the alert context, review the commits:
- Look at commit messages for keywords: "fix", "refactor", "config", "deploy", "migrate"
- Note which files were changed — config changes and dependency updates are high-signal
- Check timestamps — commits within the last hour are most likely correlated

If `recent_changes` is not present, fetch them:
```
git_recent_commits(repo="<repo-name>", count=10)
```

### 2. Identify Suspect Commits

Use `git_diff_summary` to see what files changed between the last known-good state and now:
```
git_diff_summary(repo="<repo-name>", ref_from="<last-deploy-tag>", ref_to="HEAD")
```

For specific files that appear relevant, use `git_blame` to see who changed what:
```
git_blame(repo="<repo-name>", path="<suspect-file>", lines="<relevant-lines>")
```

### 3. Cross-Reference with GitHub

Check if there are open or recently merged PRs that might explain the issue:
```
github_list_recent_prs(repo="<repo-name>", state="closed", count=5)
```

Search for related issues:
```
github_search_issues(repo="<repo-name>", query="<error-keywords>")
```

### 4. Read Relevant Code

If you need to understand what a file does at the current revision:
```
git_show_file(repo="<repo-name>", path="<file>", ref="HEAD")
```

Or via GitHub (if no local clone):
```
github_get_file_contents(repo="<repo-name>", path="<file>")
```

### 5. Document and Escalate

For confirmed code-related issues:
- Use `comment_issue` to post findings on an existing related GitHub issue
- Use `open_pr` only if a branch with a fix already exists
- Store a learning via `store_learning` with the root cause and affected service

## Correlation Heuristics

| Signal | Confidence |
|--------|-----------|
| Commit within 30 min of alert | High |
| Config file changed | High |
| Dependency update | Medium |
| Refactor of alerting service | Medium |
| Unrelated service commit | Low |

## Tools Available

| Tool | Purpose |
|------|---------|
| `git_recent_commits` | Recent commit log from a tracked repo |
| `git_diff_summary` | File-level diff stats between refs |
| `git_show_file` | Read file at a specific revision |
| `git_blame` | Blame annotation for a file |
| `git_log_path` | History for a specific file/directory |
| `github_list_recent_prs` | Recent pull requests |
| `github_get_pr` | PR details with diff stats |
| `github_list_recent_commits` | Commits via GitHub API |
| `github_get_issue` | Issue details |
| `github_search_issues` | Search issues/PRs |
| `github_get_file_contents` | Read file via GitHub API |
| `github_compare_commits` | Compare two refs |
| `github_create_pr` | Open a new PR |
| `github_create_issue_comment` | Comment on an issue/PR |
