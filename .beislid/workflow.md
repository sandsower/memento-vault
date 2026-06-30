<!-- beislid-workflow: v1 -->

# Beislið workflow config — memento-vault

## Issue tracker

Linear issues in the personal `teotl` workspace, team `memento`, accessed via Linear MCP.

```beislid:ticket_source
type: mcp
tool: mcp__personal-linear-server__get_issue
id_pattern: '^MEM-\d+$'
link_template: 'https://linear.app/teotl/issue/{id}'
```

```beislid:branch_pattern
^[^/]+/([a-z]+-\d+)
```

```beislid:ticket_update
type: mcp
comment_tool: mcp__personal-linear-server__save_comment
issue_tool: mcp__personal-linear-server__save_issue
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
- name: frontmatter-schema-drift
  command: '.venv/bin/python scripts/check_frontmatter_schema.py'
- name: targeted-tests
  command: '.venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py'
- name: retrieval-tests
  command: '.venv/bin/python -m pytest tests/test_tenet_*.py tests/test_multi_hop.py tests/test_deep_recall.py'
- name: mcp-server-tests
  command: '.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_remote_client.py tests/test_integration_remote.py'
- name: install-tests
  command: '.venv/bin/python -m pytest tests/test_install_helpers.py tests/test_install_register_mcp.py'
- name: release-smoke
  command: '.venv/bin/python scripts/release_smoke.py'
- name: install-exec-smoke
  command: '.venv/bin/python scripts/release_smoke.py --install-exec'
```

## Action policy

Unattended completion handoff is allowed for feature branches only after the configured gates pass. Use the repo policy file when evaluating remote handoff actions:

```bash
beislid action-policy evaluate --policy-file .beislid/action-policy.json --mode unattended-auto --action git.push --class git-remote --sandbox-baseline non-default-branch
beislid action-policy evaluate --policy-file .beislid/action-policy.json --mode unattended-auto --action gh.pr.create --class git-remote --sandbox-baseline non-default-branch
beislid action-policy evaluate --policy-file .beislid/action-policy.json --mode unattended-auto --action gh.pr.ready --class git-remote --sandbox-baseline non-default-branch
beislid action-policy evaluate --policy-file .beislid/action-policy.json --mode unattended-auto --action gh.pr.comment --class git-remote --sandbox-baseline non-default-branch
```

The permitted terminal path for unattended agents is: finish local work, run all configured gates, push the non-default feature branch, create or update a draft PR, link it on the Linear ticket, and leave the ticket in `In Review`. If all configured gates pass and the existing PR is a green draft, unattended agents may mark that PR ready for review (`gh.pr.ready`) and may post bounded PR comments whose sole purpose is to trigger configured review automation or record review-handoff state (`gh.pr.comment`). Unattended agents must not push from the default branch and must not merge automatically.

```beislid:action_policy
policy_file: .beislid/action-policy.json
run_mode: unattended-auto
modes:
  supervised-auto:
    rules:
      workspace-write: allow
    actions:
      git.branch: allow
      git.commit: allow
      git.merge: allow
      gh.pr.ready: allow
      gh.pr.comment: allow
      review.fix: allow
    sandbox:
      minimum: none
      on_uncommitted_changes: allow
  unattended-auto:
    rules:
      workspace-write: allow
      dependency-install: allow
      git-remote: deny
    actions:
      git.branch: allow
      git.commit: allow
      git.push: allow
      gh.pr.create: allow
      gh.pr.ready: allow
      gh.pr.comment: allow
      tracker.issue.transition: allow
      ticket.comment: allow
    sandbox:
      minimum: non-default-branch
      on_uncommitted_changes: allow
```

## Probe cache

```beislid:probe_cache
ttl_hours: 24
```
