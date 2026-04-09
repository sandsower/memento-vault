#!/usr/bin/env bash
# Memento Vault — Remote deployment helper
#
# Deploys the vault as a Docker service, either locally or on a remote server.
# Handles vault data migration, API key generation, and TLS setup.
#
# Usage:
#   ./setup-remote.sh                          # Deploy locally (localhost)
#   ./setup-remote.sh --host vault.example.com # Deploy for remote access
#   ./setup-remote.sh --host vault.example.com --tls  # With automatic TLS via Caddy
#   ./setup-remote.sh --port 9000              # Custom port
#   ./setup-remote.sh --no-migrate             # Skip vault data copy (fresh start)
#
# Other deployment options (not managed by this script):
#   Fly.io:           See fly.toml (fly launch && fly deploy)
#   Cloudflare Tunnel: See docker-compose.cloudflare.yml
#   Oracle Cloud Free: See deploy/cloud-init-oracle.yml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/memento-vault"
PORT="${MEMENTO_PORT:-8745}"
HOST=""
ENABLE_TLS=false
SKIP_MIGRATE=false

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-migrate) SKIP_MIGRATE=true; shift ;;
        --tls) ENABLE_TLS=true; shift ;;
        --host)
            HOST="${2:-}"
            shift 2
            ;;
        --port)
            PORT="${2:-8745}"
            shift 2
            ;;
        *) shift ;;
    esac
done

# Colors
if [ -t 1 ]; then
    BOLD='\033[1m' DIM='\033[2m' GREEN='\033[0;32m' YELLOW='\033[0;33m' RED='\033[0;31m' CYAN='\033[0;36m' NC='\033[0m'
else
    BOLD='' DIM='' GREEN='' YELLOW='' RED='' CYAN='' NC=''
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

if ! docker compose version &>/dev/null 2>&1; then
    error "Docker Compose is required but not installed."
    exit 1
fi
info "Docker Compose: available"

# --- Determine deployment mode ---

step "Step 1: Deployment target"

if [ -z "$HOST" ]; then
    echo ""
    echo "Where will this vault be accessible from?"
    echo "  1) This machine only (localhost)"
    echo "  2) Over the network (VPS, home server, etc.)"
    echo ""
    read -rp "Choice [1/2]: " deploy_choice
    if [ "$deploy_choice" = "2" ]; then
        read -rp "Hostname or IP (e.g., vault.example.com or 203.0.113.10): " HOST
        if [ -z "$HOST" ]; then
            error "Hostname is required for network deployment."
            exit 1
        fi
    fi
fi

if [ -n "$HOST" ]; then
    info "Deploying for remote access at: $HOST"
    IS_REMOTE=true
else
    info "Deploying for local access (localhost)"
    HOST="localhost"
    IS_REMOTE=false
fi

# --- TLS decision ---

if [ "$IS_REMOTE" = true ] && [ "$ENABLE_TLS" != true ]; then
    echo ""
    warn "Deploying without TLS means API keys are sent in cleartext."
    echo ""
    echo "Options:"
    echo "  1) Enable automatic TLS via Caddy (requires a domain name, not just an IP)"
    echo "  2) I'll handle TLS myself (reverse proxy, Cloudflare tunnel, Tailscale, etc.)"
    echo "  3) No TLS (testing/private network only)"
    echo ""
    read -rp "Choice [1/2/3]: " tls_choice
    case "$tls_choice" in
        1) ENABLE_TLS=true ;;
        2) info "You'll need to configure TLS termination pointing at localhost:$PORT" ;;
        3) warn "Proceeding without TLS. Do NOT use this over the public internet." ;;
    esac
fi

# --- Detect existing vault ---

step "Step 2: Detect existing vault"

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

step "Step 3: Configure authentication"

API_KEY="${MEMENTO_API_KEY:-}"
if [ -z "$API_KEY" ]; then
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    info "Generated API key: $API_KEY"
    echo ""
    warn "Save this key! You'll need it to connect clients."
else
    info "Using existing MEMENTO_API_KEY from environment"
fi

# --- Generate vault identity ---

step "Step 4: Vault identity"

python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from memento.config import get_vault_id
vid = get_vault_id()
print(f'  Vault ID: {vid}')
" 2>/dev/null && info "Vault identity ready" || warn "Could not generate vault identity (non-fatal)"

# --- Generate Compose file with optional TLS ---

step "Step 5: Configure Docker services"

COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

if [ "$ENABLE_TLS" = true ]; then
    # Generate a TLS-enabled compose with Caddy as reverse proxy
    COMPOSE_FILE="$SCRIPT_DIR/docker-compose.prod.yml"
    cat > "$COMPOSE_FILE" << COMPOSE_EOF
services:
  vault:
    build: .
    expose:
      - "8745"
    volumes:
      - vault-data:/vault
      - vault-config:/home/memento/.config/memento-vault
    environment:
      - MEMENTO_VAULT_PATH=/vault
      - MEMENTO_TRANSPORT=streamable-http
      - MEMENTO_HOST=0.0.0.0
      - MEMENTO_PORT=8745
      - MEMENTO_API_KEY=\${MEMENTO_API_KEY:-}
    restart: unless-stopped

  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - caddy-data:/data
      - caddy-config:/config
    environment:
      - VAULT_DOMAIN=${HOST}
    command: caddy reverse-proxy --from \${VAULT_DOMAIN} --to vault:8745
    depends_on:
      - vault
    restart: unless-stopped

volumes:
  vault-data:
  vault-config:
  caddy-data:
  caddy-config:
COMPOSE_EOF
    info "Generated $COMPOSE_FILE with Caddy TLS reverse proxy"
    info "Caddy will auto-provision Let's Encrypt certificates for $HOST"
fi

# --- Build and start ---

step "Step 6: Build and start"

export MEMENTO_API_KEY="$API_KEY"
export MEMENTO_PORT="$PORT"

docker compose -f "$COMPOSE_FILE" build
info "Docker image built"

docker compose -f "$COMPOSE_FILE" up -d
info "Container started"

# Determine the check URL
if [ "$ENABLE_TLS" = true ]; then
    CHECK_URL="https://$HOST/mcp"
    VAULT_URL="https://$HOST"
else
    CHECK_URL="http://$HOST:$PORT/mcp"
    VAULT_URL="http://$HOST:$PORT"
fi

# Wait for health (send API key if configured, since HTTP transport requires auth)
HEALTH_OK=false
echo -n "Waiting for vault to be ready..."
for i in $(seq 1 20); do
    if python3 -c "
import os, urllib.request
req = urllib.request.Request('$CHECK_URL')
key = os.environ.get('MEMENTO_API_KEY', '')
if key:
    req.add_header('Authorization', f'Bearer {key}')
urllib.request.urlopen(req, timeout=3)
" 2>/dev/null; then
        echo ""
        info "Vault is healthy!"
        HEALTH_OK=true
        break
    fi
    echo -n "."
    sleep 3
done
echo ""
if [ "$HEALTH_OK" != true ]; then
    warn "Vault did not become healthy within 60 seconds."
    warn "Check logs: docker compose -f $COMPOSE_FILE logs -f"
fi

# --- Copy vault data ---

if [ "$SKIP_MIGRATE" != true ] && [ "$NOTE_COUNT" -gt 0 ]; then
    step "Step 7: Migrate vault data"

    echo ""
    read -rp "Copy your existing vault ($NOTE_COUNT notes) into Docker? [Y/n] " migrate
    if [[ ! "$migrate" =~ ^[Nn] ]]; then
        CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q vault)
        if [ -n "$CONTAINER_ID" ]; then
            for dir in notes fleeting projects archive; do
                if [ -d "$VAULT_PATH/$dir" ]; then
                    docker cp "$VAULT_PATH/$dir/." "$CONTAINER_ID:/vault/$dir/"
                fi
            done
            if [ -f "$VAULT_PATH/memento.yml" ]; then
                docker cp "$VAULT_PATH/memento.yml" "$CONTAINER_ID:/vault/memento.yml"
            fi
            info "Vault data migrated to Docker container"
        else
            warn "Could not find running container. Copy vault data manually."
        fi
    fi
else
    if [ "$SKIP_MIGRATE" = true ]; then
        info "Skipping vault data migration (--no-migrate)"
    fi
fi

# --- Client connection instructions ---

step "Step 8: Connect clients"

echo ""
echo -e "${BOLD}To connect THIS machine:${NC}"
echo ""
echo -e "  ${CYAN}cd $SCRIPT_DIR${NC}"
echo -e "  ${CYAN}MEMENTO_API_KEY=$API_KEY ./install.sh --remote $VAULT_URL --experimental${NC}"
echo ""
echo -e "${BOLD}To connect ANOTHER machine (laptop, CI, etc.):${NC}"
echo ""
echo -e "  ${CYAN}git clone https://github.com/sandsower/memento-vault.git${NC}"
echo -e "  ${CYAN}cd memento-vault${NC}"
echo -e "  ${CYAN}MEMENTO_API_KEY=$API_KEY ./install.sh --remote $VAULT_URL --experimental${NC}"
echo ""
echo -e "${BOLD}To connect from Claude Code on the web (claude.ai/code):${NC}"
echo ""
echo "  Add to your MCP server config:"
echo ""
echo "    \"memento-vault\": {"
echo "      \"url\": \"$VAULT_URL/mcp\","
echo "      \"headers\": {\"Authorization\": \"Bearer $API_KEY\"}"
echo "    }"
echo ""

if [ "$IS_REMOTE" != true ]; then
    read -rp "Reconfigure this machine now? [Y/n] " reconfig
    if [[ ! "$reconfig" =~ ^[Nn] ]]; then
        MEMENTO_API_KEY="$API_KEY" "$SCRIPT_DIR/install.sh" --remote "$VAULT_URL" --experimental --force
    fi
fi

# --- Done ---

step "Deployment complete!"
echo ""
echo "  Vault URL:  $VAULT_URL"
echo "  MCP URL:    $VAULT_URL/mcp"
echo "  API Key:    $API_KEY"
if [ "$ENABLE_TLS" = true ]; then
    echo "  TLS:        Automatic via Caddy (Let's Encrypt)"
fi
echo ""
echo "Docker management:"
echo "  Logs:       docker compose -f $COMPOSE_FILE logs -f"
echo "  Stop:       docker compose -f $COMPOSE_FILE down"
echo "  Restart:    docker compose -f $COMPOSE_FILE restart"
echo "  Backup:     docker compose -f $COMPOSE_FILE exec vault tar czf - /vault > vault-backup.tar.gz"
echo ""
if [ "$NOTE_COUNT" -gt 0 ]; then
    echo "Your local vault at $VAULT_PATH is preserved as a backup."
fi
echo ""
