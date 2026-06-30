#!/usr/bin/env bash
# Memento Vault uninstaller
# Removes installed hooks, skills, agents, package files, MCP registrations,
# and Claude/Codex settings entries created by install.sh.
# Does NOT delete the vault itself (your notes are safe).
#
# Usage: ./uninstall.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
CONFIG_DIR="$HOME/.config/memento-vault"
HELPER="$SCRIPT_DIR/lib/install_helpers.py"
VAULT_PATH="${MEMENTO_VAULT_PATH:-$HOME/memento}"

if [ -t 1 ]; then
    BOLD='\033[1m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    NC='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' NC=''
fi

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
step()  { echo -e "\n${BOLD}$1${NC}"; }

remove_file() {
    local path="$1"
    if [ -f "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        info "Removed $path"
    fi
}

remove_dir() {
    local path="$1"
    if [ -d "$path" ]; then
        rm -rf "$path"
        info "Removed $path/"
    fi
}

load_vault_path() {
    if [ -f "$CONFIG_DIR/manifest.json" ] && command -v python3 >/dev/null 2>&1; then
        local manifest_vault
        manifest_vault=$(python3 - "$CONFIG_DIR/manifest.json" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("vault_path", ""))
PY
)
        if [ -n "$manifest_vault" ]; then
            VAULT_PATH="$manifest_vault"
            return
        fi
    fi

    if [ -f "$CONFIG_DIR/memento.yml" ]; then
        local config_vault
        config_vault=$(awk -F: '/^vault_path:/ {sub(/^[[:space:]]+/, "", $2); print $2; exit}' "$CONFIG_DIR/memento.yml" 2>/dev/null || true)
        if [ -n "$config_vault" ]; then
            VAULT_PATH="${config_vault/#\~/$HOME}"
        fi
    fi
}

step "Removing Memento Vault from Claude Code..."

load_vault_path

# Remove Claude hooks installed by install.sh (stable + experimental).
for file in \
    memento-triage.py \
    vault-commit.sh \
    memento-sweeper.py \
    wait-and-commit.py \
    _backfill_certainty.py \
    memento-remote-sync.py \
    memento_utils.py \
    vault-briefing.py \
    vault-recall.py \
    vault-tool-context.py \
    memento-inception.py \
    tenet_reranker.py; do
    remove_file "$CLAUDE_DIR/hooks/$file"
done

# Remove the installed Python package copied under Claude hooks.
remove_dir "$CLAUDE_DIR/hooks/memento"

# Remove Claude skills installed by install.sh (stable + experimental).
for skill in memento memento-defrag start-fresh continue-work inception orra-init; do
    remove_dir "$CLAUDE_DIR/skills/$skill"
done

# Remove Codex/generic skills installed when Codex was present.
for skill in memento memento-defrag start-fresh continue-work concierge inception; do
    remove_dir "$CODEX_HOME_DIR/skills/$skill"
done

# Remove Claude agent.
remove_file "$CLAUDE_DIR/agents/concierge.md"

# Remove installer-created remote environment file.
remove_file "$CLAUDE_DIR/memento-remote.env"

# Remove installer-created CLI symlink only when it points at this checkout.
cli_dest="${MEMENTO_CLI_BIN_DIR:-$HOME/.local/bin}/memento-vault"
if [ -L "$cli_dest" ]; then
    cli_target=$(readlink "$cli_dest" || true)
    if [ "$cli_target" = "$SCRIPT_DIR/bin/memento-vault" ]; then
        remove_file "$cli_dest"
    else
        warn "Leaving CLI symlink at $cli_dest (points to $cli_target)"
    fi
elif [ -e "$cli_dest" ]; then
    warn "Leaving CLI at $cli_dest (not an installer-created symlink)"
fi

# Remove installer-owned shell warmup blocks while preserving user shell config.
step "Removing shell warmup blocks..."
if command -v python3 >/dev/null 2>&1; then
    python3 - "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.config/fish/config.fish" <<'PY'
from pathlib import Path
import sys

markers = ("qmd vsearch", "python3 -c", "memento-vault warmup")
for arg in sys.argv[1:]:
    path = Path(arg)
    if not path.exists():
        continue
    lines = path.read_text().splitlines(keepends=True)
    kept = []
    removed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("# Warm QMD embedding model on shell startup") and any(m in next_line for m in markers):
            removed += 1
            i += 2
            continue
        kept.append(line)
        i += 1
    if removed:
        path.write_text("".join(kept))
        print(f"Removed {removed} memento warmup block(s) from {path}")
PY
else
    warn "python3 not available; skipping shell warmup cleanup"
fi

# Remove MCP registrations and generic MCP config entry.
step "Removing MCP registrations..."
if command -v claude >/dev/null 2>&1; then
    claude mcp remove memento-vault -s user >/dev/null 2>&1 && info "Removed Claude MCP registration" || true
fi
if command -v codex >/dev/null 2>&1; then
    codex mcp remove memento-vault >/dev/null 2>&1 && info "Removed Codex MCP registration" || true
fi
if command -v python3 >/dev/null 2>&1 && [ -f "$HELPER" ]; then
    python3 "$HELPER" uninstall-mcp-config "$CLAUDE_DIR" || warn "Could not update $CLAUDE_DIR/mcp-servers.json"
else
    warn "python3 not available; skipping mcp-servers.json cleanup"
fi

# Remove Claude settings hooks and permissions owned by memento-vault.
step "Updating Claude Code settings..."
if command -v python3 >/dev/null 2>&1 && [ -f "$HELPER" ]; then
    python3 "$HELPER" uninstall-settings "$CLAUDE_DIR/settings.json" "$CLAUDE_DIR" "$VAULT_PATH" || warn "Could not update $CLAUDE_DIR/settings.json"
else
    warn "python3 not available; skipping settings.json cleanup"
fi

step "Done!"
echo ""
warn "Your vault and config are untouched:"
echo "  - Vault: $VAULT_PATH"
echo "  - Config: $CONFIG_DIR/memento.yml"
echo "  - Install manifest/state: $CONFIG_DIR/manifest.json and $CONFIG_DIR/base/"
echo "  - QMD config: ~/.config/qmd/index.yml"
echo ""
echo "To fully remove everything including your notes:"
echo "  rm -rf \"$VAULT_PATH\""
echo "  rm -rf ~/.config/memento-vault"
echo ""
