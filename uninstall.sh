#!/usr/bin/env bash
# Memento Vault uninstaller
# Removes hooks, skills, and agents from Claude Code.
# Does NOT delete the vault itself (your notes are safe).
#
# Usage: ./uninstall.sh

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"

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

step "Removing Memento Vault from Claude Code..."

# Remove hooks
for file in memento-triage.py vault-commit.sh memento-sweeper.py; do
    if [ -f "$CLAUDE_DIR/hooks/$file" ]; then
        rm "$CLAUDE_DIR/hooks/$file"
        info "Removed $CLAUDE_DIR/hooks/$file"
    fi
done

# Remove skills
for skill in memento memento-defrag start-fresh continue-work; do
    if [ -d "$CLAUDE_DIR/skills/$skill" ]; then
        rm -rf "$CLAUDE_DIR/skills/$skill"
        info "Removed $CLAUDE_DIR/skills/$skill/"
    fi
done

# Remove agent
if [ -f "$CLAUDE_DIR/agents/concierge.md" ]; then
    rm "$CLAUDE_DIR/agents/concierge.md"
    info "Removed $CLAUDE_DIR/agents/concierge.md"
fi

step "Done!"
echo ""
warn "You still need to manually remove from $CLAUDE_DIR/settings.json:"
echo "  - The SessionEnd hook referencing memento-triage.py"
echo "  - The vault permissions (Read/Edit/Write/Bash entries)"
echo ""
warn "Your vault and config are untouched:"
echo "  - Vault: check ~/.config/memento-vault/memento.yml for location"
echo "  - Config: ~/.config/memento-vault/memento.yml"
echo "  - QMD config: ~/.config/qmd/index.yml"
echo ""
echo "To fully remove everything including your notes:"
echo "  rm -rf \$(grep vault_path ~/.config/memento-vault/memento.yml | awk '{print \$2}')"
echo "  rm -rf ~/.config/memento-vault"
echo ""
