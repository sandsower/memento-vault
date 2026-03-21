#!/usr/bin/env bash
# vault-commit.sh — auto-commit all changes in the memento vault.
# Called by memento-triage.py, memento-sweeper.py, /memento, /memento-defrag.
# Idempotent: exits cleanly if nothing to commit.
#
# Usage: vault-commit.sh [commit message]
# Default message: "auto: vault update"

set -euo pipefail

# Resolve vault path from config, falling back to ~/memento
resolve_vault() {
    local config_files=(
        "$HOME/.config/memento-vault/memento.yml"
        "$HOME/.memento-vault.yml"
    )
    for cfg in "${config_files[@]}"; do
        if [ -f "$cfg" ]; then
            local path
            path=$(grep -E '^vault_path:' "$cfg" 2>/dev/null | sed 's/^vault_path:[[:space:]]*//' | sed 's/^["'"'"']//;s/["'"'"']$//' | sed "s|^~|$HOME|")
            if [ -n "$path" ] && [ -d "$path" ]; then
                echo "$path"
                return
            fi
        fi
    done
    echo "$HOME/memento"
}

VAULT="$(resolve_vault)"
MSG="${1:-auto: vault update}"

cd "$VAULT"

# Init repo if somehow missing
if [ ! -d .git ]; then
    git init
    git add -A
    git commit -m "init: bootstrap memento vault"
    exit 0
fi

# Stage everything (new files, modifications, deletions)
git add -A

# Only commit if there are staged changes
if ! git diff --cached --quiet; then
    git commit -m "$MSG"
fi
