---
name: orra-init
description: Initialize orra in the current repo. Runs orra_setup, installs all stock directives, and adds vault-bridge when memento-vault is available. Use when Vic says "init orra", "orra setup", "set up orra here", or invokes /orra-init.
---

# orra-init

Scaffold orra in the current repo: run setup, install the stock directive set, and wire up memento integration via vault-bridge when memento is live.

This skill is shipped by memento-vault as an experimental integration, gated behind `./install.sh --experimental`. It brings two systems together without either becoming a hard dependency of the other: orra continues to work without memento, memento continues to work without orra, and the vault-bridge directive is the only coupling surface.

## When to use

- User invokes `/orra-init`
- User says "init orra", "set up orra in this repo", "bootstrap orra", or similar
- User has cloned a new repo and wants orra ready to orchestrate

## Preflight

Orra requires a git repo. Run `git rev-parse --git-dir` from the current working directory. If it fails, tell the user orra needs a git repo (offer `git init` if they want one) and stop. Do not call `orra_setup` outside a repo.

## Steps

1. **Load deferred tools if needed.** If `orra_setup`, `orra_directive`, and `memento_status` are not already loaded, load them in one batch:

   ```
   ToolSearch("select:mcp__orra__orra_setup,mcp__orra__orra_directive,mcp__memento-vault__memento_status")
   ```

2. **Run `orra_setup`.** Creates `.orra/config.json`, installs the orchestrator persona to `.claude/agents/orchestrator.md`, adds `.orra/` to `.gitignore`, scaffolds `.orra/memory/`. Idempotent; safe on repos that already have `.orra/`.

3. **Install all stock directives.** Call `orra_directive({action: "install-all"})`. Existing customized directives are preserved (the tool skips them explicitly).

4. **Check memento availability.** Call `memento_status`. A healthy response (returns `vault_exists: true` or a positive `note_count`) means memento is live. Error or unhealthy response means memento is not set up; skip vault-bridge silently, that's a supported configuration.

5. **Install vault-bridge (if memento is live).**
   - If `.orra/directives/vault-bridge.md` already exists: leave it alone and note that the user has a local copy (possibly customized).
   - If it does not exist: copy the template.
     ```bash
     cp ~/.claude/skills/orra-init/templates/vault-bridge.md .orra/directives/vault-bridge.md
     ```

6. **Patch anchor lines into the four memory-using directives (if vault-bridge is installed).** This step is what actually lets vault-bridge intercept memory operations; without it, stock directives write to `.orra/memory/` and ignore memento. Orra's `install-all` pulls stock templates from the npm package that have no anchor, so the patch must be re-applied every time fresh directives land.

   The exact anchor text to insert (copy verbatim):

   ```markdown
   > **Memory routing:** if `vault-bridge.md` is present in `.orra/directives/`, follow its routing table for memory reads and writes. The `.orra/memory/*` paths described below are the default when vault-bridge is not installed.
   ```

   For each of `morning-briefing.md`, `shutdown-ritual.md`, `memory-recall.md`, `linear-deadline-tracker.md` in `.orra/directives/`:

   - If the file already contains `**Memory routing:** if \`vault-bridge.md\``, skip (idempotent).
   - Otherwise, find the FIRST `### ` heading in the file and insert the anchor blockquote plus a blank line immediately BEFORE that heading. This places the anchor after the directive's intro paragraph and before its first section, giving the model the routing hint before it follows any storage instructions.

   This step is idempotent and safe to re-run after every `install-all` that might have refreshed stock directives.

7. **Report.** One-screen summary:
   - Orra scaffolded at `.orra/` (or "already present, no-op")
   - N directives installed, M skipped
   - Vault-bridge: installed | already present | skipped (memento unavailable)
   - Memento status: note count, project count, fleeting count (if healthy)
   - Next step: `claude --agent orchestrator` to start an orchestrator session in this repo

## Rules

- Never overwrite `.orra/directives/*.md` files that already exist. Both `orra_directive install-all` and the vault-bridge copy respect local customizations.
- If memento is unavailable, that is a normal outcome, not an error. The user may be in a repo where they intentionally want orra without memento coupling.
- Do not run `orra_setup` outside a git repo. The tool will error with `fatal: not a git repository`. Preflight catches this.
- The only modification to stock directives is the Step 6 anchor-line patch for the four memory-using directives, and only when vault-bridge is installed. Do not make other edits to stock directives. If orra's package ships updated templates via `install-all`, re-running `/orra-init` is the path to re-apply the anchor.

## Related skills and next steps

- After this skill runs, start an orchestrator session: `claude --agent orchestrator`. Morning-briefing will fire on session start.
- To customize which directives get installed in this repo, use `orra_directive({action: "remove", name: "..."})` or `install` individual ones by name.
- To refresh vault-bridge from the canonical template, `rm .orra/directives/vault-bridge.md` first, then re-run `/orra-init`.
