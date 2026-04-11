#!/usr/bin/env bash
# Memento Vault bootstrap — one-liner install:
#   curl -fsSL https://raw.githubusercontent.com/sandsower/memento-vault/main/bootstrap.sh | bash
#
# Pass flags through:
#   curl -fsSL ... | bash -s -- --experimental --remote https://vault.example.com:8745
set -euo pipefail

REPO="https://github.com/sandsower/memento-vault.git"
INSTALL_DIR="${MEMENTO_INSTALL_DIR:-$HOME/.local/share/memento-vault}"

# Colors
if [ -t 1 ]; then
    BOLD='\033[1m' GREEN='\033[0;32m' NC='\033[0m'
else
    BOLD='' GREEN='' NC=''
fi

echo -e "${BOLD}Memento Vault${NC} — bootstrap installer"
echo ""

if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${GREEN}[+]${NC} Updating existing install at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || git -C "$INSTALL_DIR" pull --rebase
else
    echo -e "${GREEN}[+]${NC} Cloning memento-vault to $INSTALL_DIR..."
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi

echo ""
exec "$INSTALL_DIR/install.sh" "$@"
