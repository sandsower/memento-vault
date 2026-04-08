#!/usr/bin/env bash
# Memento Vault — Local-to-Docker migration helper
#
# This script helps migrate an existing local vault to a Docker-hosted
# remote vault. It handles:
#   1. Generating a vault identity (if missing)
#   2. Generating an API key
#   3. Building and starting the Docker container
#   4. Copying vault data into the container
#   5. Reconfiguring local hooks to use the remote vault
#
# Usage:
#   ./setup-remote.sh                 # Interactive migration
#   ./setup-remote.sh --port 9000     # Custom port
#   ./setup-remote.sh --no-migrate    # Skip vault data copy (fresh start)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/memento-vault"
PORT="${MEMENTO_PORT:-8745}"
SKIP_MIGRATE=false

for arg in "$@"; do
    case "$arg" in
        --no-migrate) SKIP_MIGRATE=true ;;
        --port)
            shift
            PORT="${1:-8745}"
            ;;
    esac
    shift 2>/dev/null || true
done

# Colors
if [ -t 1 ]; then
    BOLD='\033[1m' GREEN='\033[0;32m' YELLOW='\033[0;33m' RED='\033[0;31m' CYAN='\033[0;36m' NC='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' RED='' CYAN='' NC=''
fi

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; }
step()  { echo -e "\n${BOLD}$1${NC}"; }

# --- Preflight ---

step "Memento Vault — Remote Deployment Setup"
echo ""

if ! command -v docker &>/dev/null; then
    error "Docker is required but not installed."
    echo "Install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi
info "Docker: $(docker --version | head -1)"

if ! command -v docker compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    error "Docker Compose is required but not installed."
    exit 1
fi
info "Docker Compose: available"

# --- Detect existing vault ---

step "Step 1: Detect existing vault"

VAULT_PATH=""
if [ -f "$CONFIG_DIR/memento.yml" ]; then
    VAULT_PATH=$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from memento.config import load_config
print(load_config()['vault_path'])
" 2>/dev/null || echo "")
fi

if [ -z "$VAULT_PATH" ]; then
    VAULT_PATH="$HOME/memento"
fi

if [ -d "$VAULT_PATH/notes" ]; then
    NOTE_COUNT=$(find "$VAULT_PATH/notes" -name "*.md" 2>/dev/null | wc -l)
    info "Found existing vault at $VAULT_PATH ($NOTE_COUNT notes)"
else
    warn "No existing vault found at $VAULT_PATH"
    NOTE_COUNT=0
fi

# --- Generate API key ---

step "Step 2: Configure authentication"

API_KEY="${MEMENTO_API_KEY:-}"
if [ -z "$API_KEY" ]; then
    # Generate a random API key
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    info "Generated API key: $API_KEY"
    echo ""
    warn "Save this key! You'll need it to connect clients."
    echo "  export MEMENTO_API_KEY=$API_KEY"
else
    info "Using existing MEMENTO_API_KEY from environment"
fi

# --- Generate vault identity ---

step "Step 3: Vault identity"

python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from memento.config import get_vault_id
vid = get_vault_id()
print(f'Vault ID: {vid}')
" 2>/dev/null && info "Vault identity ready" || warn "Could not generate vault identity (non-fatal)"

# --- Build Docker image ---

step "Step 4: Build Docker image"

echo "Building memento-vault Docker image..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" build
info "Docker image built"

# --- Start container ---

step "Step 5: Start container"

export MEMENTO_API_KEY="$API_KEY"
export MEMENTO_PORT="$PORT"

docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d
info "Container started on port $PORT"

# Wait for health
echo -n "Waiting for vault to be ready..."
for i in $(seq 1 15); do
    if python3 -c "from urllib.request import urlopen; urlopen('http://localhost:$PORT/mcp')" 2>/dev/null; then
        echo ""
        info "Vault is healthy!"
        break
    fi
    echo -n "."
    sleep 2
done
echo ""

# --- Copy vault data ---

if [ "$SKIP_MIGRATE" != true ] && [ "$NOTE_COUNT" -gt 0 ]; then
    step "Step 6: Migrate vault data"

    echo ""
    read -rp "Copy your existing vault ($NOTE_COUNT notes) to Docker? [Y/n] " migrate
    if [[ ! "$migrate" =~ ^[Nn] ]]; then
        CONTAINER_ID=$(docker compose -f "$SCRIPT_DIR/docker-compose.yml" ps -q vault)
        if [ -n "$CONTAINER_ID" ]; then
            for dir in notes fleeting projects archive; do
                if [ -d "$VAULT_PATH/$dir" ]; then
                    docker cp "$VAULT_PATH/$dir/." "$CONTAINER_ID:/vault/$dir/"
                fi
            done
            # Copy config if exists
            if [ -f "$VAULT_PATH/memento.yml" ]; then
                docker cp "$VAULT_PATH/memento.yml" "$CONTAINER_ID:/vault/memento.yml"
            fi
            info "Vault data migrated to Docker container"
        else
            warn "Could not find running container. Copy vault data manually."
        fi
    else
        info "Skipping vault data migration (starting fresh)"
    fi
else
    if [ "$SKIP_MIGRATE" = true ]; then
        info "Skipping vault data migration (--no-migrate)"
    fi
fi

# --- Reconfigure local hooks ---

step "Step 7: Reconfigure local hooks for remote mode"

echo ""
echo "To connect this machine's hooks to the remote vault, run:"
echo ""
echo -e "  ${CYAN}cd $SCRIPT_DIR${NC}"
echo -e "  ${CYAN}./install.sh --remote http://localhost:$PORT --experimental${NC}"
echo ""
echo "To connect a DIFFERENT machine, run on that machine:"
echo ""
echo -e "  ${CYAN}git clone https://github.com/sandsower/memento-vault.git${NC}"
echo -e "  ${CYAN}cd memento-vault${NC}"
echo -e "  ${CYAN}MEMENTO_API_KEY=$API_KEY ./install.sh --remote http://YOUR_SERVER_IP:$PORT --experimental${NC}"
echo ""

read -rp "Reconfigure this machine now? [Y/n] " reconfig
if [[ ! "$reconfig" =~ ^[Nn] ]]; then
    MEMENTO_API_KEY="$API_KEY" "$SCRIPT_DIR/install.sh" --remote "http://localhost:$PORT" --experimental --force
fi

# --- Done ---

step "Remote vault deployment complete!"
echo ""
echo "Summary:"
echo "  Vault URL:  http://localhost:$PORT"
echo "  MCP URL:    http://localhost:$PORT/mcp"
echo "  API Key:    $API_KEY"
echo ""
echo "Docker management:"
echo "  Stop:       docker compose -f $SCRIPT_DIR/docker-compose.yml down"
echo "  Logs:       docker compose -f $SCRIPT_DIR/docker-compose.yml logs -f"
echo "  Restart:    docker compose -f $SCRIPT_DIR/docker-compose.yml restart"
echo ""
echo "Your local vault at $VAULT_PATH is unchanged."
echo "You can keep it as a backup or remove it once you've verified the migration."
echo ""
