# Windows Console Window Flashing Fix

## Problem

Windows users reported credential-process spawning dozens of CMD windows every few minutes during Claude Code sessions. The windows appeared as short-lived processes in Task Manager and flashed briefly on screen.

## Root Cause

The AWS SDK invokes `credential_process` (configured in `~/.aws/config`) on **every AWS API call** to check if credentials need refreshing. During a Claude Code conversation with streaming responses, this can result in:

- **5-10 credential checks per minute** during active conversation
- **Dozens of process spawns** as Claude Code makes multiple Bedrock API calls
- Each `credential-process.exe` invocation spawned a **visible console window** because the binary was built as a console subsystem application

The credential-process is designed to return cached credentials instantly (from `~/.aws/credentials`) without triggering browser auth, but the AWS SDK still calls it frequently to verify expiration times.

## The Fix

Modified [source/go/Makefile](source/go/Makefile) to build Windows binaries with `-H windowsgui` ldflags:

```makefile
# Before:
LDFLAGS_WIN := -X ccwb-go/internal/version.Version=$(VERSION)

# After:
LDFLAGS_WIN := -H windowsgui -X ccwb-go/internal/version.Version=$(VERSION)
```

This builds the executables as **GUI subsystem** applications instead of console applications, preventing the console window from appearing.

### What Still Works

- ✅ **Credential output**: stdout still works (AWS SDK captures JSON credentials)
- ✅ **Error messages**: stderr still works (visible when run from PowerShell/CMD)
- ✅ **Interactive commands**: `--version`, `--clear-cache`, `--get-monitoring-token` all work
- ✅ **Browser authentication**: Opens browser popup when credentials expire
- ✅ **Daemon mode**: Background daemon continues to run with `--daemon` flag

## Testing Instructions

### Build the Fix

```bash
cd source/go
make clean
make windows
```

This creates:
- `bin/credential-process-windows.exe`
- `bin/otel-helper-windows.exe`

### Verify Binary Subsystem

On macOS (cross-compile check):
```bash
file bin/credential-process-windows.exe | grep GUI
```

On Windows (after installing):
```powershell
# Run from PowerShell - should NOT show console window
$env:AWS_PROFILE = "ClaudeCode"
aws sts get-caller-identity

# Verify it works - should output JSON
~\claude-code-with-bedrock\credential-process.exe --profile ClaudeCode
```

### Test with Claude Code

1. Install the new binaries:
   ```batch
   copy /Y bin\credential-process-windows.exe %USERPROFILE%\claude-code-with-bedrock\credential-process.exe
   copy /Y bin\otel-helper-windows.exe %USERPROFILE%\claude-code-with-bedrock\otel-helper.exe
   ```

2. Open Task Manager → Details tab

3. Start Claude Code and have a conversation with streaming responses

4. **Expected behavior**: 
   - NO visible console windows
   - `credential-process.exe` processes appear in Task Manager briefly (normal)
   - Processes exit quickly after returning cached credentials

5. **Verify credential refresh** (optional - takes 8-12 hours):
   ```batch
   REM Force credential expiration
   credential-process.exe --profile ClaudeCode --clear-cache
   
   REM Next AWS API call should trigger browser auth
   claude
   ```

## Additional Notes

### Why Not `-H=windowsgui`?

The Go linker accepts both `-H windowsgui` and `-H=windowsgui` syntax. We use the space-separated form for consistency with other ldflags.

### Windows Defender False Positives

The Makefile comment explains we do NOT strip binaries (`-s -w`) on Windows because Defender's cloud ML flags stripped Go binaries as `Wacatac.B!ml`. The `.syso` resource files provide PE version info to help prevent false positives.

### Scheduled Task Watchdog

Windows installations include a Scheduled Task (`ClaudeCodeDaemon`) that keeps the background daemon alive. This is separate from the credential-process hot path invoked by AWS SDK.

## Release Notes

Include this in the next release:

```
### Fixed

- **Windows console window flashing**: Fixed credential-process.exe and otel-helper.exe 
  spawning visible console windows on every AWS API call. Binaries are now built with 
  `-H windowsgui` ldflags (GUI subsystem) to suppress console windows while preserving 
  stdout/stderr for credential output and error messages.
```

## References

- Issue: Windows users report "credential-process spawning like crazy"
- Related memory: `project_otelcol_credential_fix.md` (separate issue about env var shadowing)
- AWS SDK credential_process protocol: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html
