#!/usr/bin/env bash
# Max-Token-Saver installer for Claude Code
# Usage: bash install.sh [--force] [--claude-dir PATH]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="max-token-saver"
FORCE=0
CLAUDE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --claude-dir) CLAUDE_DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Detect claude config directory
if [[ -z "$CLAUDE_DIR" ]]; then
    CLAUDE_DIR="$HOME/.claude"
    mkdir -p "$CLAUDE_DIR"
fi

DEST="$CLAUDE_DIR/plugins/marketplaces/$PLUGIN_NAME"
DATA_DIR="$CLAUDE_DIR/plugins/$PLUGIN_NAME"
STATUSLINE_SCRIPT="$DEST/scripts/mts_statusline.py"
COMBINED_STATUSLINE="$CLAUDE_DIR/mts-statusline-combined.sh"

echo "Max-Token-Saver installer"
echo "  Source: $SCRIPT_DIR"
echo "  Dest:   $DEST"
echo "  Data:   $DATA_DIR"
echo ""

# Check existing
if [[ -d "$DEST" && $FORCE -eq 0 ]]; then
    echo "Already installed at $DEST"
    echo "Use --force to overwrite."
    exit 1
fi

# Remove old installation
if [[ -d "$DEST" ]]; then
    echo "Removing previous installation..."
    rm -rf "$DEST"
fi

# Copy plugin files
echo "Installing plugin..."
mkdir -p "$DEST"
cp -r "$SCRIPT_DIR/.claude-plugin" "$DEST/"
cp -r "$SCRIPT_DIR/hooks" "$DEST/"
cp -r "$SCRIPT_DIR/scripts" "$DEST/"
cp -r "$SCRIPT_DIR/skills" "$DEST/"
cp -r "$SCRIPT_DIR/commands" "$DEST/"
[[ -f "$SCRIPT_DIR/README.md" ]] && cp "$SCRIPT_DIR/README.md" "$DEST/"

# Create data directory
mkdir -p "$DATA_DIR"

# Initialize overrides if not present
if [[ ! -f "$DATA_DIR/overrides.json" ]]; then
    echo '{"mode": "compact", "mode_updated_at": "install"}' > "$DATA_DIR/overrides.json"
fi

# Make scripts executable
chmod +x "$DEST/scripts/max_token_saver.py"
chmod +x "$DEST/scripts/mts_statusline.py"

# Install statusline
echo "Setting up statusline..."

cat > "$COMBINED_STATUSLINE" << 'STATUSLINE_EOF'
#!/bin/bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export PYTHONIOENCODING=utf-8
INPUT=$(cat)

# Run the existing counter statusline if present
COUNTER_SCRIPT="CLAUDE_DIR_PLACEHOLDER/claude-counter-statusline.py"
if [[ -f "$COUNTER_SCRIPT" ]]; then
    BARS=$(echo "$INPUT" | python3 "$COUNTER_SCRIPT" 2>/dev/null)
fi

# Run Max-Token-Saver statusline
MTS_SCRIPT="CLAUDE_DIR_PLACEHOLDER/plugins/marketplaces/max-token-saver/scripts/mts_statusline.py"
if [[ -f "$MTS_SCRIPT" ]]; then
    MTS=$(echo "$INPUT" | python3 "$MTS_SCRIPT" 2>/dev/null)
fi

# Combine outputs
OUTPUT=""
if [[ -n "${BARS:-}" ]]; then
    OUTPUT="$BARS"
fi
if [[ -n "${MTS:-}" ]]; then
    if [[ -n "$OUTPUT" ]]; then
        OUTPUT="$OUTPUT  $MTS"
    else
        OUTPUT="$MTS"
    fi
fi

if [[ -n "$OUTPUT" ]]; then
    echo "$OUTPUT"
fi
STATUSLINE_EOF

# Replace placeholder with actual path
sed -i "s|CLAUDE_DIR_PLACEHOLDER|$CLAUDE_DIR|g" "$COMBINED_STATUSLINE"
chmod +x "$COMBINED_STATUSLINE"

# Update settings.json to use the combined statusline
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
if [[ -f "$SETTINGS_FILE" ]]; then
    # Use python to safely update JSON
    python3 -c "
import json, sys
try:
    with open('$SETTINGS_FILE', 'r') as f:
        data = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    data = {}

data['statusLine'] = {
    'type': 'command',
    'command': '$COMBINED_STATUSLINE',
    'refreshInterval': 30
}

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
print('  Updated statusLine in settings.json')
" 2>&1
else
    # Create settings.json with statusline
    cat > "$SETTINGS_FILE" << EOF
{
  "statusLine": {
    "type": "command",
    "command": "$COMBINED_STATUSLINE",
    "refreshInterval": 30
  }
}
EOF
    echo "  Created settings.json with statusLine"
fi

# Verify
echo ""
echo "Verifying installation..."
if python3 "$DEST/scripts/max_token_saver.py" mode status >/dev/null 2>&1; then
    echo "  OK: Script runs correctly"
else
    echo "  WARNING: Script test failed. Check python3 is available."
fi

echo ""
echo "Installation complete!"
echo ""
echo "Usage:"
echo "  Restart Claude Code to activate hooks and statusline."
echo "  /mts:status  — view dashboard"
echo "  /mts:on      — enable compact mode"
echo "  /mts:off     — disable compact mode"
echo "  /mts:full    — get full output for next tool call"
echo "  /mts:compress CLAUDE.md — compress memory files"
echo "  /mts:audit   — find bloated context files"
echo "  /mts:plan    — create implementation contract"
echo "  /mts:guard   — check for scope drift"
echo ""
echo "Statusline shows: Tokens Saved count and percentage"
echo ""
echo "To uninstall: bash $(dirname "$0")/uninstall.sh"
