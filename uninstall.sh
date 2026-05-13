#!/usr/bin/env bash
# Max-Token-Saver uninstaller for Claude Code
# Usage: bash uninstall.sh [--claude-dir PATH]

set -euo pipefail

PLUGIN_NAME="max-token-saver"
CLAUDE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --claude-dir) CLAUDE_DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$CLAUDE_DIR" ]]; then
    CLAUDE_DIR="$HOME/.claude"
fi

PLUGIN_DIR="$CLAUDE_DIR/plugins/marketplaces/$PLUGIN_NAME"
DATA_DIR="$CLAUDE_DIR/plugins/$PLUGIN_NAME"

echo "Max-Token-Saver uninstaller"
echo ""

REMOVED=0

if [[ -d "$PLUGIN_DIR" ]]; then
    echo "Removing plugin: $PLUGIN_DIR"
    rm -rf "$PLUGIN_DIR"
    REMOVED=1
fi

if [[ -d "$DATA_DIR" ]]; then
    echo "Removing data:   $DATA_DIR"
    rm -rf "$DATA_DIR"
    REMOVED=1
fi

if [[ $REMOVED -eq 0 ]]; then
    echo "Nothing to remove. Max-Token-Saver not found at:"
    echo "  $PLUGIN_DIR"
    echo "  $DATA_DIR"
    exit 1
fi

echo ""
echo "Uninstalled. Restart Claude Code to complete removal."
