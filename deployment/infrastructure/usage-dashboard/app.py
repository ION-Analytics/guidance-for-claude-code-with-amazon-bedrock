"""Bedrock usage dashboard — Flask app for internal ALB deployment."""

import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LOG_GROUP = os.environ.get("INVOCATION_LOG_GROUP", "bedrock-model-invocation")
REGION = os.environ.get("AWS_REGION", "eu-west-1")

QUERY = """
fields identity.arn, modelId,
       input.inputTokenCount,
       input.cacheReadInputTokenCount,
       input.cacheWriteInputTokenCount,
       output.outputTokenCount
| filter ispresent(identity.arn)
| stats
    sum(input.inputTokenCount) as input_tokens,
    sum(input.cacheReadInputTokenCount) as cache_read,
    sum(input.cacheWriteInputTokenCount) as cache_write,
    sum(output.outputTokenCount) as output_tokens,
    count() as calls
  by identity.arn, modelId
"""

MODEL_PRICING = {
    "opus":   (5.00, 25.00, 0.50, 6.25),
    "sonnet": (3.00, 15.00, 0.30, 3.75),
    "haiku":  (1.00,  5.00, 0.10, 1.25),
}


def _get_pricing(model_id):
    m = (model_id or "").lower()
    if "opus" in m:
        return MODEL_PRICING["opus"]
    if "haiku" in m:
        return MODEL_PRICING["haiku"]
    return MODEL_PRICING["sonnet"]


def _calc_cost(inp, out, cache_r, cache_w, model_id):
    pi, po, pr, pw = _get_pricing(model_id)
    return (
        inp * pi / 1_000_000
        + out * po / 1_000_000
        + cache_r * pr / 1_000_000
        + cache_w * pw / 1_000_000
    )


def _short_model(model_id):
    m = model_id.split("/")[-1].split(".")[-1].lower()
    for name in ("opus", "sonnet", "haiku"):
        if name in m:
            parts = m.split("-")
            try:
                idx = parts.index(name)
                major = parts[idx + 1] if idx + 1 < len(parts) else ""
                minor = parts[idx + 2] if idx + 2 < len(parts) else ""
                if major.isdigit() and minor.isdigit():
                    ver = f"{major}.{minor}"
                elif major.isdigit():
                    ver = major
                else:
                    ver = ""
                label = name.capitalize()
                return f"{label} {ver}" if ver else label
            except (ValueError, IndexError):
                return name.capitalize()
    return m[:12]


def _day_window(prev_days):
    now = datetime.now(timezone.utc)
    if prev_days == 0:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp()), int(now.timestamp()), now.strftime("%Y-%m-%d")
    target = (now - timedelta(days=prev_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(target.timestamp()), int((target + timedelta(days=1)).timestamp()), target.strftime("%Y-%m-%d")


def run_query(prev_days=0):
    logs = boto3.client("logs", region_name=REGION)
    start_time, end_time, date_label = _day_window(prev_days)

    response = logs.start_query(
        logGroupName=LOG_GROUP,
        startTime=start_time,
        endTime=end_time,
        queryString=QUERY,
    )
    query_id = response["queryId"]

    for _ in range(60):
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        return None, f"Query {result['status']}", date_label

    rows = []
    for row in result.get("results", []):
        f = {item["field"]: item["value"] for item in row}
        arn = f.get("identity.arn", "")
        email = arn.split("/")[-1].lower() if "@" in arn else arn
        model_id = f.get("modelId", "")
        inp = float(f.get("input_tokens", 0))
        out = float(f.get("output_tokens", 0))
        cache_r = float(f.get("cache_read", 0))
        cache_w = float(f.get("cache_write", 0))
        calls = int(f.get("calls", 0))
        usd = _calc_cost(inp, out, cache_r, cache_w, model_id)
        rows.append({
            "email": email,
            "model": _short_model(model_id),
            "calls": calls,
            "input_tokens": int(inp),
            "output_tokens": int(out),
            "cache_read": int(cache_r),
            "cache_write": int(cache_w),
            "cost": round(usd, 4),
        })

    user_totals = {}
    for r in rows:
        user_totals[r["email"]] = user_totals.get(r["email"], 0.0) + r["cost"]

    for r in rows:
        r["user_total"] = round(user_totals[r["email"]], 4)

    rows.sort(key=lambda x: (-user_totals[x["email"]], x["email"], -x["cost"]))
    return rows, None, date_label


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/usage")
def usage():
    prev_days = abs(int(request.args.get("prev_days", 0)))
    rows, error, date_label = run_query(prev_days)
    queried_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if error:
        return jsonify({"error": error, "date_label": date_label, "queried_at": queried_at}), 500
    return jsonify({"rows": rows, "date_label": date_label, "queried_at": queried_at})


@app.route("/api/heartbeats")
def heartbeats():
    """Return heartbeat status for all users seen today.

    Queries two CloudWatch metrics over the last 15 minutes:
    - ClaudeCode/Security / CollectorHeartbeat  (daemon alive)
    - ClaudeCode/Security / OtelcolHeartbeat    (otelcol process running)

    Returns {email: {cw: bool, otlp: bool}} for every user that has ever sent
    either heartbeat (ListMetrics determines the known set).
    """
    cw = boto3.client("cloudwatch", region_name=REGION)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)

    # Collect all known users from both metric dimensions
    users = set()
    try:
        for namespace, metric_name, dim_name in [
            ("ClaudeCode/Security", "CollectorHeartbeat", "UserEmail"),
            ("ClaudeCode/Security", "OtelcolHeartbeat", "UserEmail"),
        ]:
            paginator = cw.get_paginator("list_metrics")
            for page in paginator.paginate(Namespace=namespace, MetricName=metric_name):
                for m in page.get("Metrics", []):
                    for d in m.get("Dimensions", []):
                        if d["Name"] == dim_name and "@" in d["Value"]:
                            users.add(d["Value"].lower())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not users:
        return jsonify({})

    # Batch GetMetricData — max 500 queries per call; each user needs 2 queries
    result = {}
    user_list = sorted(users)

    # Build metric data queries (2 per user: daemon + otelcol)
    queries = []
    for email in user_list:
        safe = email.replace("@", "_at_").replace(".", "_").replace("-", "_")[:60]
        queries.append({
            "Id": f"cw_{safe}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "ClaudeCode/Security",
                    "MetricName": "CollectorHeartbeat",
                    "Dimensions": [{"Name": "UserEmail", "Value": email}],
                },
                "Period": 900,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })
        queries.append({
            "Id": f"otlp_{safe}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "ClaudeCode/Security",
                    "MetricName": "OtelcolHeartbeat",
                    "Dimensions": [{"Name": "UserEmail", "Value": email}],
                },
                "Period": 900,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })

    # GetMetricData accepts max 500 queries per call
    try:
        BATCH = 500
        all_results = {}
        for i in range(0, len(queries), BATCH):
            resp = cw.get_metric_data(
                MetricDataQueries=queries[i:i + BATCH],
                StartTime=window_start,
                EndTime=now,
            )
            for r in resp.get("MetricDataResults", []):
                all_results[r["Id"]] = any(v > 0 for v in r.get("Values", []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for email in user_list:
        safe = email.replace("@", "_at_").replace(".", "_").replace("-", "_")[:60]
        result[email] = {
            "cw": all_results.get(f"cw_{safe}", False),
            "otlp": all_results.get(f"otlp_{safe}", False),
        }

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
