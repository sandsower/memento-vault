# Remote vault (access from any device)

Deploy the vault as a remote service so multiple devices and agents share the same knowledge base.
The local install is the default; remote mode is opt-in.

## Connect to an existing remote vault

```bash
./install.sh --remote https://vault.example.com:8745
```

This installs hooks that sync to the remote vault over HTTP.
A local vault is always created -- remote mode is additive, not a replacement.

## Deploy the vault yourself

Four options depending on your setup:

| Option | Cost | What you need |
|--------|------|---------------|
| [Docker Compose](#docker-compose) | -- | Docker on any machine |
| [Fly.io](#flyio) | ~$3-5/mo | Fly.io account |
| [Cloudflare Tunnel](#cloudflare-tunnel) | Free | Docker + Cloudflare account with a domain |

### Docker Compose

The simplest option.
Run on any machine with Docker -- a home server, VPS, or your laptop.

```bash
MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") \
  docker compose up -d
```

Vault is at `http://localhost:8745/mcp`.
For TLS on a VPS, add Caddy or use `setup-remote.sh --host your-domain.com --tls`.

### Fly.io

Managed cloud with persistent volumes, automatic TLS, ~$3-5/mo.

```bash
fly launch --copy-config --no-deploy
fly volumes create vault_data --region iad --size 1 --yes
fly secrets set MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fly deploy
```

Vault is at `https://<app-name>.fly.dev/mcp`.
The included `fly.toml` is pre-configured.

### Cloudflare Tunnel

Expose a local Docker container to the internet via Cloudflare.
No public IP needed, automatic TLS, free.

1. Create a tunnel in [Cloudflare Zero Trust](https://one.dash.cloudflare.com) -> Networks -> Tunnels
2. Set the tunnel's public hostname to point at `http://vault:8745`
3. Run:

```bash
export CLOUDFLARE_TUNNEL_TOKEN=<your-token>
export MEMENTO_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose -f docker-compose.cloudflare.yml up -d
```

## Connecting clients

Once the vault is running, connect any device:

```bash
MEMENTO_API_KEY=<key> ./install.sh --remote https://vault.example.com --experimental
```

The installer registers the remote MCP server with Claude Code and Codex when their CLIs are installed.
Codex stores only the bearer-token environment variable name, so start Codex with `MEMENTO_API_KEY` available in the environment.

To upgrade an existing Claude-only remote install after installing Codex, rerun:

```bash
./install.sh --remote --experimental
```

If `~/.claude/memento-remote.env` exists, the installer reuses the saved remote URL and API key.

Or configure MCP directly -- see [docs/mcp.md](mcp.md#connecting-remote-http) for the Claude Code, Codex, and other-agent connection snippets.

## Architecture

The remote vault runs the same `memento/` package described in [docs/architecture.md](architecture.md), fronted by an HTTP transport instead of stdio, with pluggable bearer-token auth (`memento/auth.py`).
Hooks talk to it via `memento/remote_client.py` instead of calling the local vault directly.
