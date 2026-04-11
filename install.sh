#!/usr/bin/env bash
# Memento Vault installer
# Installs hooks, skills, and agents into Claude Code, then initializes the vault.
# Version-aware: tracks installed file checksums so upgrades skip user-modified files.
#
# Usage:
#   git clone https://github.com/sandsower/memento-vault.git
#   cd memento-vault
#   ./install.sh
#
# Or with a custom vault path:
#   MEMENTO_VAULT_PATH=~/my-vault ./install.sh
#
# Install experimental modules (Tenet retrieval + Inception consolidation):
#   ./install.sh --experimental
#
# Install MCP server config (for agents without native hook support):
#   ./install.sh --mcp
#
# Connect to a remote vault (Docker or hosted):
#   ./install.sh --remote https://vault.example.com:8745
#
# Force overwrite all files (ignore local changes):
#   ./install.sh --force

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
VAULT_PATH="${MEMENTO_VAULT_PATH:-$HOME/memento}"
CONFIG_DIR="$HOME/.config/memento-vault"
MANIFEST="$CONFIG_DIR/manifest.json"
NEW_VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "0.0.0")
FORCE=false
EXPERIMENTAL=false
MCP_INSTALL=false
REMOTE_URL=""
REMOTE_MODE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=true; shift ;;
        --experimental) EXPERIMENTAL=true; shift ;;
        --mcp) MCP_INSTALL=true; shift ;;
        --remote)
            REMOTE_MODE=true
            if [[ $# -gt 1 && ! "$2" == --* ]]; then
                REMOTE_URL="$2"
                shift 2
            else
                shift
            fi
            ;;
        *) shift ;;
    esac
done

# Load library (logging, manifest helpers, safe_copy, setup functions)
MANIFEST_FILES_JSON="{}"
source "$SCRIPT_DIR/lib/install-lib.sh"
HELPER="$SCRIPT_DIR/lib/install_helpers.py"

# --- Load existing manifest ---

load_manifest

# On reinstall/upgrade, prefer the vault path from the previous install
# unless the user explicitly overrode it via MEMENTO_VAULT_PATH.
if [ -z "${MEMENTO_VAULT_PATH:-}" ] && [ -n "$MANIFEST_VAULT_PATH" ]; then
    VAULT_PATH="$MANIFEST_VAULT_PATH"
fi

# --- Remote mode setup ---

REMOTE_API_KEY=""

if [ "$REMOTE_MODE" = true ]; then
    if [ -z "$REMOTE_URL" ]; then
        echo ""
        read -rp "Remote vault URL (e.g., https://vault.example.com:8745): " REMOTE_URL
        if [ -z "$REMOTE_URL" ]; then
            error "Remote URL is required for --remote mode."
            exit 1
        fi
    fi

    REMOTE_API_KEY="${MEMENTO_API_KEY:-}"
    if [ -z "$REMOTE_API_KEY" ]; then
        read -rp "API key for remote vault (leave empty if none): " REMOTE_API_KEY
    fi

    info "Remote mode: hooks will connect to $REMOTE_URL"
    if [ -n "$REMOTE_API_KEY" ]; then
        info "API key: configured"
    else
        warn "No API key set. Remote vault must allow unauthenticated access."
    fi

    MCP_INSTALL=true
fi

if [ -n "$INSTALLED_VERSION" ]; then
    if [ "$INSTALLED_VERSION" = "$NEW_VERSION" ] && [ "$FORCE" != true ]; then
        info "Memento Vault v${NEW_VERSION} is already installed."
        read -rp "Reinstall anyway? [y/N] " reinstall
        if [[ ! "$reinstall" =~ ^[Yy] ]]; then
            exit 0
        fi
    else
        info "Upgrading Memento Vault: v${INSTALLED_VERSION} -> v${NEW_VERSION}"
    fi
else
    info "Installing Memento Vault v${NEW_VERSION}"
fi

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

setup_vault

# --- Config file ---

step "Writing config..."

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/memento.yml" ]; then
    sed "s|~/memento|$VAULT_PATH|g" "$SCRIPT_DIR/memento.yml.example" > "$CONFIG_DIR/memento.yml"
    info "Created $CONFIG_DIR/memento.yml"
else
    info "Config already exists at $CONFIG_DIR/memento.yml"
    NEW_KEYS=()
    EXPECTED_KEYS="exchange_threshold file_count_threshold inception_enabled"
    if [ "$EXPERIMENTAL" = true ]; then
        EXPECTED_KEYS="$EXPECTED_KEYS session_briefing briefing_max_notes briefing_min_score prompt_recall recall_min_score recall_max_notes tool_context tool_context_min_score tool_context_max_notes multi_hop_enabled multi_hop_max"
    fi
    for key in $EXPECTED_KEYS; do
        if ! grep -q "^${key}:" "$CONFIG_DIR/memento.yml" 2>/dev/null; then
            NEW_KEYS+=("$key")
        fi
    done
    if [ ${#NEW_KEYS[@]} -gt 0 ]; then
        warn "New config keys available: ${NEW_KEYS[*]}"
        warn "See memento.yml.example for defaults. Your config is unchanged."
    fi
fi

# --- Install hooks ---

step "Installing Claude Code hooks..."

mkdir -p "$CLAUDE_DIR/hooks"

HOOKS_UPDATED=0
HOOKS_SKIPPED=0

STABLE_HOOKS="memento-triage.py vault-commit.sh memento-sweeper.py wait-and-commit.py _backfill_certainty.py memento-remote-sync.py"
EXPERIMENTAL_HOOKS="memento_utils.py vault-briefing.py vault-recall.py vault-tool-context.py memento-inception.py tenet_reranker.py"

if [ "$EXPERIMENTAL" = true ]; then
    INSTALL_HOOKS="$STABLE_HOOKS $EXPERIMENTAL_HOOKS"
    info "Experimental mode: installing Tenet + Inception"
else
    INSTALL_HOOKS="$STABLE_HOOKS"
fi

for hook in $INSTALL_HOOKS; do
    if safe_copy "$SCRIPT_DIR/hooks/$hook" "$CLAUDE_DIR/hooks/$hook" "hooks/$hook"; then
        ((HOOKS_UPDATED++)) || true
    else
        ((HOOKS_SKIPPED++)) || true
    fi
done
chmod +x "$CLAUDE_DIR/hooks/vault-commit.sh"

if [ "$HOOKS_SKIPPED" -gt 0 ]; then
    info "Hooks: $HOOKS_UPDATED updated, $HOOKS_SKIPPED skipped (locally modified)"
    info "  Run: diff ~/.claude/hooks/FILE ~/.claude/hooks/FILE.new to review changes"
else
    info "Hooks: $HOOKS_UPDATED installed to $CLAUDE_DIR/hooks/"
fi

# --- Install skills ---

step "Installing Claude Code skills..."

SKILLS_UPDATED=0
SKILLS_SKIPPED=0

INSTALL_SKILLS="memento memento-defrag start-fresh continue-work"
if [ "$EXPERIMENTAL" = true ]; then
    INSTALL_SKILLS="$INSTALL_SKILLS inception"
fi

for skill in $INSTALL_SKILLS; do
    mkdir -p "$CLAUDE_DIR/skills/$skill"
    if safe_copy "$SCRIPT_DIR/skills/$skill/SKILL.md" "$CLAUDE_DIR/skills/$skill/SKILL.md" "skills/$skill"; then
        ((SKILLS_UPDATED++)) || true
    else
        ((SKILLS_SKIPPED++)) || true
    fi
done

if [ "$SKILLS_SKIPPED" -gt 0 ]; then
    info "Skills: $SKILLS_UPDATED updated, $SKILLS_SKIPPED skipped (locally modified)"
else
    info "Skills: $SKILLS_UPDATED installed to $CLAUDE_DIR/skills/"
fi

# --- Install agents ---

step "Installing Claude Code agents..."

mkdir -p "$CLAUDE_DIR/agents"
if safe_copy "$SCRIPT_DIR/agents/concierge.md" "$CLAUDE_DIR/agents/concierge.md" "agents/concierge"; then
    info "Concierge agent installed to $CLAUDE_DIR/agents/"
fi

# --- Install memento package ---

step "Installing memento package..."

MEMENTO_PKG_DIR="$CLAUDE_DIR/hooks/memento"
mkdir -p "$MEMENTO_PKG_DIR/adapters"

PKG_COPIED=0
PKG_SKIPPED=0

for mod in __init__.py config.py utils.py search.py search_backend.py graph.py store.py llm.py types.py mcp_server.py __main__.py auth.py remote_client.py embedded_search.py embedding.py indexer.py; do
    if [ -f "$SCRIPT_DIR/memento/$mod" ]; then
        if safe_copy "$SCRIPT_DIR/memento/$mod" "$MEMENTO_PKG_DIR/$mod" "memento/$mod"; then
            ((PKG_COPIED++)) || true
        else
            ((PKG_SKIPPED++)) || true
        fi
    fi
done

for mod in __init__.py claude.py; do
    if [ -f "$SCRIPT_DIR/memento/adapters/$mod" ]; then
        if safe_copy "$SCRIPT_DIR/memento/adapters/$mod" "$MEMENTO_PKG_DIR/adapters/$mod" "memento/adapters/$mod"; then
            ((PKG_COPIED++)) || true
        else
            ((PKG_SKIPPED++)) || true
        fi
    fi
done

for critical in __init__.py config.py utils.py store.py search.py adapters/__init__.py adapters/claude.py; do
    if [ ! -f "$MEMENTO_PKG_DIR/$critical" ]; then
        error "Critical file missing: $MEMENTO_PKG_DIR/$critical"
        error "Hooks will not work. Rerun with --force or fix permissions."
        exit 1
    fi
done

if [ "$PKG_SKIPPED" -gt 0 ]; then
    info "Package: $PKG_COPIED updated, $PKG_SKIPPED skipped (locally modified)"
else
    info "Package: $PKG_COPIED files installed to $MEMENTO_PKG_DIR"
fi

# --- Embedded search backend (optional) ---

step "Checking embedded search backend..."
if python3 -c "import sqlite_vec; import onnxruntime" 2>/dev/null; then
    info "Embedded search backend: available (onnxruntime + sqlite-vec)"
else
    info "Embedded search backend: not installed (will use QMD or grep fallback)"
    info "To enable: pip install onnxruntime sqlite-vec numpy tokenizers"
fi

# --- MCP server config (--mcp flag) ---

if [ "$MCP_INSTALL" = true ]; then
    step "Setting up MCP server..."

    # Verify mcp Python package is available
    if ! python3 -c "import mcp" 2>/dev/null; then
        warn "MCP Python package not found. Installing..."
        if command -v uv &>/dev/null; then
            uv pip install "mcp[cli]>=1.0" 2>/dev/null && info "Installed mcp via uv" || true
        elif command -v pip3 &>/dev/null; then
            pip3 install "mcp[cli]>=1.0" 2>/dev/null && info "Installed mcp via pip3" || true
        elif command -v pip &>/dev/null; then
            pip install "mcp[cli]>=1.0" 2>/dev/null && info "Installed mcp via pip" || true
        fi

        if ! python3 -c "import mcp" 2>/dev/null; then
            error "Could not install mcp Python package. MCP server will not work."
            error "Install manually: pip install 'mcp[cli]>=1.0'"
            MCP_INSTALL=false
        fi
    else
        info "MCP Python package: available"
    fi

    # Write/merge mcp-servers.json and register with Claude Code CLI
    if [ "$MCP_INSTALL" = true ]; then
        python3 "$HELPER" mcp-config \
            "$REMOTE_MODE" "$CLAUDE_DIR" "${REMOTE_URL:-}" "${REMOTE_API_KEY:-}"
        info "MCP config written to $CLAUDE_DIR/mcp-servers.json (Cursor, Windsurf, etc.)"
        register_mcp_cli
    fi
fi

# --- Merge settings.json ---

step "Updating Claude Code settings..."

HOOK_ENV_PREFIX=""
if [ "$REMOTE_MODE" = true ]; then
    REMOTE_ENV_FILE="$CLAUDE_DIR/memento-remote.env"
    python3 "$HELPER" remote-env "$REMOTE_ENV_FILE" "$REMOTE_URL" "${REMOTE_API_KEY:-}"
    chmod 600 "$REMOTE_ENV_FILE"
    HOOK_ENV_PREFIX="bash -c 'set -a; . $REMOTE_ENV_FILE; set +a; exec \"\$@\"' -- "
fi

python3 "$HELPER" merge-settings \
    "$CLAUDE_DIR/settings.json" "$CLAUDE_DIR" "$VAULT_PATH" "$EXPERIMENTAL" "$HOOK_ENV_PREFIX"

# --- Save manifest ---

save_manifest
info "Manifest saved to $MANIFEST (v${NEW_VERSION})"

# --- QMD setup (optional) ---

setup_qmd

# --- Shell warmup (optional, experimental only) ---

setup_shell_warmup

# --- Done ---

print_summary
