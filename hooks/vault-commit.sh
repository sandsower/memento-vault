#!/usr/bin/env bash
# vault-commit.sh — auto-commit all changes in the memento vault.
# Called by memento-triage.py, memento-sweeper.py, /memento, /memento-defrag.
# Idempotent: exits cleanly if nothing to commit.
#
# Usage: vault-commit.sh [commit message]
# Default message: "auto: vault update"

set -euo pipefail

# Resolve vault path from config, falling back to ~/memento
resolve_vault() {
    local config_files=(
        "$HOME/.config/memento-vault/memento.yml"
        "$HOME/.memento-vault.yml"
    )
    for cfg in "${config_files[@]}"; do
        if [ -f "$cfg" ]; then
            local path
            path=$(grep -E '^vault_path:' "$cfg" 2>/dev/null | sed 's/^vault_path:[[:space:]]*//' | sed 's/^["'"'"']//;s/["'"'"']$//' | sed "s|^~|$HOME|")
            if [ -n "$path" ] && [ -d "$path" ]; then
                echo "$path"
                return
            fi
        fi
    done
    echo "$HOME/memento"
}

VAULT="$(resolve_vault)"
MSG="${1:-auto: vault update}"

cd "$VAULT"

# Init repo if somehow missing
if [ ! -d .git ]; then
    git init
    git add -A
    git commit -m "init: bootstrap memento vault"
    exit 0
fi

record_deleted_note_tombstones() {
    if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
        return 0
    fi

    local deleted_file
    deleted_file="$(mktemp "${TMPDIR:-/tmp}/memento-deleted-notes.XXXXXX")"
    git diff --name-only -z --diff-filter=D HEAD -- 'notes/*.md' > "$deleted_file"
    if [ ! -s "$deleted_file" ]; then
        rm -f "$deleted_file"
        return 0
    fi

    python3 - "$deleted_file" <<'PY'
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path

paths_file = Path(sys.argv[1])
vault = Path.cwd()
raw_paths = [p for p in paths_file.read_bytes().split(b"\0") if p]
tombstone_path = vault / ".memento" / "tombstones.jsonl"
latest_by_path = {}
if tombstone_path.exists():
    for line in tombstone_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = rec.get("path")
        if path and (path not in latest_by_path or str(rec.get("ts", "")) >= str(latest_by_path[path].get("ts", ""))):
            latest_by_path[path] = rec

records = []
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for raw in raw_paths:
    rel = raw.decode("utf-8", "surrogateescape")
    try:
        content = subprocess.check_output(["git", "show", f"HEAD:{rel}"])
    except subprocess.CalledProcessError:
        continue
    content_hash = hashlib.sha256(content).hexdigest()
    latest = latest_by_path.get(rel)
    if latest and latest.get("reason", "deleted") != "restored" and latest.get("content_hash") == content_hash:
        continue
    records.append({"ts": now, "path": rel, "reason": "deleted", "content_hash": content_hash})

if records:
    tombstone_path.parent.mkdir(parents=True, exist_ok=True)
    with tombstone_path.open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
PY
    rm -f "$deleted_file"
}

record_deleted_note_tombstones

# Stage everything (new files, modifications, deletions, tombstones)
git add -A

# Only commit if there are staged changes
if ! git diff --cached --quiet; then
    git commit -m "$MSG"
fi
