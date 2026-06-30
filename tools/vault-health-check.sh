#!/usr/bin/env bash
# Legacy structural vault checker.
#
# Usage: bash vault-health-check.sh [path-to-vault]
# Default: ~/memento
#
# This script is intentionally kept for low-level content checks that are not
# covered by `memento-vault health` (frontmatter, wikilinks, file naming). Use
# `memento-vault health` or `memento-vault doctor` for operational diagnostics.

set -euo pipefail

VAULT="${1:-$HOME/memento}"
ERRFILE=$(mktemp)
echo 0 > "$ERRFILE"

cat <<'EOF'
Legacy structural vault check: kept for direct callers and low-level vault
content validation. For operational/install diagnostics, use:
  memento-vault health
EOF

echo ""

inc_errors() {
    local count
    count=$(cat "$ERRFILE")
    echo $((count + 1)) > "$ERRFILE"
}

if [ ! -d "$VAULT" ]; then
    echo "ERROR: Vault not found at $VAULT"
    rm -f "$ERRFILE"
    exit 1
fi

# Check 1: Required directories exist
echo "Checking directory structure..."
for dir in fleeting notes projects archive; do
    if [ ! -d "$VAULT/$dir" ]; then
        echo "  MISSING: $VAULT/$dir/"
        inc_errors
    fi
done

# Check 2: All notes have YAML frontmatter
echo "Checking frontmatter..."
if [ -d "$VAULT/notes" ]; then
    find "$VAULT/notes" -name '*.md' | while read -r file; do
        if ! head -1 "$file" | grep -q '^---$'; then
            echo "  NO FRONTMATTER: $file"
            inc_errors
        fi
    done
fi

# Check 3: Wikilinks in notes point to existing files
echo "Checking wikilinks..."
if [ -d "$VAULT/notes" ]; then
    find "$VAULT/notes" -name '*.md' | while read -r file; do
        while read -r link; do
            target="$VAULT/notes/$link.md"
            archive_target="$VAULT/archive/$link.md"
            if [ ! -f "$target" ] && [ ! -f "$archive_target" ]; then
                echo "  BROKEN LINK: [[$link]] in $(basename "$file") -> not found"
                inc_errors
            fi
        done < <(grep -Eo '\[\[[^]|]+' "$file" 2>/dev/null | sed 's/\[\[//' || true)
    done
fi

# Check 4: File naming conventions (no uppercase or spaces in notes)
echo "Checking file naming..."
if [ -d "$VAULT/notes" ]; then
    find "$VAULT/notes" -name '*.md' | while read -r file; do
        filename=$(basename "$file")
        if echo "$filename" | grep -Eq '[[:upper:][:space:]]'; then
            echo "  NAMING: $filename has uppercase or spaces"
            inc_errors
        fi
    done
fi

# Check 5: Git repo initialized
echo "Checking git..."
if [ ! -d "$VAULT/.git" ]; then
    echo "  WARNING: Vault is not a git repository"
    inc_errors
fi

ERRORS=$(cat "$ERRFILE")
rm -f "$ERRFILE"

if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "Found $ERRORS issue(s)."
    exit 1
else
    echo ""
    echo "Vault health check passed."
    exit 0
fi
