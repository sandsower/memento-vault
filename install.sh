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

# Colors (if terminal supports them)
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
# The manifest tracks what we installed and the checksum at install time.
# On upgrade, we compare the installed file's current checksum against the
# manifest checksum. If they differ, the user modified the file and we skip it.

file_hash() {
    # Portable checksum: sha256 on Linux, shasum on macOS
    if command -v sha256sum &>/dev/null; then
        sha256sum "$1" 2>/dev/null | cut -d' ' -f1
    elif command -v shasum &>/dev/null; then
        shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
    else
        # Fallback: md5
        md5sum "$1" 2>/dev/null | cut -d' ' -f1
    fi
}

load_manifest() {
    # Read the installed version, vault path, and file checksums from the manifest
    INSTALLED_VERSION=""
    MANIFEST_VAULT_PATH=""
    if [ -f "$MANIFEST" ]; then
        INSTALLED_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST')).get('version',''))" 2>/dev/null || echo "")
        MANIFEST_VAULT_PATH=$(python3 -c "import json; print(json.load(open('$MANIFEST')).get('vault_path',''))" 2>/dev/null || echo "")
    fi
}

manifest_hash() {
    # Get the checksum recorded for a file at last install
    local key="$1"
    if [ -f "$MANIFEST" ]; then
        python3 -c "import json; m=json.load(open('$MANIFEST')); print(m.get('files',{}).get('$key',''))" 2>/dev/null || echo ""
    else
        echo ""
    fi
}

save_manifest() {
    # Write the manifest with current version and all file checksums
    mkdir -p "$CONFIG_DIR"
    python3 -c "
import json, sys
files = json.loads(sys.argv[1])
manifest = {'version': sys.argv[2], 'vault_path': sys.argv[3], 'files': files}
with open(sys.argv[4], 'w') as f:
    json.dump(manifest, f, indent=2)
" "$MANIFEST_FILES_JSON" "$NEW_VERSION" "$VAULT_PATH" "$MANIFEST"
}

# Accumulator for manifest file entries (built during install)
MANIFEST_FILES_JSON="{}"
record_file() {
    local key="$1" path="$2"
    local hash
    hash=$(file_hash "$path")
    MANIFEST_FILES_JSON=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
d[sys.argv[2]] = sys.argv[3]
print(json.dumps(d))
" "$MANIFEST_FILES_JSON" "$key" "$hash")
}

# Base copy storage — saves the installed version for future three-way merges
BASE_DIR="$CONFIG_DIR/base"

save_base() {
    local key="$1" src="$2"
    local base_path="$BASE_DIR/$key"
    mkdir -p "$(dirname "$base_path")"
    cp "$src" "$base_path"
}

# Safe copy: only overwrites if the user hasn't modified the installed copy.
# When a user has modified a file and a base copy exists, attempts a three-way
# merge using git merge-file. Falls back to saving a .new copy when merge
# isn't possible (no base or no git).
# Returns 0 if copied/merged, 1 if skipped.
safe_copy() {
    local src="$1" dest="$2" key="$3"

    # Clean up stale artifacts from previous runs
    rm -f "${dest}.new" "${dest}.merged"

    if [ "$FORCE" = true ]; then
        cp "$src" "$dest"
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    if [ ! -f "$dest" ]; then
        # New file — always install
        cp "$src" "$dest"
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    local manifest_checksum
    manifest_checksum=$(manifest_hash "$key")

    if [ -z "$manifest_checksum" ]; then
        # File exists but no manifest entry (pre-manifest install or manual file).
        # Don't overwrite — it's ambiguous. No base saved (we didn't put this file here).
        local src_hash dest_hash
        src_hash=$(file_hash "$src")
        dest_hash=$(file_hash "$dest")
        if [ "$src_hash" = "$dest_hash" ]; then
            # Identical — record it and seed the base for future merges
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
        # Already up to date — seed the base for future merges
        record_file "$key" "$dest"
        save_base "$key" "$src"
        return 0
    fi

    if [ "$current_hash" = "$manifest_checksum" ]; then
        # File unchanged since last install — safe to overwrite
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
            # Clean merge — apply it
            cp "$tmp_merged" "$dest"
            rm -f "$tmp_merged"
            record_file "$key" "$dest"
            save_base "$key" "$src"
            info "Auto-merged $key (your changes preserved)"
            return 0
        elif [ "$merge_rc" -gt 0 ]; then
            # Conflicts — save merged file with markers, keep user's file
            cp "$tmp_merged" "${dest}.merged"
            cp "$src" "${dest}.new"
            rm -f "$tmp_merged"
            skip "Skipped $key (merge conflicts)"
            skip "  Conflict file: ${dest}.merged"
            skip "  New version: ${dest}.new"
            record_file "$key" "$dest"
            return 1
        else
            # Negative exit = error, fall through to .new fallback
            rm -f "$tmp_merged"
        fi
    fi

    # Fallback: no base or git unavailable — save .new for manual diff
    cp "$src" "${dest}.new"
    skip "Skipped $key (locally modified)"
    skip "  New version saved to ${dest}.new — diff and merge your changes"
    record_file "$key" "$dest"
    return 1
}

# --- Load existing manifest ---

load_manifest

# On reinstall/upgrade, prefer the vault path from the previous install
# unless the user explicitly overrode it via MEMENTO_VAULT_PATH.
if [ -z "${MEMENTO_VAULT_PATH:-}" ] && [ -n "$MANIFEST_VAULT_PATH" ]; then
    VAULT_PATH="$MANIFEST_VAULT_PATH"
fi

# --- Remote mode setup ---
# When --remote is passed, we install hooks + package but skip local vault creation.
# Hooks will talk to the remote vault over HTTP instead of local filesystem.

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

    # In remote mode, MCP is always installed (pointing at remote URL)
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

# --- Create vault (always — local vault is the primary store even in remote mode) ---

step "Setting up vault at $VAULT_PATH..."
if [ "$REMOTE_MODE" = true ]; then
    info "Local vault is always maintained. Remote vault syncs at: $REMOTE_URL"
fi
{

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

    # --- Obsidian setup (optional, first install only) ---

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
    else
        obsidian="skip"
    fi
}

# --- Config file ---

step "Writing config..."

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/memento.yml" ]; then
    sed "s|~/memento|$VAULT_PATH|g" "$SCRIPT_DIR/memento.yml.example" > "$CONFIG_DIR/memento.yml"
    info "Created $CONFIG_DIR/memento.yml"
else
    info "Config already exists at $CONFIG_DIR/memento.yml"
    # Check for new config keys the user might want to add
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

STABLE_HOOKS="memento-triage.py vault-commit.sh memento-sweeper.py wait-and-commit.py _backfill_certainty.py"
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

# Copy the memento/ package to Claude's hooks dir so hooks can import it
MEMENTO_PKG_DIR="$CLAUDE_DIR/hooks/memento"
mkdir -p "$MEMENTO_PKG_DIR/adapters"

PKG_COPIED=0
PKG_SKIPPED=0

for mod in __init__.py config.py utils.py search.py search_backend.py graph.py store.py llm.py types.py mcp_server.py __main__.py auth.py remote_client.py; do
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

# Validate critical package files
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

    # Detect MCP config location
    MCP_CONFIG=""
    if [ -d "$HOME/.claude" ]; then
        MCP_CONFIG="$CLAUDE_DIR/mcp-servers.json"
    fi

    if [ -n "$MCP_CONFIG" ]; then
        # Create or merge MCP server entry
        if [ "$REMOTE_MODE" = true ]; then
            # Remote mode: point MCP at the remote vault URL
            MCP_HEADERS="{}"
            if [ -n "$REMOTE_API_KEY" ]; then
                MCP_HEADERS="{\"Authorization\": \"Bearer $REMOTE_API_KEY\"}"
            fi
            MCP_ENTRY=$(python3 -c "
import json, sys
entry = {'memento-vault': {'url': sys.argv[1] + '/mcp'}}
headers = json.loads(sys.argv[2])
if headers:
    entry['memento-vault']['headers'] = headers
print(json.dumps(entry, indent=2))
" "$REMOTE_URL" "$MCP_HEADERS")
        else
            MCP_ENTRY=$(cat << MCP_EOF
{
  "memento-vault": {
    "command": "python3",
    "args": ["-m", "memento"],
    "env": {
      "PYTHONPATH": "$CLAUDE_DIR/hooks"
    }
  }
}
MCP_EOF
)
        fi
        if [ -f "$MCP_CONFIG" ]; then
            # Merge into existing config
            if grep -q "memento-vault" "$MCP_CONFIG"; then
                info "MCP server already configured in $MCP_CONFIG"
            else
                python3 -c "
import json, sys, tempfile, os
config_path = sys.argv[1]
existing = json.load(open(config_path))
new_entry = json.loads(sys.argv[2])
existing.update(new_entry)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config_path), suffix='.json')
with os.fdopen(fd, 'w') as f:
    json.dump(existing, f, indent=2)
os.replace(tmp, config_path)
" "$MCP_CONFIG" "$MCP_ENTRY"
                info "Added memento-vault to $MCP_CONFIG"
            fi
        else
            echo "$MCP_ENTRY" | python3 -c "import json,sys; json.dump(json.load(sys.stdin), open(sys.argv[1],'w'), indent=2)" "$MCP_CONFIG"
            info "Created $MCP_CONFIG with memento-vault server"
        fi
    else
        warn "Could not detect MCP config location. Manual setup required."
        echo ""
        echo "Add this to your agent's MCP server config:"
        echo ""
        echo "  \"memento-vault\": {"
        echo "    \"command\": \"python3\","
        echo "    \"args\": [\"-m\", \"memento\"],"
        echo "    \"env\": {\"PYTHONPATH\": \"$CLAUDE_DIR/hooks\"}"
        echo "  }"
        echo ""
    fi
fi

# --- Merge settings.json ---

step "Updating Claude Code settings..."

SETTINGS="$CLAUDE_DIR/settings.json"

# Build environment prefix for hook commands
# In remote mode, hooks need MEMENTO_VAULT_URL (and optionally MEMENTO_API_KEY)
# to know they should talk to the remote vault instead of local filesystem.
HOOK_ENV_PREFIX=""
if [ "$REMOTE_MODE" = true ]; then
    HOOK_ENV_PREFIX="MEMENTO_VAULT_URL=$REMOTE_URL "
    if [ -n "$REMOTE_API_KEY" ]; then
        HOOK_ENV_PREFIX="${HOOK_ENV_PREFIX}MEMENTO_API_KEY=$REMOTE_API_KEY "
    fi
fi

if [ ! -f "$SETTINGS" ]; then
    if [ "$EXPERIMENTAL" = true ]; then
        cat > "$SETTINGS" << SETTINGS_EOF
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-briefing.py",
        "timeout": 8000
      }
    ],
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-recall.py",
        "timeout": 5000
      }
    ],
    "SessionEnd": [
      {
        "type": "command",
        "command": "${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/memento-triage.py",
        "timeout": 30000,
        "async": true
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-tool-context.py",
            "timeout": 2000
          }
        ]
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
        info "Created $SETTINGS with stable + experimental hooks"
    else
        cat > "$SETTINGS" << SETTINGS_EOF
{
  "hooks": {
    "SessionEnd": [
      {
        "type": "command",
        "command": "${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/memento-triage.py",
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
        info "Created $SETTINGS with stable hooks"
    fi
else
    info "settings.json already exists, checking for missing hooks..."
    MISSING_HOOKS=()
    grep -q "memento-triage" "$SETTINGS" || MISSING_HOOKS+=("SessionEnd/memento-triage")

    if [ "$EXPERIMENTAL" = true ]; then
        grep -q "vault-briefing" "$SETTINGS" || MISSING_HOOKS+=("SessionStart/vault-briefing")
        grep -q "vault-recall" "$SETTINGS" || MISSING_HOOKS+=("UserPromptSubmit/vault-recall")
        grep -q "vault-tool-context" "$SETTINGS" || MISSING_HOOKS+=("PreToolUse/vault-tool-context")
    fi

    if [ ${#MISSING_HOOKS[@]} -gt 0 ]; then
        warn "Missing hooks in settings.json: ${MISSING_HOOKS[*]}"
        echo ""
        echo -e "${DIM}Add to your settings.json under \"hooks\":${NC}"
        echo ""

        if [[ " ${MISSING_HOOKS[*]} " == *"memento-triage"* ]]; then
            echo '  "SessionEnd": ['
            echo "    {\"type\": \"command\", \"command\": \"${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/memento-triage.py\", \"timeout\": 30000, \"async\": true}"
            echo '  ],'
        fi
        if [[ " ${MISSING_HOOKS[*]} " == *"vault-briefing"* ]]; then
            echo '  "SessionStart": ['
            echo "    {\"type\": \"command\", \"command\": \"${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-briefing.py\", \"timeout\": 8000}"
            echo '  ],'
        fi
        if [[ " ${MISSING_HOOKS[*]} " == *"vault-recall"* ]]; then
            echo '  "UserPromptSubmit": ['
            echo "    {\"type\": \"command\", \"command\": \"${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-recall.py\", \"timeout\": 5000}"
            echo '  ],'
        fi
        if [[ " ${MISSING_HOOKS[*]} " == *"vault-tool-context"* ]]; then
            echo '  "PreToolUse": ['
            echo "    {\"matcher\": \"Read\", \"hooks\": [{\"type\": \"command\", \"command\": \"${HOOK_ENV_PREFIX}python3 $CLAUDE_DIR/hooks/vault-tool-context.py\", \"timeout\": 2000}]}"
            echo '  ]'
        fi
        echo ""
    else
        info "All hooks already configured"
    fi

    # In remote mode, update existing hook commands to include the remote env prefix.
    # Use python3 for JSON manipulation instead of sed to avoid issues with
    # special characters (&, /, \) in URLs and API keys breaking sed patterns.
    if [ "$REMOTE_MODE" = true ] && [ -n "$HOOK_ENV_PREFIX" ]; then
        info "Updating hook commands for remote mode..."
        python3 -c "
import json, sys, re

settings_path = sys.argv[1]
prefix = sys.argv[2]
hooks_dir = sys.argv[3] + '/hooks/'

with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.get('hooks', {})
changed = False
for event, entries in hooks.items():
    if not isinstance(entries, list):
        continue
    for entry in entries:
        # Handle both flat entries and nested {matcher, hooks} entries
        hook_list = entry.get('hooks', [entry]) if isinstance(entry, dict) else []
        for hook in hook_list:
            cmd = hook.get('command', '')
            if hooks_dir not in cmd:
                continue
            # Strip any existing env prefix, then prepend the new one
            cleaned = re.sub(r'MEMENTO_VAULT_URL=\S+\s+(MEMENTO_API_KEY=\S+\s+)?', '', cmd)
            hook['command'] = prefix + cleaned
            changed = True

if changed:
    with open(settings_path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print('Hook commands updated with remote vault URL')
else:
    print('No memento hooks found to update')
" "$SETTINGS" "$HOOK_ENV_PREFIX" "$CLAUDE_DIR"
    fi
fi

# --- Save manifest ---

save_manifest
info "Manifest saved to $MANIFEST (v${NEW_VERSION})"

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

    # Initial index (first install only)
    if [ -z "$INSTALLED_VERSION" ]; then
        echo ""
        read -rp "Run initial QMD indexing now? [Y/n] " index_now
        if [[ ! "$index_now" =~ ^[Nn] ]]; then
            qmd update -c memento && qmd embed
            info "QMD index built"
        fi
    fi
fi

# --- Shell warmup (optional, experimental only) ---

if [ "$QMD_AVAILABLE" = true ] && [ "$EXPERIMENTAL" = true ] && [ "$REMOTE_MODE" != true ]; then
    # Detect shell rc file
    SHELL_RC=""
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
    esac

    WARMUP_MARKER="qmd vsearch.*warmup"
    if [ -n "$SHELL_RC" ] && [ -f "$SHELL_RC" ]; then
        if grep -qE "$WARMUP_MARKER" "$SHELL_RC" 2>/dev/null; then
            info "QMD model warmup already in $SHELL_RC"
        else
            echo ""
            read -rp "Add QMD model warmup to $SHELL_RC? (faster session briefings) [Y/n] " warmup
            if [[ ! "$warmup" =~ ^[Nn] ]]; then
                cat >> "$SHELL_RC" << 'WARMUP_EOF'

# Warm QMD embedding model on shell startup (background, silent)
command -v qmd &>/dev/null && qmd vsearch "warmup" -c memento -n 1 &>/dev/null &
WARMUP_EOF
                info "Added QMD warmup to $SHELL_RC"
            fi
        fi
    fi
fi

# --- Done ---

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
    INCEPTION_DEPS_OK=true
    for pkg in numpy hdbscan sklearn; do
        if ! python3 -c "import $pkg" 2>/dev/null; then
            INCEPTION_DEPS_OK=false
            break
        fi
    done
    if [ "$INCEPTION_DEPS_OK" = false ]; then
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
if [[ ! "$obsidian" =~ ^[Nn] ]]; then
    echo "Browse: open $VAULT_PATH in Obsidian"
fi

if [ "$REMOTE_MODE" = true ]; then
    echo ""
    echo "To use from other tools, set these environment variables:"
    echo "  export MEMENTO_VAULT_URL=$REMOTE_URL"
    if [ -n "$REMOTE_API_KEY" ]; then
        echo "  export MEMENTO_API_KEY=$REMOTE_API_KEY"
    fi
else
    echo ""
    echo "To deploy this vault as a remote service (Docker):"
    echo "  See: ./setup-remote.sh"
fi
echo ""
