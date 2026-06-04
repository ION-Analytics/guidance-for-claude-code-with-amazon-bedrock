#!/bin/bash
# ABOUTME: Lightweight shell wrapper for otel-helper that ensures the local OTEL collector
# ABOUTME: sidecar is running (when present), then checks file cache for headers (avoids PyInstaller startup)
PROFILE="${AWS_PROFILE:-ClaudeCode}"
INSTALL_DIR="$HOME/claude-code-with-bedrock"
PID_FILE="$INSTALL_DIR/collector.pid"
CACHE_DIR="$HOME/.claude-code-session"
CACHE_FILE="$CACHE_DIR/${PROFILE}-otel-headers.json"
RAW_FILE="$CACHE_DIR/${PROFILE}-otel-headers.raw"

# Ensure collector sidecar is running (only in sidecar mode — binary present)
# Use a dedicated <profile>-collector AWS profile so the Go SDK always resolves
# credentials via credential_process (the main profile has static creds in
# ~/.aws/credentials that shadow credential_process and can't auto-refresh).
CRED_CHECK_FILE="$CACHE_DIR/${PROFILE}-cred-check"
if [ -x "$INSTALL_DIR/otelcol" ] && [ -f "$INSTALL_DIR/collector-config.yaml" ]; then
    NEED_START=true
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
        NEED_START=false
        # Periodically validate that collector credentials are still valid.
        # Throttled to once every 5 minutes to avoid overhead on every invocation.
        NOW=$(date +%s)
        LAST_CHECK=0
        [ -f "$CRED_CHECK_FILE" ] && LAST_CHECK=$(cat "$CRED_CHECK_FILE" 2>/dev/null || echo 0)
        if [ $((NOW - LAST_CHECK)) -gt 300 ]; then
            echo "$NOW" > "$CRED_CHECK_FILE"
            if ! AWS_PROFILE="${PROFILE}-collector" aws sts get-caller-identity >/dev/null 2>&1; then
                # credential-process has a stale cache — clear it so the next
                # call gets fresh tokens via OIDC refresh (non-interactive).
                "$INSTALL_DIR/credential-process" --profile "$PROFILE" --clear-cache \
                    >> "$CACHE_DIR/collector.log" 2>&1
                kill "$(cat "$PID_FILE")" 2>/dev/null
                rm -f "$PID_FILE"
                NEED_START=true
            fi
        fi
    fi
    if [ "$NEED_START" = true ]; then
        mkdir -p "$CACHE_DIR"
        # Warm the credential-process cache before starting the collector so the
        # Go SDK gets fresh creds on its first credential_process call at startup.
        "$INSTALL_DIR/credential-process" --profile "$PROFILE" \
            >> "$CACHE_DIR/collector.log" 2>/dev/null
        # Unset any AWS credential env vars inherited from the parent process —
        # they would shadow credential_process and cause immediate 403s if expired.
        env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
            -u AWS_SESSION_EXPIRATION -u AWS_CREDENTIAL_EXPIRATION \
            AWS_PROFILE="${PROFILE}-collector" \
        "$INSTALL_DIR/otelcol" --config "$INSTALL_DIR/collector-config.yaml" \
            >> "$CACHE_DIR/collector.log" 2>&1 &
        echo $! > "$PID_FILE"
    fi
fi

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
