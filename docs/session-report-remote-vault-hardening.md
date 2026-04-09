# Session Report: Remote Vault Hardening

**Branch:** `claude/multi-user-vault-docker-jP2b7`
**Date:** 2026-04-09
**Commits:** 13 (4 features + 9 hardening fixes)
**Files changed:** 28 (+2,620 / -255 lines)
**Test suite:** 83 tests passing

---

## Context

A previous session had begun implementing multi-user remote vault support with Docker, HTTP transport, and bearer token auth. That session stalled with 6 files of uncommitted changes across the branch. This session picked up the work, reviewed it, and ran **8 adversarial Codex review rounds** in a fix-review-fix loop until all critical and high-severity findings were resolved.

The branch adds the ability to deploy Memento Vault as a remote service (Docker, Fly.io, Cloudflare Tunnel, Oracle Cloud) and connect multiple devices/agents to a shared knowledge base. The original implementation treated remote mode as a full replacement for local — this session's most significant architectural decision was changing that to a **local-first** model where the local vault is always the primary store and remote is an additive sync layer.

---

## What was inherited (4 commits, uncommitted)

The branch already had 4 committed features plus 6 files of uncommitted work:

| Commit | Description |
|--------|-------------|
| `100c524` | Core remote support: Docker, HTTP transport, `remote_client.py`, `auth.py`, hooks with `is_remote()` branching |
| `c057a6c` | Migration path: `setup-remote.sh` for local-to-Docker vault migration |
| `043a35f` | VPS deployment with optional TLS via Caddy reverse proxy |
| `0a224e4` | Cloud-init templates (Oracle), Fly.io, Cloudflare Tunnel configs, README |

Uncommitted work included a `GrepBackend` fallback search, cloud-init variable scoping fixes, remote error handling in triage, and `sed`-based hook command updates.

---

## Review methodology

Each round followed the same protocol:

1. **My review** — Read all changed files, trace call chains, verify type signatures, check edge cases
2. **Codex adversarial review** — Independent automated review via `codex-companion.mjs adversarial-review --base main`, positioned as a challenge review questioning design choices and assumptions
3. **Fix** — Address all critical/high findings, re-run tests
4. **Repeat** until clean

A `/loop 5m` cron was set up to automate the fix-review cycle. The loop ran 8 iterations before being stopped.

---

## Round-by-round findings and fixes

### Round 1 — Initial review of uncommitted work

**My findings (manual review):**
- **[critical]** `memento-triage.py` fallback: `write_note()` missing required `vault_path` argument — would crash on every remote fallback
- **[critical]** `update_project_index()` called with wrong signature `(meta["cwd"], [str(note_path)])` instead of the actual 4-arg signature
- **[critical]** `install.sh` sed substitution with raw URL/API key expansion — `&` in values interpreted as sed backreference

**Codex findings:**
- **[high]** Remote-capture fallback writes raw (unsanitized) session notes into the client's local vault, bypassing `sanitize_secrets()`
- **[medium]** `GrepBackend` ignores timeout parameter and does unbounded full-vault scan

**Fixes applied in `d59449f`:**
- Fixed `write_note` and `update_project_index` call signatures
- Replaced sed with Python JSON manipulation for hook updates
- Changed fallback to write to isolated `vault/spool/remote-failures/` directory with `sanitize_secrets()` applied
- Added timeout enforcement and early termination to `GrepBackend`
- Removed `has_qmd()` gate from `memento_search` (GrepBackend handles fallback)
- Added stderr logging for remote search errors
- Fixed cloud-init variable scoping bug
- Updated test assertions for new backend fallback behavior

### Round 2 — Full branch review

**Codex findings:**
- **[critical]** Remote triage drops non-substantial sessions entirely (local path preserves them as fleeting entries)
- **[high]** Docker image ships without a working search backend (no QMD binary)
- **[high]** `config['api_key']` from memento.yml never wired into MCP server auth — only env var works

**My findings:**
- **[high]** `setup-remote.sh` health check disables TLS verification entirely
- **[medium]** `memento_search` returns `dict` error where `list[dict]` is the declared return type
- **[medium]** API key appears in plaintext in settings.json, terminal output, and CONNECTION_INFO.txt

**Fixes applied in `8a7b4d8`:**
- MCP server now refuses HTTP transport on non-localhost without `MEMENTO_API_KEY`
- Auth wired through `create_auth_provider()` (reads both env var and config)
- Docker compose files use `${MEMENTO_API_KEY:?}` (hard fail on missing)
- Added `fleeting_only` parameter to `memento_capture` so remote hooks can log non-substantial sessions without permanent notes
- Fixed `memento_search` return type consistency

### Round 3 — Architectural pivot: local-first

During review, we discovered that remote mode was a **full replacement** for local — all hooks short-circuited with `sys.exit(0)` after the remote path, `install.sh` skipped local vault creation entirely. This was identified as a design flaw: users should always have their local notes.

**Fixes applied in `5e11b69`:**
- `install.sh`: Always creates local vault, even with `--remote`
- **Triage hook**: Runs full local pipeline first (fleeting, project index, agent spawn), then additionally syncs to remote as best-effort
- **Read hooks** (briefing, recall, tool-context): Try remote first for richer cross-device data, fall through to local if remote fails or returns nothing
- All hooks: Hoisted `read_hook_input()` before remote/local branching to avoid double-read of consumed stdin

### Round 4 — QMD and briefing edge cases

**Codex findings:**
- **[high]** Remote installs with `qmd` binary present but no `memento` collection configured: `QMDBackend.is_available()` returns true but searches fail silently, never falling back to grep
- **[medium]** Stale `DEFERRED_BRIEFING_PATH` from a prior session can leak old remote notes into a new session

**Fixes applied in `d46d9a6`:**
- `QMDBackend.is_available()` now runs a test query against the configured collection — returns false if collection doesn't exist
- `install.sh`: QMD setup no longer skipped in remote mode
- `vault-briefing.py`: Clears stale deferred briefing file before remote attempt

### Round 5 — Security and deployment hardening

**Codex findings:**
- **[high]** `transcript_path` allowlist uses `str.startswith()` — paths like `/tmp-evil/session.jsonl` pass the `/tmp` check (prefix, not directory boundary)
- **[high]** Reinstalling without `--remote` doesn't strip old remote hook prefixes from settings.json — hooks silently keep syncing to the old remote vault
- **[medium]** Docker healthcheck hits auth-protected `/mcp` endpoint without credentials — secured deployments report unhealthy

**Fixes applied in `25cd158`:**
- Path containment check replaced with `Path.resolve()` + proper parent chain check
- Hook normalization now runs on every install (not just `--remote`) — local reinstalls strip stale remote prefixes
- Docker healthcheck sends `MEMENTO_API_KEY` as bearer token

### Round 6 — Data integrity and identity

**Codex findings:**
- **[high]** `fleeting_only` remote captures write dead `[[session_id]]` wikilinks into project indexes (note name is session ID, not a real note file)
- **[medium]** `setup-remote.sh` doesn't copy `vault-identity.json` during Docker migration
- **[medium]** Vault identity stored in global `~/.config/` — two vaults on the same machine share one ID

**Fixes applied in `b21f321` and `fc92ed4`:**
- `update_project_index` only called after real note creation — fleeting-only path skips note links
- Vault identity moved inside the vault directory itself (with migration from legacy location)
- `setup-remote.sh` copies `vault-identity.json` during migration
- `transcript_path` rejected entirely over HTTP transport (remote callers must use `session_summary`)
- Added tests for vault identity migration and multi-vault separation

### Round 7 — Triage parity and search coverage

**Codex findings:**
- **[high]** Remote triage uses `is_substantial()` alone to gate permanent notes — local triage requires `substantial AND new_insight`
- **[high]** `GrepBackend` only searches `notes/` — fleeting-only sessions in `fleeting/` and `projects/` are invisible to remote search
- **[medium]** `memento_capture` not idempotent — HTTP retries can create duplicate fleeting entries and notes

**Fixes applied in `0807049`:**
- Remote triage now uses `substantial and new_insight` gating (exact match with local)
- `GrepBackend` searches `notes/`, `fleeting/`, and `projects/` directories
- Capture is idempotent on `session_id` — checks for existing note/fleeting entry before writing

### Round 8 — Transport awareness and side effects

**Codex findings:**
- **[high]** `transcript_path` rejection checks `MEMENTO_TRANSPORT` env var — but `--transport streamable-http` CLI flag doesn't set it, so the gate is bypassed
- **[high]** `memento_status` calls `get_vault_id()` which creates directories as a side effect — a missing vault mount appears as present after a health check

**Fixes applied in `01b3a1a`:**
- Added module-level `_active_transport` variable set at startup from parsed CLI args, used by tools instead of env var
- `memento_status` only reads vault identity if the file already exists — no directory creation on status/health checks

---

## Architecture summary

The final architecture is **local-first with optional remote sync**:

```
Session ends
  |
  v
Local triage (always runs)
  |-- Write fleeting entry
  |-- Update project index
  |-- If substantial + new_insight: spawn agent for atomic notes
  |-- Reindex search backend
  |
  v
Remote sync (if MEMENTO_VAULT_URL configured)
  |-- Send capture to remote vault (fleeting_only or full)
  |-- On failure: spool to vault/spool/remote-failures/
  |
  v
Done
```

For reads (briefing, recall, tool-context):
```
Try remote first (has cross-device data)
  |-- If results found: use them, exit
  |-- If no results or failure: fall through
  |
  v
Local search (GrepBackend or QMDBackend)
```

### Security boundaries

| Boundary | Protection |
|----------|-----------|
| HTTP transport without API key | Startup refused on non-localhost |
| `transcript_path` over HTTP | Rejected at tool level via `_active_transport` check |
| `transcript_path` path traversal | `Path.resolve()` + parent chain containment |
| Docker compose without API key | `${MEMENTO_API_KEY:?}` — hard fail |
| Healthcheck on secured deployment | Bearer token from container env |
| Session data on remote failure | Sanitized spool, not main vault |
| Reinstall without --remote | Stale remote prefixes stripped from hooks |

### Key modules added/modified

| Module | Role |
|--------|------|
| `memento/auth.py` | Pluggable auth (NoAuth, BearerToken, MCP TokenVerifier) |
| `memento/remote_client.py` | HTTP client for hooks talking to remote vault |
| `memento/search_backend.py` | Abstract backend with QMDBackend + GrepBackend fallback |
| `memento/mcp_server.py` | HTTP transport, auth wiring, `fleeting_only` capture, idempotency |
| `memento/config.py` | Per-vault identity, legacy migration |
| `install.sh` | `--remote` flag, safe JSON hook updates, always-local vault |
| `setup-remote.sh` | Docker deployment helper with auth health checks |
| `Dockerfile` | Non-root user, authenticated healthcheck |

### Deployment options

| Option | Cost | TLS |
|--------|------|-----|
| Docker Compose | Free | Manual (Caddy/nginx) |
| Fly.io | ~$3-5/mo | Automatic |
| Cloudflare Tunnel | Free | Automatic |
| Oracle Cloud Free | Free forever | Manual |

---

## Test coverage

83 tests across 6 test files covering:
- Auth providers (NoAuth, BearerToken, env vs config precedence)
- Remote client (search, get, store, capture, status, auth header, URL handling)
- Search backend (QMD/Grep fallback, singleton lifecycle, min_score, snippet cleaning)
- Vault identity (generation, persistence, corruption recovery, legacy migration, multi-vault isolation)
- Store operations (write, lock, dedup, project index)
- Triage (sanitization, LLM error handling, lock timeout, transcript parsing)

---

## Open items for next session

The last Codex review (round 8) still returned `needs-attention`. While both findings were fixed, the review hasn't been re-run post-fix. The next session should:

1. Run a final Codex adversarial review to confirm clean status
2. Consider squashing the 9 fix commits into the 4 feature commits for a cleaner PR
3. Create the PR against `main`
4. Consider adding integration tests for Docker deployment path
