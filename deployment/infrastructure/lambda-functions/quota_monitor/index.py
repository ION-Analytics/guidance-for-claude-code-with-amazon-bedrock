# ABOUTME: Lambda function that monitors user token quotas and sends SNS alerts
# ABOUTME: Queries Bedrock model invocation logs for usage data, writes to DynamoDB, checks thresholds

import json
import boto3
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from boto3.dynamodb.conditions import Key, Attr

# Initialize clients
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

# Configuration from environment
QUOTA_TABLE = os.environ.get("QUOTA_TABLE", "UserQuotaMetrics")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
ENABLE_FINEGRAINED_QUOTAS = os.environ.get("ENABLE_FINEGRAINED_QUOTAS", "false").lower() == "true"
METRICS_REGION = os.environ.get("METRICS_REGION", os.environ.get("AWS_REGION", "us-east-1"))

# Default limits
MONTHLY_TOKEN_LIMIT = int(os.environ.get("MONTHLY_TOKEN_LIMIT", "300000000"))
WARNING_THRESHOLD_80 = int(os.environ.get("WARNING_THRESHOLD_80", "240000000"))
WARNING_THRESHOLD_90 = int(os.environ.get("WARNING_THRESHOLD_90", "270000000"))

# Bedrock EU cross-region inference pricing (USD per million tokens)
# Source: https://aws.amazon.com/bedrock/pricing/ — Europe (Ireland), Global Cross-region Inference
# Format: (input, output, cache_read, cache_write_5m)
MODEL_PRICING = {
    "opus":   ( 5.00, 25.00, 0.50,  6.25),
    "sonnet": ( 3.00, 15.00, 0.30,  3.75),
    "haiku":  ( 1.00,  5.00, 0.10,  1.25),
}


def _get_pricing(model_id):
    m = (model_id or "").lower()
    if "opus" in m:
        return MODEL_PRICING["opus"]
    if "haiku" in m:
        return MODEL_PRICING["haiku"]
    return MODEL_PRICING["sonnet"]

# DynamoDB tables
quota_table = dynamodb.Table(QUOTA_TABLE)
policies_table = dynamodb.Table(POLICIES_TABLE) if POLICIES_TABLE else None

INVOCATION_LOG_GROUP = os.environ.get("INVOCATION_LOG_GROUP", "bedrock-model-invocation")
AGGREGATION_WINDOW = 900  # 15 minutes in seconds (matches EventBridge schedule)


def fetch_usage_from_invocation_logs():
    """Query CloudWatch Logs Insights against Bedrock model invocation logs for the last 15 minutes.

    Captures all clients (CLI, VS Code, desktop app, Cowork) regardless of credential path,
    as long as the IAM session name is the user email (true for both BedrockAzureFederatedRole
    and SSO sessions at ION Analytics).
    """
    logs = boto3.client("logs")
    now = datetime.now(timezone.utc)
    end_time = int(now.timestamp())
    start_time = end_time - AGGREGATION_WINDOW

    query = """
fields identity.arn, modelId,
       input.inputTokenCount,
       input.cacheReadInputTokenCount,
       input.cacheWriteInputTokenCount,
       output.outputTokenCount
| filter ispresent(identity.arn)
| stats
    sum(input.inputTokenCount) as input_tokens,
    sum(input.cacheReadInputTokenCount) as cache_read_tokens,
    sum(input.cacheWriteInputTokenCount) as cache_write_tokens,
    sum(output.outputTokenCount) as output_tokens
  by identity.arn, modelId
"""

    response = logs.start_query(
        logGroupName=INVOCATION_LOG_GROUP,
        startTime=start_time,
        endTime=end_time,
        queryString=query,
    )
    query_id = response["queryId"]

    # Poll until complete (typically 2-5 seconds)
    for _ in range(30):
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(1)

    if result["status"] != "Complete":
        raise RuntimeError(f"CloudWatch Logs Insights query {result['status']}")

    # Aggregate per user across models, applying correct pricing per model
    users = {}
    for row in result.get("results", []):
        fields = {f["field"]: f["value"] for f in row}
        arn = fields.get("identity.arn", "")
        if not arn:
            continue
        email = arn.split("/")[-1].lower()
        if "@" not in email:
            continue
        model_id = fields.get("modelId", "")
        inp        = float(fields.get("input_tokens", 0))
        cache_read = float(fields.get("cache_read_tokens", 0))
        cache_write = float(fields.get("cache_write_tokens", 0))
        out        = float(fields.get("output_tokens", 0))
        total      = inp + cache_read + cache_write + out
        if total <= 0:
            continue
        pi, po, pr, pw = _get_pricing(model_id)
        cost_delta = (
            inp        * pi / 1_000_000 +
            out        * po / 1_000_000 +
            cache_read * pr / 1_000_000 +
            cache_write * pw / 1_000_000
        )
        u = users.setdefault(email, {
            "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_tokens": 0, "cache_write_tokens": 0, "cost": 0.0,
        })
        u["total_tokens"]      += total
        u["input_tokens"]      += inp
        u["output_tokens"]     += out
        u["cache_tokens"]      += cache_read
        u["cache_write_tokens"] += cache_write
        u["cost"]              += cost_delta

    print(f"Fetched delta usage for {len(users)} users from Bedrock invocation logs ({AGGREGATION_WINDOW}s window)")
    return users


def calculate_cost(input_tokens, output_tokens, cache_tokens, cache_write_tokens=0):
    """Calculate USD cost from token counts using configured per-million prices."""
    return (
        (input_tokens * PRICE_INPUT_PER_M / 1_000_000) +
        (output_tokens * PRICE_OUTPUT_PER_M / 1_000_000) +
        (cache_tokens * PRICE_CACHE_READ_PER_M / 1_000_000) +
        (cache_write_tokens * PRICE_CACHE_WRITE_PER_M / 1_000_000)
    )


def update_quota_metrics(usage_data):
    """Atomically increment UserQuotaMetrics with delta from PromQL (like old MetricsAggregator)."""
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    current_date = now.strftime("%Y-%m-%d")
    ttl = int((now.replace(day=28) + __import__("datetime").timedelta(days=32)).replace(day=1).timestamp())

    for email, usage in usage_data.items():
        delta = usage.get("total_tokens", 0)
        if delta <= 0:
            continue
        try:
            inp = int(usage.get("input_tokens", 0))
            out = int(usage.get("output_tokens", 0))
            cache = int(usage.get("cache_tokens", 0))
            cache_write = int(usage.get("cache_write_tokens", 0))
            cost_delta = usage.get("cost") or calculate_cost(inp, out, cache, cache_write)

            # Check if daily_date changed (new day = reset daily counter)
            response = quota_table.get_item(Key={"pk": f"USER#{email}", "sk": f"MONTH#{current_month}"})
            existing = response.get("Item", {})
            daily_reset = existing.get("daily_date") != current_date

            update_expr = "ADD total_tokens :delta, input_tokens :inp, output_tokens :out, cache_tokens :cache, total_cost :cost"
            if daily_reset:
                update_expr += " SET daily_tokens = :delta, daily_cost = :cost, daily_date = :date, last_updated = :ts, #ttl = :ttl, email = :email"
            else:
                # Adding 'daily_date = :date' here satisfies the DynamoDB validator constraint safely
                update_expr += ", daily_tokens :delta, daily_cost :cost SET daily_date = :date, last_updated = :ts, #ttl = :ttl, email = :email"

            quota_table.update_item(
                Key={"pk": f"USER#{email}", "sk": f"MONTH#{current_month}"},
                UpdateExpression=update_expr,
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":delta": Decimal(str(int(delta))),
                    ":inp": Decimal(str(inp)),
                    ":out": Decimal(str(out)),
                    ":cache": Decimal(str(cache)),
                    ":cost": Decimal(str(round(cost_delta, 6))),
                    ":date": current_date,
                    ":ts": now.isoformat().replace("+00:00", "Z"),
                    ":ttl": ttl,
                    ":email": email,
                },
            )
        except Exception as e:
            print(f"Error updating quota for {email}: {e}")

    print(f"Updated UserQuotaMetrics for {len(usage_data)} users")


def publish_cost_metrics(usage_data):
    """Publish per-user monthly and daily cost to CloudWatch for dashboard visibility.

    Namespace: ClaudeCode/Usage
    Metrics: UserMonthlyCost, UserDailyCost
    Dimension: UserEmail
    """
    if not usage_data:
        return

    cw = boto3.client("cloudwatch")
    metric_data = []

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for email, usage in usage_data.items():
        total_cost = usage.get("total_cost", 0)
        daily_cost = usage.get("daily_cost", 0)
        # If the usage record is from a previous day, treat daily cost as 0
        if usage.get("daily_date") and usage["daily_date"] != current_date:
            daily_cost = 0
        if total_cost <= 0 and daily_cost <= 0:
            continue
        dimensions = [{"Name": "UserEmail", "Value": email.lower()}]
        if total_cost > 0:
            metric_data.append({
                "MetricName": "UserMonthlyCost",
                "Dimensions": dimensions,
                "Value": round(total_cost, 4),
                "Unit": "None",
            })
        # Always publish daily cost (including 0) so stale values are overwritten
        metric_data.append({
            "MetricName": "UserDailyCost",
            "Dimensions": dimensions,
            "Value": round(daily_cost, 4),
            "Unit": "None",
        })

    # CloudWatch PutMetricData accepts max 1000 metrics per call
    for i in range(0, len(metric_data), 1000):
        try:
            cw.put_metric_data(
                Namespace="ClaudeCode/Usage",
                MetricData=metric_data[i:i+1000],
            )
        except Exception as e:
            print(f"Error publishing cost metrics to CloudWatch: {e}")

    print(f"Published cost metrics for {len(usage_data)} users to CloudWatch/Usage")


def lambda_handler(event, context):
    """Fetch usage from PromQL, update DynamoDB, check quotas, send alerts."""
    print(f"Starting quota monitoring at {datetime.now(timezone.utc).isoformat()}")

    now = datetime.now(timezone.utc)
    month_name = now.strftime("%B %Y")
    current_date = now.strftime("%Y-%m-%d")
    days_in_month = (31 if now.month in [1, 3, 5, 7, 8, 10, 12]
                     else (30 if now.month != 2 else (29 if now.year % 4 == 0 else 28)))
    days_remaining = days_in_month - now.day

    try:
        # Step 1: Fetch delta usage from Bedrock invocation logs and increment DynamoDB
        delta_data = fetch_usage_from_invocation_logs()
        if delta_data:
            update_quota_metrics(delta_data)

        # Step 2: Read cumulative totals from DynamoDB for threshold checking
        current_month = now.strftime("%Y-%m")
        usage_data = {}
        response = quota_table.scan(
            FilterExpression=Attr("sk").eq(f"MONTH#{current_month}") & Attr("pk").begins_with("USER#"),
            ProjectionExpression="email, total_tokens, daily_tokens, total_cost, daily_cost, daily_date",
        )
        for item in response.get("Items", []):
            email = item.get("email")
            if email:
                usage_data[email] = {
                    "total_tokens": float(item.get("total_tokens", 0)),
                    "daily_tokens": float(item.get("daily_tokens", 0)),
                    "total_cost": float(item.get("total_cost", 0)),
                    "daily_cost": float(item.get("daily_cost", 0)),
                    "daily_date": item.get("daily_date"),
                }
        while "LastEvaluatedKey" in response:
            response = quota_table.scan(
                FilterExpression=Attr("sk").eq(f"MONTH#{current_month}") & Attr("pk").begins_with("USER#"),
                ProjectionExpression="email, total_tokens, daily_tokens, total_cost, daily_cost, daily_date",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                email = item.get("email")
                if email:
                    usage_data[email] = {
                        "total_tokens": float(item.get("total_tokens", 0)),
                        "daily_tokens": float(item.get("daily_tokens", 0)),
                        "total_cost": float(item.get("total_cost", 0)),
                        "daily_cost": float(item.get("daily_cost", 0)),
                        "daily_date": item.get("daily_date"),
                    }

        if not usage_data:
            print("No usage data in DynamoDB")
            return {"statusCode": 200, "body": "No usage data"}

        # Step 3: Load policies
        policies_cache = {}
        if ENABLE_FINEGRAINED_QUOTAS and policies_table:
            policies_cache = load_all_policies()

        # Step 3: Check sent alerts
        sent_alerts = get_sent_alerts(month_name)

        # Step 4: Check each user against quotas
        alerts_to_send = []
        stats = {"total_users": 0, "over_80": 0, "over_90": 0, "exceeded": 0, "daily_exceeded": 0}

        for email, usage in usage_data.items():
            stats["total_users"] += 1
            policy = resolve_user_quota(email, [], policies_cache)
            if policy is None:
                continue

            total_tokens = usage.get("total_tokens", 0)
            daily_tokens = usage.get("daily_tokens", 0)
            total_cost = usage.get("total_cost", 0.0)
            daily_cost = usage.get("daily_cost", 0.0)

            alerts = check_limits_and_generate_alerts(
                email=email, total_tokens=total_tokens, daily_tokens=daily_tokens,
                total_cost=total_cost, daily_cost=daily_cost,
                policy=policy, month_name=month_name, current_date=current_date,
                days_remaining=days_remaining, days_in_month=days_in_month, sent_alerts=sent_alerts,
            )

            monthly_cost_limit = policy.get("monthly_cost_limit") or 0
            if monthly_cost_limit > 0:
                cost_pct = (total_cost / monthly_cost_limit) * 100
                if cost_pct > 100:
                    stats["exceeded"] += 1
                elif cost_pct > 90:
                    stats["over_90"] += 1
                elif cost_pct > 80:
                    stats["over_80"] += 1
            elif policy["monthly_token_limit"] > 0:
                monthly_pct = (total_tokens / policy["monthly_token_limit"]) * 100
                if monthly_pct > 100:
                    stats["exceeded"] += 1
                elif monthly_pct > 90:
                    stats["over_90"] += 1
                elif monthly_pct > 80:
                    stats["over_80"] += 1
            if policy.get("daily_cost_limit") and daily_cost > policy["daily_cost_limit"]:
                stats["daily_exceeded"] += 1
            elif policy.get("daily_token_limit") and daily_tokens > policy["daily_token_limit"]:
                stats["daily_exceeded"] += 1

            for alert in alerts:
                alert_key = f"{email}#{alert['alert_type']}#{alert['alert_level']}"
                if alert_key not in sent_alerts:
                    alerts_to_send.append(alert)
                    record_sent_alert(month_name, email, alert["alert_type"], alert["alert_level"], alert)

        if alerts_to_send:
            send_alerts(alerts_to_send)
            print(f"Sent {len(alerts_to_send)} alerts")

        # Publish per-user cost metrics to CloudWatch for dashboard visibility
        publish_cost_metrics(usage_data)

        print(f"Summary - Total: {stats['total_users']}, Over 80%: {stats['over_80']}, Over 90%: {stats['over_90']}, Exceeded: {stats['exceeded']}")
        return {"statusCode": 200, "body": json.dumps(stats)}

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return {"statusCode": 500, "body": json.dumps(f"Error: {e}")}


def load_all_policies():
    """Load all quota policies from QuotaPolicies table."""
    policies = {}
    if not policies_table:
        return policies
    try:
        response = policies_table.scan(FilterExpression=Attr("sk").eq("CURRENT"))
        for item in response.get("Items", []):
            pt, ident = item.get("policy_type"), item.get("identifier")
            if pt and ident:
                policies[f"{pt}:{ident}"] = {
                    "policy_type": pt, "identifier": ident,
                    "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
                    "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
                    "warning_threshold_80": int(item.get("warning_threshold_80", 0)),
                    "warning_threshold_90": int(item.get("warning_threshold_90", 0)),
                    "enforcement_mode": item.get("enforcement_mode", "alert"),
                    "enabled": item.get("enabled", True),
                }
        while "LastEvaluatedKey" in response:
            response = policies_table.scan(FilterExpression=Attr("sk").eq("CURRENT"), ExclusiveStartKey=response["LastEvaluatedKey"])
            for item in response.get("Items", []):
                pt, ident = item.get("policy_type"), item.get("identifier")
                if pt and ident:
                    policies[f"{pt}:{ident}"] = {
                        "policy_type": pt, "identifier": ident,
                        "monthly_token_limit": int(item.get("monthly_token_limit", 0)),
                        "daily_token_limit": int(item.get("daily_token_limit", 0)) if item.get("daily_token_limit") else None,
                        "warning_threshold_80": int(item.get("warning_threshold_80", 0)),
                        "warning_threshold_90": int(item.get("warning_threshold_90", 0)),
                        "enforcement_mode": item.get("enforcement_mode", "alert"),
                        "enabled": item.get("enabled", True),
                    }
    except Exception as e:
        print(f"Error loading policies: {e}")
    return policies


def resolve_user_quota(email, groups, policies_cache):
    """Resolve effective quota policy: user > group > default > env defaults."""
    if not ENABLE_FINEGRAINED_QUOTAS:
        monthly_cost_limit = os.environ.get("MONTHLY_COST_LIMIT")
        daily_cost_limit = os.environ.get("DAILY_COST_LIMIT")
        return {
            "policy_type": "default", "identifier": "environment",
            "monthly_token_limit": MONTHLY_TOKEN_LIMIT, "daily_token_limit": None,
            "monthly_cost_limit": float(monthly_cost_limit) if monthly_cost_limit else None,
            "daily_cost_limit": float(daily_cost_limit) if daily_cost_limit else None,
            "warning_threshold_80": WARNING_THRESHOLD_80, "warning_threshold_90": WARNING_THRESHOLD_90,
            "enforcement_mode": "alert", "enabled": True,
        }
    user_key = f"user:{email}"
    if user_key in policies_cache and policies_cache[user_key].get("enabled"):
        return policies_cache[user_key]
    group_policies = [policies_cache[f"group:{g}"] for g in (groups or [])
                      if f"group:{g}" in policies_cache and policies_cache[f"group:{g}"].get("enabled")]
    if group_policies:
        return min(group_policies, key=lambda p: p["monthly_token_limit"])
    default_key = "default:default"
    if default_key in policies_cache and policies_cache[default_key].get("enabled"):
        return policies_cache[default_key]
    return None


def check_limits_and_generate_alerts(email, total_tokens, daily_tokens, total_cost, daily_cost,
                                     policy, month_name, current_date, days_remaining, days_in_month, sent_alerts):
    """Check limits and generate alert dicts. Cost limits take precedence over token limits."""
    alerts = []
    policy_info = f"{policy['policy_type']}:{policy['identifier']}"
    enforcement_mode = policy.get("enforcement_mode", "alert")
    monthly_cost_limit = policy.get("monthly_cost_limit") or 0
    daily_cost_limit = policy.get("daily_cost_limit") or 0

    # Monthly — prefer cost limit if configured, fall back to token limit
    if monthly_cost_limit > 0:
        pct = (total_cost / monthly_cost_limit) * 100
        level = None
        if total_cost >= monthly_cost_limit:
            level = "exceeded"
        elif pct >= 90:
            level = "critical"
        elif pct >= 80:
            level = "warning"
        if level and f"{email}#monthly_cost#{level}" not in sent_alerts:
            day_of_month = int(current_date.split("-")[2])
            daily_avg_cost = total_cost / max(1, day_of_month)
            projected_cost = daily_avg_cost * days_in_month
            alerts.append({
                "user": email, "alert_type": "monthly_cost", "alert_level": level,
                "current_usage": round(total_cost, 4), "limit": monthly_cost_limit,
                "percentage": round(pct, 1), "month": month_name,
                "days_remaining": days_remaining, "projected_total": round(projected_cost, 4),
                "policy_info": policy_info, "enforcement_mode": enforcement_mode,
            })
    else:
        monthly_limit = policy.get("monthly_token_limit", 0)
        if monthly_limit > 0:
            monthly_pct = (total_tokens / monthly_limit) * 100
            day_of_month = int(current_date.split("-")[2])
            daily_average = total_tokens / max(1, day_of_month)
            projected_total = daily_average * days_in_month
            level = None
            if total_tokens >= monthly_limit:
                level = "exceeded"
            elif total_tokens > policy.get("warning_threshold_90", monthly_limit * 0.9):
                level = "critical"
            elif total_tokens > policy.get("warning_threshold_80", monthly_limit * 0.8):
                level = "warning"
            if level and f"{email}#monthly#{level}" not in sent_alerts:
                alerts.append({
                    "user": email, "alert_type": "monthly", "alert_level": level,
                    "current_usage": int(total_tokens), "limit": monthly_limit,
                    "percentage": round(monthly_pct, 1), "month": month_name,
                    "days_remaining": days_remaining, "daily_average": int(daily_average),
                    "projected_total": int(projected_total), "policy_info": policy_info,
                    "enforcement_mode": enforcement_mode,
                })

    # Daily — prefer cost limit if configured, fall back to token limit
    if daily_cost_limit > 0:
        dpct = (daily_cost / daily_cost_limit) * 100
        dlevel = None
        if daily_cost >= daily_cost_limit:
            dlevel = "exceeded"
        elif dpct >= 90:
            dlevel = "critical"
        elif dpct >= 80:
            dlevel = "warning"
        if dlevel and f"{email}#daily_cost#{current_date}#{dlevel}" not in sent_alerts:
            alerts.append({
                "user": email, "alert_type": "daily_cost", "alert_level": dlevel,
                "current_usage": round(daily_cost, 4), "limit": daily_cost_limit,
                "percentage": round(dpct, 1), "date": current_date,
                "policy_info": policy_info, "enforcement_mode": enforcement_mode,
            })
    else:
        daily_limit = policy.get("daily_token_limit")
        if daily_limit and daily_limit > 0:
            daily_pct = (daily_tokens / daily_limit) * 100
            dlevel = None
            if daily_tokens >= daily_limit:
                dlevel = "exceeded"
            elif daily_tokens > (daily_limit * 0.9):
                dlevel = "critical"
            elif daily_tokens > (daily_limit * 0.8):
                dlevel = "warning"
            if dlevel and f"{email}#daily#{current_date}#{dlevel}" not in sent_alerts:
                alerts.append({
                    "user": email, "alert_type": "daily", "alert_level": dlevel,
                    "current_usage": int(daily_tokens), "limit": daily_limit,
                    "percentage": round(daily_pct, 1), "date": current_date,
                    "policy_info": policy_info, "enforcement_mode": enforcement_mode,
                })
    return alerts


def get_sent_alerts(month_name):
    """Get alerts already sent this month."""
    sent = set()
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        response = quota_table.query(
            KeyConditionExpression=Key("pk").eq("ALERTS") & Key("sk").begins_with(f"{month_prefix}#ALERT#")
        )
        for item in response.get("Items", []):
            parts = item["sk"].split("#")
            if len(parts) >= 5:
                email, atype, alevel = parts[2], parts[3], parts[4]
                if atype == "daily" and len(parts) >= 6:
                    sent.add(f"{email}#{atype}#{parts[5]}#{alevel}")
                else:
                    sent.add(f"{email}#{atype}#{alevel}")
        while "LastEvaluatedKey" in response:
            response = quota_table.query(
                KeyConditionExpression=Key("pk").eq("ALERTS") & Key("sk").begins_with(f"{month_prefix}#ALERT#"),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            for item in response.get("Items", []):
                parts = item["sk"].split("#")
                if len(parts) >= 5:
                    email, atype, alevel = parts[2], parts[3], parts[4]
                    if atype in ("daily", "daily_cost") and len(parts) >= 6:
                        sent.add(f"{email}#{atype}#{parts[5]}#{alevel}")
                    else:
                        sent.add(f"{email}#{atype}#{alevel}")
    except Exception as e:
        print(f"Error checking sent alerts: {e}")
    return sent


def record_sent_alert(month_name, email, alert_type, alert_level, alert_data):
    """Record sent alert to prevent duplicates."""
    try:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        if alert_type in ("daily", "daily_cost"):
            date = alert_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}#{date}"
        else:
            sk = f"{month_prefix}#ALERT#{email}#{alert_type}#{alert_level}"
        quota_table.put_item(Item={
            "pk": "ALERTS", "sk": sk,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "email": email, "alert_type": alert_type, "alert_level": alert_level,
            "usage_at_alert": Decimal(str(alert_data.get("current_usage", 0))),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + (60 * 86400),
        })
    except Exception as e:
        print(f"Error recording alert: {e}")


def send_alerts(alerts):
    """Send alerts via SNS."""
    if not SNS_TOPIC_ARN:
        print("SNS_TOPIC_ARN not configured")
        return
    for alert in alerts:
        try:
            level_prefix = {"warning": "WARNING", "critical": "CRITICAL", "exceeded": "EXCEEDED"}.get(alert["alert_level"], "ALERT")
            alert_type = alert["alert_type"]
            is_cost = alert_type in ("monthly_cost", "daily_cost")

            type_label = {
                "monthly": "Monthly Token Quota",
                "daily": "Daily Token Quota",
                "monthly_cost": "Monthly Cost Quota",
                "daily_cost": "Daily Cost Quota",
            }.get(alert_type, "Quota")

            subject = f"Claude Code {level_prefix} - {type_label} - {alert['percentage']:.0f}%"

            if is_cost:
                usage_str = f"${alert['current_usage']:.4f} / ${alert['limit']:.2f} ({alert['percentage']:.1f}%)"
            else:
                usage_str = f"{int(alert['current_usage']):,} / {int(alert['limit']):,} tokens ({alert['percentage']:.1f}%)"

            lines = [
                f"User:        {alert['user']}",
                f"Alert:       {type_label} - {alert['alert_level'].upper()}",
                f"Usage:       {usage_str}",
                f"Policy:      {alert.get('policy_info', 'default')}",
                f"Enforcement: {alert.get('enforcement_mode', 'alert')}",
            ]

            if alert_type == "monthly_cost" and alert.get("projected_total") is not None:
                lines.append(f"Projected:   ${alert['projected_total']:.2f} this month ({alert.get('days_remaining', '?')} days remaining)")
            elif alert_type == "monthly" and alert.get("projected_total") is not None:
                lines.append(f"Projected:   {int(alert['projected_total']):,} tokens ({alert.get('days_remaining', '?')} days remaining)")

            if alert.get("date"):
                lines.append(f"Date:        {alert['date']} (daily limit resets at UTC midnight)")

            message = "\n".join(lines)
            sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        except Exception as e:
            print(f"Error sending alert for {alert['user']}: {e}")
