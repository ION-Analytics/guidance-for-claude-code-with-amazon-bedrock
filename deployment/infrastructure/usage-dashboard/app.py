"""Bedrock usage dashboard — Flask app for internal ALB deployment."""

import os
import time
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key, Attr as DdbAttr
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LOG_GROUP = os.environ.get("INVOCATION_LOG_GROUP", "bedrock-model-invocation")
REGION = os.environ.get("AWS_REGION", "eu-west-1")
METRICS_TABLE = os.environ.get("USER_METRICS_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("QUOTA_POLICIES_TABLE", "QuotaPolicies")

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


def _run_cw_query(start_time, end_time):
    """Run the Bedrock usage CW Logs query and return {email: total_cost}."""
    logs = boto3.client("logs", region_name=REGION)
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
        return None, None, f"Query {result['status']}"
    costs = {}
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
        costs[email] = costs.get(email, 0.0) + usd
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
    return costs, rows, None


def run_query(prev_days=0):
    start_time, end_time, date_label = _day_window(prev_days)
    costs, rows, err = _run_cw_query(start_time, end_time)
    if err:
        return None, err, date_label

    user_totals = costs
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
    # Version lookback is longer — a user might not have sent a beat in the last 15 min
    version_window_start = now - timedelta(hours=24)

    # Collect all known users from both metric dimensions
    users = set()
    user_versions = {}  # email -> version string from ClientVersion metric
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

        # ClientVersion metric carries both UserEmail and Version dimensions.
        # Collect every (email, version) pair seen historically.
        version_candidates = []  # list of (email, version)
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(Namespace="ClaudeCode/Security", MetricName="ClientVersion"):
            for m in page.get("Metrics", []):
                dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
                email = dims.get("UserEmail", "").lower()
                version = dims.get("Version", "")
                if "@" in email and version:
                    version_candidates.append((email, version))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not users:
        return jsonify({})

    result = {}
    user_list = sorted(users)

    # Build GetMetricData queries:
    #   - 2 per user for heartbeat dots (CollectorHeartbeat + OtelcolHeartbeat)
    #   - 1 per (email, version) candidate to find the most recently active version
    queries = []
    # Map from query id -> (email, version) for version candidates
    version_query_map = {}

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

    for idx, (email, version) in enumerate(version_candidates):
        safe = email.replace("@", "_at_").replace(".", "_").replace("-", "_")[:50]
        # Sanitise version for use as a CW query Id (alphanumeric + underscore only)
        vsafe = version.replace(".", "_").replace("-", "_")[:20]
        qid = f"ver_{safe}_{vsafe}_{idx}"
        version_query_map[qid] = (email, version)
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "ClaudeCode/Security",
                    "MetricName": "ClientVersion",
                    "Dimensions": [
                        {"Name": "UserEmail", "Value": email},
                        {"Name": "Version", "Value": version},
                    ],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })

    # Split queries: heartbeat queries use 15-min window; version queries use 24h window
    hb_queries = [q for q in queries if q["Id"].startswith("cw_") or q["Id"].startswith("otlp_")]
    ver_queries = [q for q in queries if q["Id"].startswith("ver_")]

    try:
        BATCH = 500
        all_results = {}
        for i in range(0, len(hb_queries), BATCH):
            resp = cw.get_metric_data(
                MetricDataQueries=hb_queries[i:i + BATCH],
                StartTime=window_start,
                EndTime=now,
            )
            for r in resp.get("MetricDataResults", []):
                vals = r.get("Values", [])
                all_results[r["Id"]] = {"active": any(v > 0 for v in vals)}

        for i in range(0, len(ver_queries), BATCH):
            resp = cw.get_metric_data(
                MetricDataQueries=ver_queries[i:i + BATCH],
                StartTime=version_window_start,
                EndTime=now,
            )
            for r in resp.get("MetricDataResults", []):
                vals = r.get("Values", [])
                timestamps = r.get("Timestamps", [])
                all_results[r["Id"]] = {
                    "active": any(v > 0 for v in vals),
                    "latest_ts": max(timestamps) if timestamps else None,
                }
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Pick the best version per user: prefer any non-dev version; among ties pick
    # the most recent timestamp; among equal timestamps pick highest semver-ish string.
    for qid, (email, version) in version_query_map.items():
        r = all_results.get(qid, {})
        if not r.get("active"):
            continue
        ts = r.get("latest_ts")
        existing = user_versions.get(email)
        existing_ts = user_versions.get(email + "_ts")
        if existing is None:
            user_versions[email] = version
            user_versions[email + "_ts"] = ts
        else:
            # Prefer non-dev over dev
            existing_is_dev = existing == "dev"
            candidate_is_dev = version == "dev"
            if existing_is_dev and not candidate_is_dev:
                user_versions[email] = version
                user_versions[email + "_ts"] = ts
            elif not existing_is_dev and not candidate_is_dev:
                # Both real versions — pick most recent timestamp, then higher string
                if ts and (existing_ts is None or ts >= existing_ts):
                    if ts > existing_ts or version > existing:
                        user_versions[email] = version
                        user_versions[email + "_ts"] = ts

    for email in user_list:
        safe = email.replace("@", "_at_").replace(".", "_").replace("-", "_")[:60]
        result[email] = {
            "cw": all_results.get(f"cw_{safe}", {}).get("active", False),
            "otlp": all_results.get(f"otlp_{safe}", {}).get("active", False),
            "version": user_versions.get(email, ""),
        }

    return jsonify(result)


@app.route("/api/quotas")
def quotas():
    """Return per-user quota status (cost-based only).

    Daily and monthly costs come from DynamoDB UserQuotaMetrics.
    Limits and enforcement modes come from DynamoDB QuotaPolicies.
    """
    ddb = boto3.resource("dynamodb", region_name=REGION)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Usage from DynamoDB — filter to current-month USER# items only to avoid
    # ALERTS items (which also carry an email attribute) overwriting real values
    metrics_tbl = ddb.Table(METRICS_TABLE)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    today_costs, monthly_costs = {}, {}
    try:
        scan_kwargs = dict(
            FilterExpression=DdbAttr("sk").eq(f"MONTH#{current_month}") & DdbAttr("pk").begins_with("USER#"),
            ProjectionExpression="#em, total_cost, daily_cost, daily_date",
            ExpressionAttributeNames={"#em": "email"},
        )
        resp = metrics_tbl.scan(**scan_kwargs)
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = metrics_tbl.scan(**scan_kwargs, ExclusiveStartKey=resp["LastEvaluatedKey"])
            items += resp.get("Items", [])
        for item in items:
            email = str(item.get("email", "")).lower()
            if not email:
                continue
            monthly_costs[email] = float(item.get("total_cost", 0))
            today_costs[email] = float(item.get("daily_cost", 0)) if str(item.get("daily_date", "")) == today else 0.0
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Policies from DynamoDB
    policies_tbl = ddb.Table(POLICIES_TABLE)
    user_policies, default_policy = {}, None
    try:
        resp = policies_tbl.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = policies_tbl.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items += resp.get("Items", [])
        for item in items:
            pt = item.get("policy_type")
            ident = str(item.get("identifier", "")).lower()
            if item.get("enabled") is False:
                continue
            p = {
                "monthly_cost_limit": float(item["monthly_cost_limit"]) if item.get("monthly_cost_limit") else None,
                "daily_cost_limit": float(item["daily_cost_limit"]) if item.get("daily_cost_limit") else None,
                "monthly_enforcement": item.get("monthly_enforcement_mode", item.get("enforcement_mode", "alert")),
                "daily_enforcement": item.get("daily_enforcement_mode", "alert"),
            }
            if pt == "user":
                user_policies[ident] = p
            elif pt == "default":
                default_policy = p
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def resolve_policy(email):
        if email in user_policies:
            return user_policies[email], "ind"
        return default_policy, "def"

    out = {}
    all_emails = set(today_costs) | set(monthly_costs)
    for email in all_emails:
        policy, policy_source = resolve_policy(email)
        if not policy:
            continue
        result = {"policy_source": policy_source}

        dcl = policy.get("daily_cost_limit") or 0
        if dcl > 0:
            dc = today_costs.get(email, 0.0)
            result["daily_pct"] = round(min((dc / dcl) * 100, 999), 1)
            result["daily_label"] = f"${dc:.2f}/${dcl:.0f}"
        else:
            result["daily_pct"] = None
            result["daily_label"] = None
        result["daily_enforcement"] = policy.get("daily_enforcement", "alert")

        mcl = policy.get("monthly_cost_limit") or 0
        if mcl > 0:
            mc = monthly_costs.get(email, 0.0)
            result["monthly_pct"] = round(min((mc / mcl) * 100, 999), 1)
            result["monthly_label"] = f"${mc:.2f}/${mcl:.0f}"
        else:
            result["monthly_pct"] = None
            result["monthly_label"] = None
        result["monthly_enforcement"] = policy.get("monthly_enforcement", "alert")

        out[email] = result

    return jsonify(out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
