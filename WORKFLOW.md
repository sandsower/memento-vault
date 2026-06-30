---
# Rondo execution profile — memento-vault
# Run `./scripts/run-rondo` against this profile to execute Linear issues from the
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
    - In Review
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
    .venv/bin/python -m pip install --quiet --upgrade pip setuptools wheel
    .venv/bin/python -m pip install --quiet -e '.[mcp]' pytest ruff
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
model_routing:
  defaults:
    tier: standard
    mode: prefer
  tiers:
    light:
      - adapter: pi
        model: openrouter/deepseek/deepseek-chat
    standard:
      # Subscription tier has been upgraded; prefer Codex for primary workflow
      # execution and retain OpenRouter only as fallback capacity.
      - adapter: pi
        model: openai-codex/gpt-5.4-mini
      - adapter: pi
        model: openrouter/deepseek/deepseek-v4-pro
    heavy:
      - adapter: pi
        model: openai-codex/gpt-5.5
      - adapter: pi
        model: openrouter/deepseek/deepseek-v4-pro
      - adapter: pi
        model: openrouter/z-ai/glm-5.2
    frontier:
      - adapter: pi
        model: openai-codex/gpt-5.5
      - adapter: pi
        model: openrouter/deepseek/deepseek-v4-pro
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

## In Review babysit loop

When the ticket status is `In Review`, do not start new feature work. Treat the run as a review/babysit loop:

1. Find the linked/open PR for the issue branch; if none exists, move the ticket back to `In Progress`, update the workpad with the missing review artifact, and stop.
2. Read top-level PR comments, inline review comments, reviews, CI/check status, mergeability, and branch freshness.
3. Treat every actionable human/bot comment as blocking until it is fixed and replied to, or an explicit justified pushback is posted.
4. Run the configured workflow gates before every babysit-owned push or merge boundary.
5. Only leave the ticket in `In Review` when the PR is reviewable, checks are green or legitimately pending human review, and unresolved actionable feedback is recorded in the workpad. If changes are required, move the ticket to `In Progress` and execute the fixes end-to-end.
6. Never merge automatically from this prompt-level fallback unless the workflow has native release-loop support and action policy permits it.
