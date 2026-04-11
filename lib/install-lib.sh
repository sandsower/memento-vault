#!/usr/bin/env bash
# Sourced library for memento-vault install.sh
# Provides: logging, manifest helpers, safe_copy, and extracted setup functions.
#
# Expected globals (set by the caller before sourcing):
#   SCRIPT_DIR  CLAUDE_DIR  VAULT_PATH  CONFIG_DIR  MANIFEST
#   NEW_VERSION  FORCE  EXPERIMENTAL  MCP_INSTALL
#   REMOTE_MODE  REMOTE_URL  REMOTE_API_KEY
#   MANIFEST_FILES_JSON  (accumulator, init to "{}")
#   INSTALLED_VERSION  MANIFEST_VAULT_PATH  (set by load_manifest)
#   QMD_AVAILABLE  (set by preflight checks)

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$LIB_DIR/install_helpers.py"

# --- Colors & logging ---

if [ -t 1 ]; then
    BOLD='\033[1m'
    DIM='\033[2m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    BOLD='' DIM='' GREEN='' YELLOW='' RED='' CYAN='' NC=''
fi

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; }
step()  { echo -e "\n${BOLD}$1${NC}"; }
skip()  { echo -e "${CYAN}[~]${NC} $1"; }

# --- Manifest helpers ---

file_hash() {
    if command -v sha256sum &>/dev/null; then
        sha256sum "$1" 2>/dev/null | cut -d' ' -f1
    elif command -v shasum &>/dev/null; then
        shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
    else
        md5sum "$1" 2>/dev/null | cut -d' ' -f1
    fi
}

load_manifest() {
    INSTALLED_VERSION=""
    MANIFEST_VAULT_PATH=""
    if [ -f "$MANIFEST" ]; then
        local output
        output=$(python3 "$HELPER" manifest-load "$MANIFEST" 2>/dev/null) || return
        INSTALLED_VERSION=$(echo "$output" | sed -n '1p')
        MANIFEST_VAULT_PATH=$(echo "$output" | sed -n '2p')
    fi
}

manifest_hash() {
    local key="$1"
    if [ -f "$MANIFEST" ]; then
        python3 "$HELPER" manifest-hash "$MANIFEST" "$key" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

record_file() {
    local key="$1" path="$2"
    local hash
    hash=$(file_hash "$path")
    MANIFEST_FILES_JSON=$(python3 "$HELPER" manifest-record "$MANIFEST_FILES_JSON" "$key" "$hash")
}

save_manifest() {
    mkdir -p "$CONFIG_DIR"
    python3 "$HELPER" manifest-save "$MANIFEST_FILES_JSON" "$NEW_VERSION" "$VAULT_PATH" "$MANIFEST"
}

# --- Base copy storage ---

BASE_DIR="$CONFIG_DIR/base"

save_base() {
    local key="$1" src="$2"
    local base_path="$BASE_DIR/$key"
    mkdir -p "$(dirname "$base_path")"
    cp "$src" "$base_path"
}

# --- safe_copy ---
# Only overwrites if the user hasn't modified the installed copy.
# When a user has modified a file and a base copy exists, attempts a three-way
# merge using git merge-file. Falls back to saving a .new copy.
# Returns 0 if copied/merged, 1 if skipped.

safe_copy() {
    local src="$1" dest="$2" key="$3"

    rm -f "${dest}.new" "${dest}.merged"

    if [ "$FORCE" = true ]; then
        cp "$src" "$dest"
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    if [ ! -f "$dest" ]; then
        cp "$src" "$dest"
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    local manifest_checksum
    manifest_checksum=$(manifest_hash "$key")

    if [ -z "$manifest_checksum" ]; then
        local src_hash dest_hash
        src_hash=$(file_hash "$src")
        dest_hash=$(file_hash "$dest")
        if [ "$src_hash" = "$dest_hash" ]; then
            record_file "$key" "$dest"
            save_base "$key" "$src"
            return 0
        else
            cp "$src" "${dest}.new"
            skip "Skipped $key (exists, may have local changes)"
            skip "  New version saved to ${dest}.new — diff and merge your changes"
            record_file "$key" "$dest"
            return 1
        fi
    fi

    local current_hash src_hash
    current_hash=$(file_hash "$dest")
    src_hash=$(file_hash "$src")

    if [ "$current_hash" = "$src_hash" ]; then
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    if [ "$current_hash" = "$manifest_checksum" ]; then
        cp "$src" "$dest"
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    # User modified the file — try three-way merge if we have a base copy
    local base_path="$BASE_DIR/$key"
    if [ -f "$base_path" ] && command -v git &>/dev/null; then
        local tmp_merged merge_rc
        tmp_merged=$(mktemp)
        cp "$dest" "$tmp_merged"

        merge_rc=0
        git merge-file "$tmp_merged" "$base_path" "$src" >/dev/null 2>&1 || merge_rc=$?

        if [ "$merge_rc" -eq 0 ]; then
            cp "$tmp_merged" "$dest"
            rm -f "$tmp_merged"
            record_file "$key" "$dest"
            save_base "$key" "$src"
            info "Auto-merged $key (your changes preserved)"
            return 0
        elif [ "$merge_rc" -gt 0 ]; then
            cp "$tmp_merged" "${dest}.merged"
            cp "$src" "${dest}.new"
            rm -f "$tmp_merged"
            skip "Skipped $key (merge conflicts)"
            skip "  Conflict file: ${dest}.merged"
            skip "  New version: ${dest}.new"
            record_file "$key" "$dest"
            return 1
        else
            rm -f "$tmp_merged"
        fi
    fi

    cp "$src" "${dest}.new"
    skip "Skipped $key (locally modified)"
    skip "  New version saved to ${dest}.new — diff and merge your changes"
    record_file "$key" "$dest"
    return 1
}

# --- setup_vault ---
# Creates the vault directory, git repo, and optionally Obsidian views.
# Sets global OBSIDIAN_INSTALLED for use by print_summary.

OBSIDIAN_INSTALLED=""

setup_vault() {
    step "Setting up vault at $VAULT_PATH..."
    if [ "$REMOTE_MODE" = true ]; then
        info "Local vault is always maintained. Remote vault syncs at: $REMOTE_URL"
    fi

    if [ -d "$VAULT_PATH" ]; then
        info "Vault directory already exists, preserving contents."
    else
        mkdir -p "$VAULT_PATH"
        info "Created $VAULT_PATH"
    fi

    for dir in fleeting notes projects archive; do
        mkdir -p "$VAULT_PATH/$dir"
    done
    info "Directory structure: fleeting/ notes/ projects/ archive/"

    if [ ! -f "$VAULT_PATH/.gitignore" ]; then
        cp "$SCRIPT_DIR/templates/vault/.gitignore" "$VAULT_PATH/.gitignore"
        info "Added .gitignore"
    fi

    if [ ! -d "$VAULT_PATH/.git" ]; then
        git -C "$VAULT_PATH" init
        git -C "$VAULT_PATH" add -A
        git -C "$VAULT_PATH" commit -m "init: bootstrap memento vault" --allow-empty
        info "Initialized git repository"
    else
        info "Git repo already initialized"
    fi

    # Obsidian setup (optional, first install only)
    if [ -z "$INSTALLED_VERSION" ]; then
        echo ""
        read -rp "Set up Obsidian views? (Base views for browsing notes) [Y/n] " obsidian
        if [[ ! "$obsidian" =~ ^[Nn] ]]; then
            if [ ! -d "$VAULT_PATH/.obsidian" ]; then
                cp -r "$SCRIPT_DIR/templates/obsidian/.obsidian" "$VAULT_PATH/.obsidian"
                info "Added Obsidian config (.obsidian/)"
            else
                info "Obsidian config already exists, skipping"
            fi

            for base in "$SCRIPT_DIR"/templates/obsidian/*.base; do
                basename=$(basename "$base")
                if [ ! -f "$VAULT_PATH/$basename" ]; then
                    cp "$base" "$VAULT_PATH/$basename"
                fi
            done
            info "Added Base views (by-type, by-project, recent, decisions, bugfixes, by-source, by-tag)"
        fi
        OBSIDIAN_INSTALLED="$obsidian"
    else
        OBSIDIAN_INSTALLED="skip"
    fi
}

# --- register_mcp_cli ---
# Registers the MCP server with `claude mcp add` (or prints manual instructions).

register_mcp_cli() {
    if command -v claude &>/dev/null; then
        claude mcp remove memento-vault -s user 2>/dev/null || true

        if [ "$REMOTE_MODE" = true ]; then
            local mcp_url
            mcp_url=$(python3 "$HELPER" mcp-url "$REMOTE_URL")
            if [ -n "$REMOTE_API_KEY" ]; then
                claude mcp add -s user --transport http memento-vault "$mcp_url" \
                    --header "Authorization: Bearer $REMOTE_API_KEY"
            else
                claude mcp add -s user --transport http memento-vault "$mcp_url"
            fi
        else
            claude mcp add -s user -e PYTHONPATH="$CLAUDE_DIR/hooks" \
                memento-vault -- python3 -m memento
        fi
        info "MCP server registered with Claude Code (scope: user)"
    else
        warn "Claude Code CLI not found. To register manually, run:"
        echo ""
        if [ "$REMOTE_MODE" = true ]; then
            local mcp_url
            mcp_url=$(python3 "$HELPER" mcp-url "$REMOTE_URL")
            echo "  claude mcp add -s user --transport http memento-vault $mcp_url \\"
            if [ -n "$REMOTE_API_KEY" ]; then
                echo "    --header \"Authorization: Bearer $REMOTE_API_KEY\""
            fi
        else
            echo "  claude mcp add -s user -e PYTHONPATH=\"$CLAUDE_DIR/hooks\" \\"
            echo "    memento-vault -- python3 -m memento"
        fi
        echo ""
    fi
}

# --- setup_qmd ---

setup_qmd() {
    if [ "$QMD_AVAILABLE" != true ]; then
        return
    fi

    step "Setting up QMD collection..."

    local qmd_config="$HOME/.config/qmd/index.yml"
    if [ -f "$qmd_config" ]; then
        if grep -q "memento:" "$qmd_config"; then
            info "QMD memento collection already configured"
        else
            warn "QMD config exists but has no memento collection."
            echo -e "${DIM}Add this to $qmd_config under collections:${NC}"
            echo ""
            echo "  memento:"
            echo "    path: $VAULT_PATH"
            echo '    pattern: "**/*.md"'
            echo "    context:"
            echo '      "": Personal knowledge vault with session notes, decisions, discoveries, and project history.'
        fi
    else
        mkdir -p "$(dirname "$qmd_config")"
        sed "s|~/memento|$VAULT_PATH|g" "$SCRIPT_DIR/templates/qmd-collection.yml" > "$qmd_config"
        info "Created QMD config at $qmd_config"
    fi

    # Initial index (first install only)
    if [ -z "$INSTALLED_VERSION" ]; then
        echo ""
        read -rp "Run initial QMD indexing now? [Y/n] " index_now
        if [[ ! "$index_now" =~ ^[Nn] ]]; then
            qmd update -c memento && qmd embed
            info "QMD index built"
        fi
    fi
}

# --- setup_shell_warmup ---

setup_shell_warmup() {
    if [ "$QMD_AVAILABLE" != true ] || [ "$EXPERIMENTAL" != true ] || [ "$REMOTE_MODE" = true ]; then
        return
    fi

    local shell_rc=""
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)  shell_rc="$HOME/.zshrc" ;;
        bash) shell_rc="$HOME/.bashrc" ;;
        fish) shell_rc="$HOME/.config/fish/config.fish" ;;
    esac

    local warmup_marker="qmd vsearch.*warmup"
    if [ -n "$shell_rc" ] && [ -f "$shell_rc" ]; then
        if grep -qE "$warmup_marker" "$shell_rc" 2>/dev/null; then
            info "QMD model warmup already in $shell_rc"
        else
            echo ""
            read -rp "Add QMD model warmup to $shell_rc? (faster session briefings) [Y/n] " warmup
            if [[ ! "$warmup" =~ ^[Nn] ]]; then
                cat >> "$shell_rc" << 'WARMUP_EOF'

# Warm QMD embedding model on shell startup (background, silent)
command -v qmd &>/dev/null && qmd vsearch "warmup" -c memento -n 1 &>/dev/null &
WARMUP_EOF
                info "Added QMD warmup to $shell_rc"
            fi
        fi
    fi
}

# --- print_summary ---

print_summary() {
    if [ "$REMOTE_MODE" = true ] && [ "$EXPERIMENTAL" = true ]; then
        step "Installation complete! (v${NEW_VERSION} — local + remote)"
    elif [ "$REMOTE_MODE" = true ]; then
        step "Installation complete! (v${NEW_VERSION} — local + remote)"
    elif [ "$EXPERIMENTAL" = true ] && [ "$MCP_INSTALL" = true ]; then
        step "Installation complete! (v${NEW_VERSION} + Tenet + Inception + MCP)"
    elif [ "$EXPERIMENTAL" = true ]; then
        step "Installation complete! (v${NEW_VERSION} + Tenet + Inception)"
    elif [ "$MCP_INSTALL" = true ]; then
        step "Installation complete! (v${NEW_VERSION} + MCP)"
    else
        step "Installation complete! (v${NEW_VERSION})"
    fi

    echo ""
    echo "Your vault is at: $VAULT_PATH"
    if [ "$REMOTE_MODE" = true ]; then
        echo "Remote sync:    $REMOTE_URL"
    fi
    echo ""
    echo "What happens now:"
    echo "  - Every session end, the triage hook captures knowledge locally"
    echo "  - Trivial sessions get a one-liner in fleeting/"
    echo "  - Substantial sessions spawn a background agent that writes atomic notes"
    if [ "$REMOTE_MODE" = true ]; then
        echo "  - Sessions are also sent to the remote vault for cross-device access"
    fi
    if [ "$EXPERIMENTAL" = true ]; then
        echo "  - Sessions start with a vault briefing (relevant notes for your project)"
        echo "  - Each prompt triggers JIT recall (related vault notes injected automatically)"
        echo "  - File reads inject vault notes about known code areas (tool-aware context)"
    fi
    echo "  - Use /memento to manually capture insights during a session"
    echo "  - Use /inception to find cross-session patterns (Inception)"
    echo "  - Use /memento-defrag monthly to archive stale notes"
    echo "  - Use /continue-work to pick up where you left off"
    echo "  - Use /start-fresh to checkpoint and clear context"
    echo ""

    # Check Inception dependencies if enabled
    if grep -q "^inception_enabled: true" "$CONFIG_DIR/memento.yml" 2>/dev/null; then
        local inception_deps_ok=true
        for pkg in numpy hdbscan sklearn; do
            if ! python3 -c "import $pkg" 2>/dev/null; then
                inception_deps_ok=false
                break
            fi
        done
        if [ "$inception_deps_ok" = false ]; then
            warn "Inception is enabled but dependencies are missing."
            echo "  pip install numpy hdbscan scikit-learn"
            echo ""
        fi
    fi

    if [ "$QMD_AVAILABLE" = true ]; then
        echo "Search: qmd search \"your query\" -c memento"
    else
        echo "Search: grep -r \"your query\" $VAULT_PATH/notes/"
        echo "  (install qmd for semantic search: https://github.com/tobi/qmd)"
    fi
    if [[ ! "$OBSIDIAN_INSTALLED" =~ ^[Nn] ]]; then
        echo "Browse: open $VAULT_PATH in Obsidian"
    fi

    if [ "$REMOTE_MODE" = true ]; then
        echo ""
        echo "To use from other tools, set these environment variables:"
        echo "  export MEMENTO_VAULT_URL=$REMOTE_URL"
        if [ -n "$REMOTE_API_KEY" ]; then
            echo "  export MEMENTO_API_KEY=<stored in $CLAUDE_DIR/memento-remote.env>"
        fi
    else
        echo ""
        echo "To deploy this vault as a remote service (Docker):"
        echo "  See: ./setup-remote.sh"
    fi
    echo ""
}
