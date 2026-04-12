#!/usr/bin/env python3
"""Install helpers for memento-vault.

Consolidates all JSON manipulation needed by install.sh into a single
script with subcommand dispatch. Called via:
    python3 lib/install_helpers.py <subcommand> [args...]
"""
import json
import os
import re
import sys
import tempfile


# --- Manifest operations ---

def manifest_load(manifest_path):
    """Print 'version\\nvault_path' from the manifest file."""
    if not os.path.exists(manifest_path):
        print("\n")
        return
    with open(manifest_path) as f:
        m = json.load(f)
    print(m.get("version", ""))
    print(m.get("vault_path", ""))


def manifest_hash(manifest_path, key):
    """Print the stored checksum for a file key."""
    if not os.path.exists(manifest_path):
        return
    with open(manifest_path) as f:
        m = json.load(f)
    print(m.get("files", {}).get(key, ""))


def manifest_record(json_acc, key, hash_val):
    """Merge a key into the JSON accumulator, print updated JSON."""
    d = json.loads(json_acc)
    d[key] = hash_val
    print(json.dumps(d))


def manifest_save(json_acc, version, vault_path, manifest_path):
    """Write the manifest file atomically."""
    files = json.loads(json_acc)
    manifest = {"version": version, "vault_path": vault_path, "files": files}
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


# --- MCP configuration ---

def mcp_config(remote_mode, claude_dir, remote_url, api_key):
    """Build MCP entry and write/merge mcp-servers.json."""
    remote = remote_mode == "true"

    # Build the MCP entry
    if remote:
        url = remote_url.rstrip("/")
        if not url.endswith("/mcp"):
            url += "/mcp"
        entry = {"memento-vault": {"type": "http", "url": url}}
        if api_key:
            entry["memento-vault"]["headers"] = {
                "Authorization": f"Bearer {api_key}"
            }
    else:
        entry = {
            "memento-vault": {
                "command": "python3",
                "args": ["-m", "memento"],
                "env": {"PYTHONPATH": claude_dir + "/hooks"},
            }
        }

    # Write/merge mcp-servers.json
    config_path = os.path.join(claude_dir, "mcp-servers.json")
    if not os.path.isdir(claude_dir):
        return

    if os.path.exists(config_path):
        with open(config_path) as f:
            existing = json.load(f)
        existing.update(entry)
        data = existing
    else:
        data = entry

    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(config_path), suffix=".json"
    )
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config_path)


# --- Remote environment file ---

def remote_env(env_file_path, remote_url, api_key):
    """Write the remote environment file (key=value format)."""
    env = {"MEMENTO_VAULT_URL": remote_url}
    if api_key:
        env["MEMENTO_API_KEY"] = api_key
    lines = [f"{k}={json.dumps(v)}" for k, v in env.items()]
    with open(env_file_path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --- Settings.json merge ---

def merge_settings(settings_path, claude_dir, vault_path, experimental, hook_env_prefix):
    """Inject hooks and permissions into Claude Code settings.json."""
    hooks_dir = claude_dir + "/hooks/"
    prefix = hook_env_prefix
    is_experimental = experimental == "true"

    # Build the hooks we want present
    wanted = {
        "SessionEnd": {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": prefix + "python3 " + hooks_dir + "memento-triage.py",
                    "timeout": 30,
                    "async": True,
                }
            ],
        },
    }
    if is_experimental:
        wanted["SessionStart"] = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": prefix + "python3 " + hooks_dir + "vault-briefing.py",
                    "timeout": 8,
                }
            ],
        }
        wanted["UserPromptSubmit"] = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": prefix + "python3 " + hooks_dir + "vault-recall.py",
                    "timeout": 5,
                }
            ],
        }
        wanted["PreToolUse"] = {
            "matcher": "Read",
            "hooks": [
                {
                    "type": "command",
                    "command": prefix + "python3 " + hooks_dir + "vault-tool-context.py",
                    "timeout": 2,
                }
            ],
        }

    # Load or create settings
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            cfg = json.load(f)
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})

    # Inject missing hooks
    added = []
    for event, entry in wanted.items():
        event_hooks = hooks.setdefault(event, [])
        hook_script = entry["hooks"][0]["command"]
        script_name = (
            hook_script.rsplit("/", 1)[-1] if "/" in hook_script else hook_script
        )
        already = any(
            script_name in h.get("command", "")
            for item in event_hooks
            for h in (item.get("hooks", [item]) if isinstance(item, dict) else [])
        )
        if not already:
            event_hooks.append(entry)
            added.append(event + "/" + script_name.replace(".py", ""))

    # Normalize existing hook commands (update remote prefix)
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            hook_list = (
                entry.get("hooks", [entry]) if isinstance(entry, dict) else []
            )
            for hook in hook_list:
                cmd = hook.get("command", "")
                if hooks_dir not in cmd:
                    continue
                match = re.search(
                    r"(python3\s+" + re.escape(hooks_dir) + r".*)", cmd
                )
                cleaned = match.group(1) if match else cmd
                hook["command"] = prefix + cleaned

    # Ensure vault permissions exist
    perms = cfg.setdefault("permissions", {}).setdefault("allow", [])
    base_dir = hooks_dir.rstrip("/").rsplit("/", 1)[0]
    vault_rules = [
        "Read(" + vault_path + "/**)",
        "Edit(" + vault_path + "/**)",
        "Write(" + vault_path + "/**)",
        "Bash(" + base_dir + "/hooks/vault-commit.sh:*)",
    ]
    for rule in vault_rules:
        if rule not in perms:
            perms.append(rule)

    # Write atomically
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(settings_path), suffix=".json"
    )
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, settings_path)

    if added:
        print("Hooks added: " + ", ".join(added))
    else:
        print("All hooks already configured")


# --- MCP URL helper (for bash to capture) ---

def mcp_url(remote_url):
    """Normalize remote URL to MCP endpoint and print it."""
    url = remote_url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    print(url)


# --- MCP warmup (wake suspended/stopped Fly.io machines) ---

def warmup(remote_url, api_key):
    """Ping the remote vault with retries to wake it from suspend/stop.

    Prints 'OK <name> v<version>' on success, exits 1 on failure.
    """
    import time
    import urllib.request

    url = remote_url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "memento-installer", "version": "1.0.0"},
        },
    }).encode()

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(
                url, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
            if "result" in result:
                info = result["result"].get("serverInfo", {})
                print(f"OK {info.get('name', 'unknown')} v{info.get('version', '?')}")
                return
            raise RuntimeError(result.get("error", {}).get("message", "bad response"))
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(2)
                continue
            print(f"FAIL {e}", file=sys.stderr)
            sys.exit(1)


# --- Clear stale MCP auth cache ---

def clear_auth_cache(claude_dir, server_name):
    """Remove a server from Claude Code's mcp-needs-auth-cache.json."""
    cache_path = os.path.join(claude_dir, "mcp-needs-auth-cache.json")
    if not os.path.exists(cache_path):
        return
    with open(cache_path) as f:
        data = json.load(f)
    if server_name in data:
        del data[server_name]
        with open(cache_path, "w") as f:
            json.dump(data, f)
        print(f"Cleared {server_name} from auth cache")
    else:
        print(f"No stale cache for {server_name}")


# --- Dispatch ---

COMMANDS = {
    "manifest-load": lambda: manifest_load(sys.argv[2]),
    "manifest-hash": lambda: manifest_hash(sys.argv[2], sys.argv[3]),
    "manifest-record": lambda: manifest_record(
        sys.argv[2], sys.argv[3], sys.argv[4]
    ),
    "manifest-save": lambda: manifest_save(
        sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    ),
    "mcp-config": lambda: mcp_config(
        sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    ),
    "merge-settings": lambda: merge_settings(
        sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
        sys.argv[6] if len(sys.argv) > 6 else "",
    ),
    "remote-env": lambda: remote_env(
        sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""
    ),
    "mcp-url": lambda: mcp_url(sys.argv[2]),
    "warmup": lambda: warmup(
        sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
    ),
    "clear-auth-cache": lambda: clear_auth_cache(sys.argv[2], sys.argv[3]),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(
            f"Usage: {sys.argv[0]} <{'|'.join(COMMANDS)}> [args...]",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        COMMANDS[sys.argv[1]]()
    except Exception as e:
        print(f"install_helpers: {sys.argv[1]}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
