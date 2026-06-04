"""
Credential daemon — manages otelcol lifecycle and emits a heartbeat metric.

Spawned by credential-process if not already running. Runs as a detached
background process; owns the otelcol PID file and credential refresh cycle.

Heartbeat metric: sends `claude_code_daemon_heartbeat` (value=1) to the local
otelcol OTLP receiver (localhost:4318) every INTERVAL seconds. otelcol forwards
it to the CloudWatch Prometheus endpoint with SigV4, co-located with gen_ai
usage metrics. Absence of the metric for >10 min indicates otelcol bypass.
"""

import json
import logging
import os
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Interval between health checks and heartbeat emissions (seconds)
INTERVAL = 300  # 5 minutes

INSTALL_DIR = Path.home() / "claude-code-with-bedrock"
CACHE_DIR = Path.home() / ".claude-code-session"
DAEMON_PID_FILE = INSTALL_DIR / "daemon.pid"
COLLECTOR_PID_FILE = INSTALL_DIR / "collector.pid"
LOG_FILE = CACHE_DIR / "daemon.log"
OTELCOL_HEALTH_URL = "http://localhost:13133/"
OTLP_ENDPOINT = "http://localhost:4318/v1/metrics"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile() -> str:
    return os.environ.get("AWS_PROFILE", os.environ.get("CCWB_PROFILE", "ClaudeCode"))


def _credential_process() -> Path:
    return INSTALL_DIR / "credential-process"


def _read_email() -> str | None:
    """Read user email from the cached monitoring token (session storage)."""
    profile = _profile()
    token_file = CACHE_DIR / f"{profile}-monitoring.json"
    try:
        with open(token_file) as f:
            data = json.load(f)
        email = data.get("email", "")
        return email if email else None
    except Exception:
        return None


def _otelcol_running() -> bool:
    if not COLLECTOR_PID_FILE.exists():
        return False
    try:
        pid = int(COLLECTOR_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _otelcol_healthy() -> bool:
    try:
        with urllib.request.urlopen(OTELCOL_HEALTH_URL, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _sts_valid() -> bool:
    profile = _profile()
    env = {**os.environ, "AWS_PROFILE": f"{profile}-collector"}
    # Strip inherited credential env vars to avoid shadowing credential_process
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_SESSION_EXPIRATION", "AWS_CREDENTIAL_EXPIRATION"):
        env.pop(var, None)
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            env=env, capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _warm_credentials() -> None:
    profile = _profile()
    cp = _credential_process()
    if not cp.exists():
        return
    env = {k: v for k, v in os.environ.items()
           if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN", "AWS_SESSION_EXPIRATION",
                        "AWS_CREDENTIAL_EXPIRATION")}
    stable_tmp = INSTALL_DIR / "tmp"
    stable_tmp.mkdir(exist_ok=True)
    env["TMPDIR"] = str(stable_tmp)
    try:
        subprocess.run(
            [str(cp), "--profile", profile],
            env=env, capture_output=True, timeout=30,
        )
    except Exception as e:
        log.warning("credential warm failed: %s", e)


def _start_otelcol() -> None:
    config = INSTALL_DIR / "collector-config.yaml"
    otelcol = INSTALL_DIR / "otelcol"
    if not otelcol.exists() or not config.exists():
        log.info("otelcol binary or config not found, skipping")
        return

    profile = _profile()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    _warm_credentials()

    env = {k: v for k, v in os.environ.items()
           if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN", "AWS_SESSION_EXPIRATION",
                        "AWS_CREDENTIAL_EXPIRATION")}
    env["AWS_PROFILE"] = f"{profile}-collector"
    # Use a stable TMPDIR so macOS doesn't clean up PyInstaller's extraction
    # directory while credential-process is running as a subprocess of otelcol.
    stable_tmp = INSTALL_DIR / "tmp"
    stable_tmp.mkdir(exist_ok=True)
    env["TMPDIR"] = str(stable_tmp)

    log_file = open(CACHE_DIR / "collector.log", "a")
    proc = subprocess.Popen(
        [str(otelcol), "--config", str(config)],
        env=env,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    COLLECTOR_PID_FILE.write_text(str(proc.pid))
    log.info("started otelcol pid=%d", proc.pid)


def _stop_otelcol() -> None:
    if not COLLECTOR_PID_FILE.exists():
        return
    try:
        pid = int(COLLECTOR_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log.info("stopped otelcol pid=%d", pid)
    except Exception:
        pass
    COLLECTOR_PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Heartbeat via OTLP
# ---------------------------------------------------------------------------

def _build_otlp_heartbeat(email: str, ts_ns: int) -> bytes:
    """Build a minimal OTLP protobuf payload for claude_code_daemon_heartbeat.

    Encodes the ExportMetricsServiceRequest manually using raw protobuf
    encoding (field tag + wire type + value) to avoid a protobuf dependency.

    Schema:
      gauge{ datapoints=[{ as_double=1.0, time_unix_nano=ts_ns,
               attributes=[{key="user.email", value=email}] }] }
      metric{ name="claude_code_daemon_heartbeat", gauge=... }
      scope_metrics{ metrics=[metric] }
      resource_metrics{ scope_metrics=[scope_metrics] }
      ExportMetricsServiceRequest{ resource_metrics=[resource_metrics] }
    """

    def varint(n: int) -> bytes:
        buf = b""
        while True:
            b = n & 0x7F
            n >>= 7
            buf += bytes([b | (0x80 if n else 0)])
            if not n:
                break
        return buf

    def tag(field: int, wire: int) -> bytes:
        return varint((field << 3) | wire)

    def ldelim(field: int, data: bytes) -> bytes:
        return tag(field, 2) + varint(len(data)) + data

    def string_field(field: int, s: str) -> bytes:
        return ldelim(field, s.encode())

    def fixed64_field(field: int, value: int) -> bytes:
        return tag(field, 1) + struct.pack("<Q", value)

    def double_field(field: int, value: float) -> bytes:
        return tag(field, 1) + struct.pack("<d", value)

    # AnyValue { string_value = email }
    any_value = string_field(1, email)

    # KeyValue { key="user.email", value=AnyValue }
    kv = string_field(1, "user.email") + ldelim(2, any_value)

    # NumberDataPoint { attributes=[kv], time_unix_nano=ts_ns, as_double=1.0 }
    datapoint = (
        ldelim(1, kv)                     # attributes (field 1)
        + fixed64_field(3, ts_ns)          # time_unix_nano (field 3)
        + double_field(4, 1.0)             # as_double (field 4)
    )

    # Gauge { data_points=[datapoint] }
    gauge = ldelim(1, datapoint)

    # Metric { name="claude_code_daemon_heartbeat", gauge=gauge }
    metric = string_field(1, "claude_code_daemon_heartbeat") + ldelim(5, gauge)

    # ScopeMetrics { metrics=[metric] }
    scope_metrics = ldelim(3, metric)

    # ResourceMetrics { scope_metrics=[scope_metrics] }
    resource_metrics = ldelim(2, scope_metrics)

    # ExportMetricsServiceRequest { resource_metrics=[resource_metrics] }
    return ldelim(1, resource_metrics)


def _send_heartbeat(email: str) -> None:
    ts_ns = time.time_ns()
    payload = _build_otlp_heartbeat(email, ts_ns)
    req = urllib.request.Request(
        OTLP_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-protobuf"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("heartbeat sent for %s status=%d", email, resp.status)
    except Exception as e:
        log.warning("heartbeat send failed: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run_loop() -> None:
    log.info("daemon started pid=%d profile=%s", os.getpid(), _profile())
    cred_check_file = CACHE_DIR / f"{_profile()}-cred-check"
    last_check = 0.0

    # Ensure otelcol is running at startup
    if not _otelcol_running() or not _otelcol_healthy():
        log.info("otelcol not running at startup, starting")
        _start_otelcol()
        # Wait up to 15s for healthy
        for _ in range(15):
            time.sleep(1)
            if _otelcol_healthy():
                log.info("otelcol healthy")
                break
        else:
            log.warning("otelcol did not become healthy within 15s")

    while True:
        now = time.time()

        # Periodic credential + health check
        if now - last_check >= INTERVAL:
            last_check = now
            cred_check_file.write_text(str(int(now)))

            if not _otelcol_running() or not _otelcol_healthy():
                log.info("otelcol unhealthy, restarting")
                _stop_otelcol()
                _start_otelcol()
            elif not _sts_valid():
                log.info("STS check failed, refreshing credentials and restarting otelcol")
                cp = _credential_process()
                profile = _profile()
                try:
                    subprocess.run(
                        [str(cp), "--profile", profile, "--clear-cache"],
                        capture_output=True, timeout=15,
                    )
                except Exception as e:
                    log.warning("clear-cache failed: %s", e)
                _stop_otelcol()
                _start_otelcol()

            # Send heartbeat
            email = _read_email()
            if email:
                _send_heartbeat(email)
            else:
                log.info("no email found, skipping heartbeat")

        time.sleep(10)


def _handle_signal(signum: int, _frame: object) -> None:
    log.info("daemon received signal %d, shutting down", signum)
    _stop_otelcol()
    DAEMON_PID_FILE.unlink(missing_ok=True)
    sys.exit(0)


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Write PID file
    DAEMON_PID_FILE.write_text(str(os.getpid()))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        _run_loop()
    except Exception as e:
        log.exception("daemon crashed: %s", e)
        DAEMON_PID_FILE.unlink(missing_ok=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
