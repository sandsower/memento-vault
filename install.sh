#!/usr/bin/env bash
# Memento Vault installer
# Installs hooks, skills, and agents into Claude Code, then initializes the vault.
#
# Usage:
#   git clone https://github.com/sandsower/memento-vault.git
#   cd memento-vault
#   ./install.sh
#
# Or with a custom vault path:
#   MEMENTO_VAULT_PATH=~/my-vault ./install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
VAULT_PATH="${MEMENTO_VAULT_PATH:-$HOME/memento}"
CONFIG_DIR="$HOME/.config/memento-vault"

# Colors (if terminal supports them)
if [ -t 1 ]; then
    BOLD='\033[1m'
    DIM='\033[2m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    NC='\033[0m'
else
    BOLD='' DIM='' GREEN='' YELLOW='' RED='' NC=''
fi

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; }
step()  { echo -e "\n${BOLD}$1${NC}"; }

# --- Preflight checks ---

step "Checking prerequisites..."

if ! command -v git &>/dev/null; then
    error "git is required but not installed."
    exit 1
fi
info "git: $(git --version | head -1)"

if ! command -v python3 &>/dev/null; then
    error "python3 is required but not installed."
    exit 1
fi
info "python3: $(python3 --version)"

if ! command -v claude &>/dev/null; then
    warn "Claude Code CLI not found. Hooks and skills require Claude Code to function."
    warn "Install it from: https://docs.anthropic.com/en/docs/claude-code"
    echo ""
    read -rp "Continue anyway? [y/N] " cont
    if [[ ! "$cont" =~ ^[Yy] ]]; then
        exit 0
    fi
else
    info "claude: $(claude --version 2>/dev/null || echo 'installed')"
fi

if command -v qmd &>/dev/null; then
    info "qmd: installed (semantic search enabled)"
    QMD_AVAILABLE=true
else
    warn "qmd not found. Semantic search will be disabled (grep fallback works fine)."
    warn "Install qmd later for better search: https://github.com/tobi/qmd"
    QMD_AVAILABLE=false
fi

# --- Create vault ---

step "Setting up vault at $VAULT_PATH..."

if [ -d "$VAULT_PATH" ]; then
    info "Vault directory already exists, preserving contents."
else
    mkdir -p "$VAULT_PATH"
    info "Created $VAULT_PATH"
fi

# Create subdirectories
for dir in fleeting notes projects archive; do
    mkdir -p "$VAULT_PATH/$dir"
done
info "Directory structure: fleeting/ notes/ projects/ archive/"

# Copy .gitignore if not present
if [ ! -f "$VAULT_PATH/.gitignore" ]; then
    cp "$SCRIPT_DIR/templates/vault/.gitignore" "$VAULT_PATH/.gitignore"
    info "Added .gitignore"
fi

# Initialize git repo if not present
if [ ! -d "$VAULT_PATH/.git" ]; then
    git -C "$VAULT_PATH" init
    git -C "$VAULT_PATH" add -A
    git -C "$VAULT_PATH" commit -m "init: bootstrap memento vault" --allow-empty
    info "Initialized git repository"
else
    info "Git repo already initialized"
fi

# --- Obsidian setup (optional) ---

echo ""
read -rp "Set up Obsidian views? (Base views for browsing notes) [Y/n] " obsidian
if [[ ! "$obsidian" =~ ^[Nn] ]]; then
    # Copy .obsidian config
    if [ ! -d "$VAULT_PATH/.obsidian" ]; then
        cp -r "$SCRIPT_DIR/templates/obsidian/.obsidian" "$VAULT_PATH/.obsidian"
        info "Added Obsidian config (.obsidian/)"
    else
        info "Obsidian config already exists, skipping"
    fi

    # Copy base views
    for base in "$SCRIPT_DIR"/templates/obsidian/*.base; do
        basename=$(basename "$base")
        if [ ! -f "$VAULT_PATH/$basename" ]; then
            cp "$base" "$VAULT_PATH/$basename"
        fi
    done
    info "Added Base views (by-type, by-project, recent, decisions, bugfixes, by-source, by-tag)"
fi

# --- Config file ---

step "Writing config..."

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/memento.yml" ]; then
    sed "s|~/memento|$VAULT_PATH|g" "$SCRIPT_DIR/memento.yml.example" > "$CONFIG_DIR/memento.yml"
    info "Created $CONFIG_DIR/memento.yml"
else
    info "Config already exists at $CONFIG_DIR/memento.yml"
fi

# --- Install hooks ---

step "Installing Claude Code hooks..."

mkdir -p "$CLAUDE_DIR/hooks"
cp "$SCRIPT_DIR/hooks/memento-triage.py" "$CLAUDE_DIR/hooks/memento-triage.py"
cp "$SCRIPT_DIR/hooks/vault-commit.sh" "$CLAUDE_DIR/hooks/vault-commit.sh"
cp "$SCRIPT_DIR/hooks/memento-sweeper.py" "$CLAUDE_DIR/hooks/memento-sweeper.py"
chmod +x "$CLAUDE_DIR/hooks/vault-commit.sh"
info "Copied hooks to $CLAUDE_DIR/hooks/"

# --- Install skills ---

step "Installing Claude Code skills..."

for skill in memento memento-defrag start-fresh continue-work; do
    mkdir -p "$CLAUDE_DIR/skills/$skill"
    cp "$SCRIPT_DIR/skills/$skill/SKILL.md" "$CLAUDE_DIR/skills/$skill/SKILL.md"
done
info "Copied skills to $CLAUDE_DIR/skills/"

# --- Install agents ---

step "Installing Claude Code agents..."

mkdir -p "$CLAUDE_DIR/agents"
cp "$SCRIPT_DIR/agents/concierge.md" "$CLAUDE_DIR/agents/concierge.md"
info "Copied concierge agent to $CLAUDE_DIR/agents/"

# --- Merge settings.json ---

step "Updating Claude Code settings..."

SETTINGS="$CLAUDE_DIR/settings.json"

if [ ! -f "$SETTINGS" ]; then
    # No existing settings — create from scratch
    cat > "$SETTINGS" << SETTINGS_EOF
{
  "hooks": {
    "SessionEnd": [
      {
        "type": "command",
        "command": "python3 $CLAUDE_DIR/hooks/memento-triage.py",
        "timeout": 30000,
        "async": true
      }
    ]
  },
  "permissions": {
    "allow": [
      "Read($VAULT_PATH/**)",
      "Edit($VAULT_PATH/**)",
      "Write($VAULT_PATH/**)",
      "Bash($CLAUDE_DIR/hooks/vault-commit.sh:*)"
    ]
  }
}
SETTINGS_EOF
    info "Created $SETTINGS with memento hooks and permissions"
else
    # Settings exist — check if our hook is already there
    if grep -q "memento-triage" "$SETTINGS"; then
        info "SessionEnd hook already configured"
    else
        warn "Existing settings.json found. You need to add the hook manually."
        echo ""
        echo -e "${DIM}Add to your settings.json under \"hooks\":${NC}"
        echo ""
        cat << 'HOOK_EOF'
  "SessionEnd": [
    {
      "type": "command",
      "command": "python3 ~/.claude/hooks/memento-triage.py",
      "timeout": 30000,
      "async": true
    }
  ]
HOOK_EOF
        echo ""
        echo -e "${DIM}And under \"permissions\".\"allow\":${NC}"
        echo ""
        cat << PERM_EOF
  "Read($VAULT_PATH/**)",
  "Edit($VAULT_PATH/**)",
  "Write($VAULT_PATH/**)",
  "Bash($CLAUDE_DIR/hooks/vault-commit.sh:*)"
PERM_EOF
        echo ""
    fi
fi

# --- QMD setup (optional) ---

if [ "$QMD_AVAILABLE" = true ]; then
    step "Setting up QMD collection..."

    QMD_CONFIG="$HOME/.config/qmd/index.yml"
    if [ -f "$QMD_CONFIG" ]; then
        if grep -q "memento:" "$QMD_CONFIG"; then
            info "QMD memento collection already configured"
        else
            warn "QMD config exists but has no memento collection."
            echo -e "${DIM}Add this to $QMD_CONFIG under collections:${NC}"
            echo ""
            echo "  memento:"
            echo "    path: $VAULT_PATH"
            echo '    pattern: "**/*.md"'
            echo "    context:"
            echo '      "": Personal knowledge vault with session notes, decisions, discoveries, and project history.'
        fi
    else
        mkdir -p "$(dirname "$QMD_CONFIG")"
        sed "s|~/memento|$VAULT_PATH|g" "$SCRIPT_DIR/templates/qmd-collection.yml" > "$QMD_CONFIG"
        info "Created QMD config at $QMD_CONFIG"
    fi

    # Initial index
    echo ""
    read -rp "Run initial QMD indexing now? [Y/n] " index_now
    if [[ ! "$index_now" =~ ^[Nn] ]]; then
        qmd update -c memento && qmd embed
        info "QMD index built"
    fi
fi

# --- Done ---

step "Installation complete!"
echo ""
echo "Your vault is at: $VAULT_PATH"
echo ""
echo "What happens now:"
echo "  - Every time a Claude Code session ends, the triage hook captures it"
echo "  - Trivial sessions get a one-liner in fleeting/"
echo "  - Substantial sessions spawn a background agent that writes atomic notes"
echo "  - Use /memento to manually capture insights during a session"
echo "  - Use /memento-defrag monthly to archive stale notes"
echo "  - Use /continue-work to pick up where you left off"
echo "  - Use /start-fresh to checkpoint and clear context"
echo ""
if [ "$QMD_AVAILABLE" = true ]; then
    echo "Search: qmd search \"your query\" -c memento"
else
    echo "Search: grep -r \"your query\" $VAULT_PATH/notes/"
    echo "  (install qmd for semantic search: https://github.com/tobi/qmd)"
fi
if [[ ! "$obsidian" =~ ^[Nn] ]]; then
    echo "Browse: open $VAULT_PATH in Obsidian"
fi
echo ""
