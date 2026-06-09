<!-- beislid-workflow: v1 -->

# Beislið workflow config — memento-vault

## Issue tracker

Linear issues in the personal `teotl` workspace, team `memento`, accessed via Linear MCP.

```beislid:ticket_source
type: mcp
tool: mcp__linear_personal__get_issue
id_pattern: '^MEM-\d+$'
link_template: 'https://linear.app/teotl/issue/{id}'
```

```beislid:branch_pattern
^[^/]+/([a-z]+-\d+)
```

```beislid:ticket_update
type: mcp
comment_tool: mcp__linear_personal__save_comment
issue_tool: mcp__linear_personal__save_issue
```

## PR reviews

```beislid:pr_review_source
type: cli
summary_command: 'gh pr view --json url,number,reviewDecision,reviews,comments'
threads_command: 'gh api repos/{owner}/{repo}/pulls/{number}/comments --paginate'
```

```beislid:pr_review_update
type: cli
reply_command: 'gh api repos/{owner}/{repo}/pulls/{number}/comments --method POST --input {json_file}'
rerequest_command: 'gh api repos/{owner}/{repo}/pulls/{number}/requested_reviewers --method POST --input {json_file}'
```

## Quality gates

```beislid:gates
- name: ruff-check
  command: '.venv/bin/python -m ruff check .'
- name: ruff-format-check
  command: '.venv/bin/python -m ruff format --check .'
- name: python-compileall
  command: '.venv/bin/python -m compileall -q memento hooks scripts'
- name: targeted-tests
  command: '.venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py'
- name: retrieval-tests
  command: '.venv/bin/python -m pytest tests/test_tenet_*.py tests/test_multi_hop.py tests/test_deep_recall.py'
- name: mcp-server-tests
  command: '.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_remote_client.py tests/test_integration_remote.py'
- name: install-tests
  command: '.venv/bin/python -m pytest tests/test_install_helpers.py tests/test_install_register_mcp.py'
- name: release-smoke
  command: '.venv/bin/python scripts/release_smoke.py'
- name: claude-sandbox-smoke
  command: '.venv/bin/python scripts/claude_sandbox_smoke.py'
```

## Action policy

```beislid:action_policy
modes:
  supervised-auto:
    rules:
      workspace-write: allow
    actions:
      git.branch: allow
      git.commit: allow
      git.merge: allow
      review.fix: allow
    sandbox:
      on_uncommitted_changes: allow
```

## Probe cache

```beislid:probe_cache
ttl_hours: 24
```
