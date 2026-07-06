# Documentation index

Grouped by what you're trying to do, not by when it was written.

## Get it running

- [Install](install.md) -- flags, upgrading, requirements, health/doctor, portable archives, model warmup
- [Configuration](configuration.md) -- every tunable knob, one config file
- [Search backends](search-backends.md) -- QMD, Embedded, and Grep, and when each is used

## Integrate an agent

- [MCP](mcp.md) -- tool inventory, filtered search, `memento_related`, connecting Cursor/Codex/Windsurf/OpenCode locally or remotely
- [Pi extension](pi.md) -- native pi integration, TUI commands, queue lifecycle
- [OpenCode integration](opencode-integration.md) -- MCP config and AGENTS.md instructions for OpenCode
- [Remote deployment](remote-deployment.md) -- Docker Compose, Fly.io, Cloudflare Tunnel

## Understand the system

- [How it works](how-it-works.md) -- capture flow, Tenet retrieval, Inception consolidation, defrag
- [Architecture](architecture.md) -- module map, request flow, LLM backend abstraction
- [Frontmatter schema](frontmatter-schema.md) -- note fields, certainty scale, durability tiers, auto-archive and fleeting-note lifecycle
- [Performance analysis](performance-analysis.md) -- latency, injection rate, and cost benchmarks
- [Automation MemoryProvider contract](automation-memory-provider.md) -- rules for Rondo/Beislið-style automated runners

## History

Point-in-time analyses, release process notes, and superseded plans live in [docs/history/](history/).
They are not maintained against current behavior; read them for context, not as current documentation.
