<!-- beislid-workflow: v1 -->

# Beislið workflow config — memento-vault

## Issue tracker

GitHub Issues on `sandsower/memento-vault`, accessed via the `gh` CLI.

```beislid:ticket_source
type: cli
command: 'gh issue view {id} --json title,body,labels'
id_pattern: '^#?\d+$'
link_template: 'https://github.com/sandsower/memento-vault/issues/{id}'
```

```beislid:ticket_update
type: cli
comment_command: 'gh issue comment {id} --body-file {body_file}'
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
- name: targeted-tests
  command: '.venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py'
- name: claude-sandbox-smoke
  command: '.venv/bin/python scripts/claude_sandbox_smoke.py'
```

## Probe cache

```beislid:probe_cache
ttl_hours: 24
```
