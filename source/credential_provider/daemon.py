"""
Credential daemon — manages otelcol lifecycle and emits a heartbeat metric.

Spawned by credential-process if not already running. Runs as a detached
background process; owns the otelcol PID file and credential refresh cycle.

Heartbeat metric: sends `claude_code.daemon.heartbeat` (value=1) to the local
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

import configparser

import boto3

# Interval between health checks and heartbeat emissions (seconds)
INTERVAL = 300  # 5 minutes

INSTALL_DIR = Path.home() / "claude-code-with-bedrock"
CACHE_DIR = Path.home() / ".claude-code-session"
DAEMON_PID_FILE = INSTALL_DIR / "daemon.pid"
COLLECTOR_PID_FILE = INSTALL_DIR / "collector.pid"
LOG_FILE = CACHE_DIR / "daemon.log"
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



def _collector_credentials_valid() -> bool:
    """Check if collector profile creds in ~/.aws/credentials are still valid."""
    import configparser
    profile = _profile()
    collector_profile = f"{profile}-collector"
    creds_file = Path.home() / ".aws" / "credentials"
    try:
        config = configparser.ConfigParser(inline_comment_prefixes=())
        config.read(creds_file)
        if collector_profile not in config:
            return False
        exp = config[collector_profile].get("x-expiration")
        if not exp:
            return False
        exp_time = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        return (exp_time - datetime.now(timezone.utc)).total_seconds() > 30
    except Exception:
        return False


def _sts_valid() -> bool:
    """Check both main and collector credentials are still valid."""
    return _credentials_cached() and _collector_credentials_valid()


def _credentials_cached() -> bool:
    """Check if valid credentials are already in the cache file — avoids calling
    credential-process recursively when the daemon is itself spawned by it."""
    profile = _profile()
    creds_file = Path.home() / ".aws" / "credentials"
    try:
        config = configparser.ConfigParser(inline_comment_prefixes=())
        config.read(creds_file)
        if profile not in config:
            return False
        exp = config[profile].get("x-expiration")
        if not exp:
            return False
        exp_time = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        return (exp_time - datetime.now(timezone.utc)).total_seconds() > 30
    except Exception:
        return False


def _write_collector_credentials() -> None:
    """Mirror main profile credentials into the collector profile in ~/.aws/credentials.

    otelcol reads static creds directly from this file — no credential_process
    subprocess needed, avoiding PyInstaller temp dir issues.
    """
    import configparser
    import tempfile

    profile = _profile()
    collector_profile = f"{profile}-collector"
    creds_file = Path.home() / ".aws" / "credentials"

    try:
        config = configparser.ConfigParser(inline_comment_prefixes=())
        config.read(creds_file)

        if profile not in config:
            log.warning("no credentials found for profile %s, cannot write collector creds", profile)
            return

        src = config[profile]
        if src.get("aws_access_key_id") == "EXPIRED":
            log.warning("credentials for %s are expired, skipping collector write", profile)
            return

        config[collector_profile] = {
            "aws_access_key_id": src.get("aws_access_key_id", ""),
            "aws_secret_access_key": src.get("aws_secret_access_key", ""),
            "aws_session_token": src.get("aws_session_token", ""),
        }
        if src.get("x-expiration"):
            config[collector_profile]["x-expiration"] = src["x-expiration"]

        creds_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=creds_file.parent, prefix=".credentials.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                config.write(f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, creds_file)
            log.info("wrote collector credentials for %s", collector_profile)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log.warning("failed to write collector credentials: %s", e)


def _start_otelcol() -> None:
    config = INSTALL_DIR / "collector-config.yaml"
    otelcol = INSTALL_DIR / "otelcol"
    if not otelcol.exists() or not config.exists():
        log.info("otelcol binary or config not found, skipping")
        return

    profile = _profile()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not _credentials_cached():
        log.warning("no cached credentials found, deferring otelcol start until credentials available")
        return
    _write_collector_credentials()

    env = {k: v for k, v in os.environ.items()
           if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                        "AWS_SESSION_TOKEN", "AWS_SESSION_EXPIRATION",
                        "AWS_CREDENTIAL_EXPIRATION")}
    env["AWS_PROFILE"] = f"{profile}-collector"

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
    """Build a minimal OTLP protobuf payload for claude_code.daemon.heartbeat.

    Encodes the ExportMetricsServiceRequest manually using raw protobuf
    encoding (field tag + wire type + value) to avoid a protobuf dependency.

    Schema:
      gauge{ datapoints=[{ as_double=1.0, time_unix_nano=ts_ns,
               attributes=[{key="user.email", value=email}] }] }
      metric{ name="claude_code.daemon.heartbeat", gauge=... }
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

    # Metric { name="claude_code.daemon.heartbeat", gauge=gauge }
    metric = string_field(1, "claude_code.daemon.heartbeat") + ldelim(5, gauge)

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

    # Also send directly to CloudWatch via boto3 — bypasses the local otelcol
    # entirely so a user disabling otelcol cannot suppress this signal.
    # The quota Lambda correlates this against otelcol token usage to detect
    # collector bypass.
    _send_cloudwatch_heartbeat(email)


def _send_cloudwatch_heartbeat(email: str) -> None:
    """Send heartbeat directly to CloudWatch via SigV4-signed HTTP, bypassing local otelcol.

    Uses urllib + manual SigV4 signing (no botocore data files required) so this
    works correctly inside a PyInstaller bundle. Credentials read directly from
    ~/.aws/credentials which the daemon keeps fresh.
    Namespace: ClaudeCode/Security, queryable separately from OTel metrics.
    """
    import hashlib
    import hmac
    import urllib.parse

    try:
        profile = _profile()
        region = os.environ.get("AWS_REGION", "eu-west-1")

        creds_file = Path.home() / ".aws" / "credentials"
        config = configparser.ConfigParser(inline_comment_prefixes=())
        config.read(creds_file)
        if profile not in config:
            log.warning("no credentials for direct CW heartbeat")
            return

        creds = config[profile]
        access_key = creds.get("aws_access_key_id", "")
        secret_key = creds.get("aws_secret_access_key", "")
        session_token = creds.get("aws_session_token", "")
        if not access_key or access_key == "EXPIRED":
            log.warning("credentials expired, skipping direct CW heartbeat")
            return

        # Build PutMetricData request body
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = f"monitoring.{region}.amazonaws.com"
        endpoint = f"https://{host}/"

        body = urllib.parse.urlencode({
            "Action": "PutMetricData",
            "Version": "2010-08-01",
            "Namespace": "ClaudeCode/Security",
            "MetricData.member.1.MetricName": "CollectorHeartbeat",
            "MetricData.member.1.Value": "1.0",
            "MetricData.member.1.Unit": "Count",
            "MetricData.member.1.Dimensions.member.1.Name": "UserEmail",
            "MetricData.member.1.Dimensions.member.1.Value": email,
        })

        # SigV4 signing
        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        def get_signature_key(key: str, date: str, region: str, service: str) -> bytes:
            k_date = sign(("AWS4" + key).encode("utf-8"), date)
            k_region = sign(k_date, region)
            k_service = sign(k_region, service)
            return sign(k_service, "aws4_request")

        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        headers_to_sign = {
            "content-type": "application/x-www-form-urlencoded",
            "host": host,
            "x-amz-date": amz_date,
        }
        if session_token:
            headers_to_sign["x-amz-security-token"] = session_token

        canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))
        signed_headers = ";".join(sorted(headers_to_sign.keys()))
        canonical_request = "\n".join([
            "POST", "/", "",
            canonical_headers, signed_headers, payload_hash,
        ])

        credential_scope = f"{date_stamp}/{region}/monitoring/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        signing_key = get_signature_key(secret_key, date_stamp, region, "monitoring")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        req_headers = {**headers_to_sign, "Authorization": auth_header}
        req = urllib.request.Request(
            endpoint,
            data=body.encode("utf-8"),
            headers=req_headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("direct CW heartbeat sent for %s status=%d", email, resp.status)

    except Exception as e:
        log.warning("direct CW heartbeat failed: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run_loop() -> None:
    log.info("daemon started pid=%d profile=%s", os.getpid(), _profile())
    cred_check_file = CACHE_DIR / f"{_profile()}-cred-check"
    last_check = 0.0

    # Ensure otelcol is running at startup
    if not _otelcol_running():
        log.info("otelcol not running at startup, starting")
        _start_otelcol()
        time.sleep(3)  # brief pause for otelcol to initialise

    while True:
        now = time.time()

        # Start otelcol as soon as credentials become available (every tick)
        if not _otelcol_running() and _credentials_cached():
            _start_otelcol()

        # Periodic credential + health check
        if now - last_check >= INTERVAL:
            last_check = now
            cred_check_file.write_text(str(int(now)))

            if not _otelcol_running():
                log.info("otelcol not running, attempting start")
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
                _write_collector_credentials()
                _stop_otelcol()
                _start_otelcol()
            else:
                # Credentials still valid — keep collector profile in sync
                _write_collector_credentials()

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
