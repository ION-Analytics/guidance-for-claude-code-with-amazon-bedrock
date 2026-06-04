#!/bin/bash
# ABOUTME: Lightweight shell wrapper for otel-helper that checks file cache for headers
# ABOUTME: (avoids PyInstaller startup). otelcol lifecycle is managed by the credential daemon.
PROFILE="${AWS_PROFILE:-ClaudeCode}"
INSTALL_DIR="$HOME/claude-code-with-bedrock"
CACHE_DIR="$HOME/.claude-code-session"
CACHE_FILE="$CACHE_DIR/${PROFILE}-otel-headers.json"
RAW_FILE="$CACHE_DIR/${PROFILE}-otel-headers.raw"

# Check if cache exists and token is still valid
if [ -f "$CACHE_FILE" ] && [ -f "$RAW_FILE" ]; then
    # Extract token_exp from JSON using grep+sed (no jq dependency)
    TOKEN_EXP=$(grep -o '"token_exp":[[:space:]]*[0-9]*' "$CACHE_FILE" | sed 's/.*:[[:space:]]*//')
    NOW=$(date +%s)

    if [ -n "$TOKEN_EXP" ] && [ "$TOKEN_EXP" -gt "$((NOW + 60))" ]; then
        # Token still valid (>60s remaining) - serve cached headers
        cat "$RAW_FILE"
        exit 0
    fi
    # Token expired or missing - fall through to binary
fi

# Cache miss or expired - fall back to full PyInstaller binary (which writes the cache)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/otel-helper-bin" "$@"
