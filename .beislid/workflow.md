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

CodeRabbit is a scarce final-review resource. Do not trigger it for WIP or routine iteration; run local gates and Beislið review first, then opt in by adding the `coderabbit-ready` label or `coderabbit:review` PR body keyword.

```beislid:review_policy
coderabbit:
  mode: opt_in_final_review
  label: coderabbit-ready
  description_keyword: coderabbit:review
risk:
  max_auto_closeout_risk: low
  high_risk_paths:
    - '**/config/**'
    - '**/.github/workflows/**'
    - 'memento/mcp_server.py'
    - 'memento/lifecycle.py'
    - 'memento/pi_bridge.py'
    - 'memento/capture_runtime.py'
    - 'memento/search*.py'
    - 'memento/embedded_search.py'
    - 'memento/graph.py'
    - 'hooks/**'
    - 'install.sh'
    - 'setup-remote.sh'
    - 'bootstrap.sh'
    - 'lib/**'
    - 'Formula/**'
    - 'package.json'
    - 'VERSION'
    - '.beislid/**'
  low_risk_paths:
    - 'docs/**'
    - 'tests/**'
    - '**/*.md'
    - '**/*.markdown'
    - '**/*.mdx'
    - '**/*.rst'
    - 'README*'
    - 'CHANGELOG.md'
  high_risk_file_count: 12
  high_risk_total_changes: 500
  low_risk_file_count: 3
  low_risk_total_changes: 120
```

## Quality gates

The gate list uses Beislið's rich staged metadata while preserving the existing
pre-PR command surface. Older orchestrators may still treat each entry as a flat
`name` + `command` gate; current Beislið/Rondo consumers can use `stage`,
`cost`, selectors, output parsers, and failure policy for automatic work.

```beislid:gates
- name: ruff-check
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m ruff check .'
  timeout_seconds: 120
  cost: cheap
  mutates: false
  parallel_safe: true
  changed_file_selector:
    include: ['memento/**/*.py', 'hooks/**/*.py', 'scripts/**/*.py', 'tests/**/*.py']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    hint: 'Fix lint errors before review; do not skip without a documented environment blocker.'
- name: ruff-format-check
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m ruff format --check .'
  timeout_seconds: 120
  cost: cheap
  mutates: false
  parallel_safe: true
  changed_file_selector:
    include: ['memento/**/*.py', 'hooks/**/*.py', 'scripts/**/*.py', 'tests/**/*.py']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 1
    hint: 'Run the formatter or make equivalent formatting edits, then rerun the gate.'
- name: python-compileall
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m compileall -q memento hooks scripts'
  timeout_seconds: 120
  cost: cheap
  mutates: false
  parallel_safe: true
  changed_file_selector:
    include: ['memento/**/*.py', 'hooks/**/*.py', 'scripts/**/*.py']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    hint: 'Fix syntax/import-time compile errors before review.'
- name: frontmatter-schema-drift
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python scripts/check_frontmatter_schema.py'
  timeout_seconds: 120
  cost: cheap
  mutates: false
  changed_file_selector:
    include: ['docs/frontmatter-schema.md', 'memento/types.py', 'scripts/check_frontmatter_schema.py', 'tests/test_frontmatter_schema.py']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 1
    hint: 'Keep documented frontmatter schema and generated checks in sync.'
- name: targeted-tests
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py tests/test_frontmatter_schema.py tests/test_script_harnesses.py'
  timeout_seconds: 300
  cost: medium
  mutates: false
  changed_file_selector:
    include: ['memento/**/*.py', 'hooks/**/*.py', 'scripts/**/*.py', 'tests/**/*.py']
  output:
    parser: pytest
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    stop_if_patterns:
      - 'No module named'
    hint: 'Fix deterministic test failures; stop and report dependency/environment gaps.'
- name: retrieval-tests
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m pytest tests/test_tenet_*.py tests/test_multi_hop.py tests/test_deep_recall.py'
  timeout_seconds: 600
  cost: expensive
  mutates: false
  changed_file_selector:
    include: ['memento/search*.py', 'memento/embedded_search.py', 'memento/graph.py', 'hooks/tenet_reranker.py', 'tests/test_tenet_*.py', 'tests/test_multi_hop.py', 'tests/test_deep_recall.py']
  output:
    parser: pytest
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    stop_if_patterns:
      - 'No module named'
    hint: 'Preserve recall quality; stop if vector/search dependencies are unavailable.'
- name: mcp-server-tests
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m pytest tests/test_mcp_server.py tests/test_remote_client.py tests/test_integration_remote.py'
  timeout_seconds: 600
  cost: expensive
  mutates: false
  changed_file_selector:
    include: ['memento/mcp_server.py', 'memento/remote_client.py', 'tests/test_mcp_server.py', 'tests/test_remote_client.py', 'tests/test_integration_remote.py']
  output:
    parser: pytest
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    stop_if_patterns:
      - 'No module named'
    hint: 'Keep local and remote MCP surfaces compatible.'
- name: install-tests
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python -m pytest tests/test_install_helpers.py tests/test_install_register_mcp.py'
  timeout_seconds: 300
  cost: medium
  mutates: false
  changed_file_selector:
    include: ['install.sh', 'setup-remote.sh', 'bootstrap.sh', 'lib/**', 'tests/test_install_helpers.py', 'tests/test_install_register_mcp.py']
  output:
    parser: pytest
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 2
    hint: 'Preserve install helper behavior and MCP registration compatibility.'
- name: release-smoke
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python scripts/release_smoke.py'
  timeout_seconds: 300
  cost: medium
  mutates: false
  changed_file_selector:
    include: ['VERSION', 'package.json', 'Formula/**', 'scripts/release_smoke.py', 'install.sh', 'lib/**', 'memento/**/*.py']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 1
    hint: 'Fix release packaging or smoke-check drift before review.'
- name: install-exec-smoke
  stage: pre-pr
  kind: sensor
  execution: computational
  command: '.venv/bin/python scripts/release_smoke.py --install-exec'
  timeout_seconds: 600
  cost: expensive
  mutates: false
  changed_file_selector:
    include: ['install.sh', 'lib/**', 'scripts/release_smoke.py', 'Formula/**', 'package.json']
  output:
    parser: generic-text
    agent_summary: true
  failure:
    retryable: true
    max_fix_iterations: 1
    stop_if_patterns:
      - 'Permission denied'
    hint: 'The gate mutates only a throwaway HOME; stop if the host cannot execute install scripts.'
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
