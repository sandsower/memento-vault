---
# Rondo execution profile — memento-vault
# Run rondo against this profile to execute Linear issues from the
# "Rondo intake — memento" project. Adding an issue to that project is the
# explicit AFK opt-in. Envelope-driven runs use `rondo run-once --manifest`
# and override tracker polling entirely (pilot envelopes: docs/plans/envelopes/).
tracker:
  kind: linear
  api_key: "$LINEAR_API_KEY"
  project_slug: "rondo-intake-memento-e41ef5a4ca45"
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
polling:
  interval_ms: 30000
workspace:
  root: ~/code/rondo-workspaces
hooks:
  after_create: |
    git clone --depth 1 git@github.com:sandsower/memento-vault.git .
    python3 -m venv .venv
    .venv/bin/pip install --quiet -e '.[mcp]' pytest ruff
  timeout_ms: 300000
gates:
  - name: ruff-check
    command: .venv/bin/python -m ruff check .
  - name: ruff-format-check
    command: .venv/bin/python -m ruff format --check .
  - name: python-compileall
    command: .venv/bin/python -m compileall -q memento hooks scripts
  - name: targeted-tests
    command: .venv/bin/python -m pytest tests/test_llm_backends.py tests/test_lifecycle.py tests/test_triage.py tests/test_store.py
    timeout_ms: 600000
agent:
  adapter: pi
  max_concurrent_agents: 2
  max_turns: 20
claude:
  command: claude
  permission_mode: bypassPermissions
  dangerously_skip_permissions: true
  output_format: stream-json
pi:
  command: pi
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
action_policy:
  command: beislid
  run_mode: unattended-auto
  policy_file: /Users/vicvalenzuela/Personal/memento-vault/.beislid/action-policy.json
process_provider:
  kind: beislid
  required: false
---

You are working on Linear ticket `{{ issue.identifier }}` in the memento-vault
repo (Memento — memory/learning store for coding agents; never a run ledger).

Issue context:
Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
Current status: {{ issue.state }}
URL: {{ issue.url }}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

Instructions:

1. This is an unattended orchestration session. Never ask a human to perform
   follow-up actions; only stop for a true blocker (missing auth/permissions).
2. Work only in the provided repository copy.
3. Maintain a single persistent Linear workpad comment as the source of truth
   for progress; bring it up to date before new implementation work.
4. Treat any ticket-authored Validation/Test Plan section as non-negotiable
   acceptance input; execute it before considering the work complete.
5. Project conventions live in `.beislid/workflow.md` (gates, action policy,
   ticket/PR conventions). Run the configured gates before any push.
6. Out-of-scope discoveries become new Linear issues in the same project,
   linked `related`, never scope expansion.
7. Final message reports completed actions and blockers only.
