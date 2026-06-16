# Claude Code Quota Monitoring

Quota monitoring tracks user token and cost consumption, sending automated alerts when usage thresholds are exceeded and optionally blocking credential issuance when limits are reached.

## Overview

The quota monitoring system is an optional CloudFormation stack that tracks per-user token and cost consumption against configurable limits. Usage data is sourced from Bedrock model invocation logs — a server-side data source that captures all Claude Code clients (CLI, VS Code extension, Claude Desktop, browser) without requiring any client-side telemetry configuration.

### Key Features

- **Server-side data source**: Reads from the `bedrock-model-invocation` CloudWatch log group — captures usage from all Bedrock clients automatically
- **Per-user token and cost tracking**: Monthly and daily consumption monitoring with token and USD cost limits
- **Fine-grained quota policies**: Set limits at user, group, or default levels with precedence rules
- **Multiple limit types**: Monthly tokens, daily tokens, monthly cost (USD), daily cost (USD)
- **Configurable thresholds**: Alerts at 80%, 90%, and 100% of limits
- **JWT group integration**: Automatically extract group membership from identity provider claims
- **Alert deduplication**: One alert per threshold per limit type per user per period
- **DynamoDB storage**: Efficient tracking with automatic TTL cleanup
- **Real-time blocking**: Credential issuance denied at auth time when limits exceeded
- **Visual notifications**: Progress bars with cost and token data in both browser popup and terminal

### Architecture Components

- **UserQuotaMetrics Table**: DynamoDB table storing monthly/daily usage totals with token type breakdown and cost
- **QuotaPolicies Table**: DynamoDB table storing fine-grained quota policies (user/group/default) with token and cost limits
- **Quota Monitor Lambda** (`quota_monitor`): Runs every 15 minutes — queries `bedrock-model-invocation` log group via CloudWatch Logs Insights, updates DynamoDB, checks thresholds, sends SNS alerts, and publishes CloudWatch metrics. Also resets stale `daily_cost` values in DynamoDB at UTC midnight.
- **Quota Check Lambda** (`quota_check`): Real-time check called at credential issuance time — validates token and cost usage against the effective policy
- **Usage Dashboard**: Shows per-user quota status (daily %, monthly %, enforcement mode, policy tag) inline. Cost amounts are displayed to 2 decimal places. Quota colours indicate green (0–80%), yellow (80–100%), and red (>100%). The client version (e.g. `v1.0.0`) is displayed next to each user's email address, sourced from the `ClientVersion` CloudWatch metric emitted by the daemon.
- **SNS Topic**: Alert delivery to administrators
- **EventBridge Rule**: Lambda scheduling
- **API Gateway**: Secured HTTP endpoint for real-time quota checks

## Configuration

> **Prerequisites**: Quota monitoring requires the `bedrock-model-invocation` log group to be enabled in your AWS account. This is a Bedrock account-level setting. See the [Bedrock model invocation logging docs](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) for setup instructions.

During `ccwb init`, quota monitoring is configured as part of the optional features section. You'll be prompted to configure:
- Monthly token limit per user (default: 225 million tokens)
- Automatic threshold calculation (80% warning at 180M, 90% critical at 202.5M)
- Daily token limit with burst buffer (auto-calculated from monthly)
- Enforcement modes for daily and monthly limits

Cost limits (`--monthly-cost-limit`, `--daily-cost-limit`) are set per policy using the `ccwb quota` CLI commands after deployment — they are not set during `ccwb init`.

Deploy using `poetry run ccwb deploy` (deploys all enabled stacks) or `poetry run ccwb deploy quota` for just the quota stack. The OIDC configuration is automatically passed from your profile settings. For complete deployment instructions, see the [CLI Reference](CLI_REFERENCE.md#deploy---deploy-infrastructure).

> **Important**: After deploying or updating the quota stack, set `ENABLE_FINEGRAINED_QUOTAS=true` on both the `quota_monitor` and `quota_check` Lambda functions to enable fine-grained policy support. This is done automatically by `ccwb deploy quota` when fine-grained quotas are enabled in your profile.

## Configuration Settings

| Parameter               | Default     | Description                                    |
| ----------------------- | ----------- | ---------------------------------------------- |
| MonthlyTokenLimit       | 225M tokens | Default maximum per user per month             |
| DailyTokenLimit         | ~8.25M tokens| Daily limit (auto-calculated with burst buffer)|
| BurstBufferPercent      | 10%         | Daily buffer for usage variation (5-25%)       |
| MonthlyEnforcementMode  | block       | Block access when monthly limit exceeded       |
| DailyEnforcementMode    | alert       | Alert only when daily limit exceeded           |
| Warning Threshold       | 80% (180M)  | First alert level                              |
| Critical Threshold      | 90% (202.5M)| Second alert level                             |
| Check Frequency         | 15 minutes  | Lambda execution interval                      |
| Alert Retention         | 60 days     | DynamoDB TTL for deduplication                 |
| EnableFinegrainedQuotas | false       | Enable fine-grained policy support (must be true to use per-policy cost limits) |

To update limits: Re-run `ccwb init` and redeploy with `ccwb deploy quota`.

### Data Source: Bedrock Model Invocation Logs

The quota monitor reads usage from the `bedrock-model-invocation` CloudWatch log group rather than from client-side OTLP telemetry. This approach:

- **Captures all clients** — CLI, VS Code extension, Claude Desktop, API calls — without any client-side configuration
- **Is authoritative** — server-side data cannot be bypassed or spoofed by the client
- **Provides cost data** — input/output/cache token counts are available per invocation, enabling accurate USD cost calculation

The Lambda queries this log group using CloudWatch Logs Insights every 15 minutes, looking back a configurable window to aggregate token counts per user and model. User identity is extracted from the IAM session name in the `identity.arn` field — for federated users, this is the email address.

### Per-Model Pricing (EU, as of June 2026)

The quota monitor uses these prices to compute USD cost from token counts:

| Model   | Input (per 1M) | Output (per 1M) | Cache Read | Cache Write |
|---------|---------------|----------------|------------|-------------|
| Opus    | $5.00         | $25.00         | $0.50      | $6.25       |
| Sonnet  | $3.00         | $15.00         | $0.30      | $3.75       |
| Haiku   | $1.00         | $5.00          | $0.10      | $1.25       |

Prices are configurable in the Lambda code (`quota_monitor/index.py`) if your region uses different rates.

## Daily Limits and Bill Shock Protection

To prevent unexpected costs from runaway usage, the system auto-calculates a daily limit from your monthly quota with a configurable burst buffer.

### Why Daily Limits?

Without daily limits, a user could consume their entire monthly quota in just 2-3 days of heavy usage, leading to unexpected costs or blocked access mid-month. Daily limits catch runaway usage within 24 hours while still allowing legitimate work patterns.

### Calculation

```
daily_limit = monthly_limit ÷ 30 × (1 + burst_buffer%)
```

Example with 225M monthly limit and 10% burst:
- Base daily: 225,000,000 ÷ 30 = 7,500,000 tokens/day
- With 10% burst: 7,500,000 × 1.10 = **8,250,000 tokens/day**

### Burst Buffer Guidance

The burst buffer allows for legitimate daily variation above the average:

| Buffer | Daily (225M/month) | Use Case |
|--------|-------------------|----------|
| 5% (strict)  | 7,875,000 tokens | Tight cost control, heavy days blocked quickly |
| 10% (default)| 8,250,000 tokens | Balanced protection for typical usage |
| 25% (flexible)| 9,375,000 tokens | Allows 1.25x average days, catches only extreme spikes |

### Enforcement Modes

Each limit type can be configured with different enforcement:

| Mode | Behavior | Use Case |
|------|----------|----------|
| **alert** | Send notifications, allow continued use | Monitoring, soft limits |
| **block** | Deny credential issuance when exceeded | Hard cost control |

**Recommended defaults:**
- **Daily**: `alert` - Warn about unusual patterns, don't interrupt work
- **Monthly**: `block` - Hard stop at budget limit

### Example Configuration

```
Monthly Limit: 225,000,000 tokens (block)
Daily Limit:   8,250,000 tokens (alert)
Burst Buffer:  10%

Behavior:
- Day 1: User consumes 9M tokens → Daily alert sent
- Day 2: User consumes 8.5M tokens → Daily alert sent
- Day 3-5: Normal usage (~7M/day) → No alerts
- Day 15: Monthly usage reaches 180M → 80% warning alert
- Day 20: Monthly usage reaches 225M → Access blocked
```

## Fine-Grained Quota Policies

Fine-grained quotas allow administrators to set different limits for different users and groups, with a clear precedence hierarchy.

### Policy Types

1. **User Policies**: Apply to a specific user by email address
2. **Group Policies**: Apply to all users in a group (from JWT claims)
3. **Default Policy**: Applies to all users without a more specific policy

### Policy Precedence

When determining the effective quota for a user:

1. **User-specific policy** (highest priority): If a policy exists for the user's email, use it
2. **Group policy** (most restrictive): If user belongs to multiple groups with policies, use the **lowest limit** (most restrictive)
3. **Default policy**: If no user or group policy applies, use the default
4. **No policy**: If no policies are defined, usage is **unlimited** (quota monitoring disabled for that user)

### Limit Types

Each policy can configure four types of limits:

| Limit Type           | Description                              | Reset Period      |
| -------------------- | ---------------------------------------- | ----------------- |
| Monthly Token Limit  | Maximum tokens per calendar month        | 1st of each month |
| Daily Token Limit    | Maximum tokens per day                   | UTC midnight      |
| Monthly Cost Limit   | Maximum USD spend per calendar month     | 1st of each month |
| Daily Cost Limit     | Maximum USD spend per day                | UTC midnight      |

Cost limits are checked before token limits. If a cost limit is exceeded, the user is blocked regardless of their token count.

### Managing Policies with CLI

Use the `ccwb quota` commands to manage policies:

```bash
# Set a user-specific policy with token and cost limits
ccwb quota set-user john.doe@company.com --monthly-limit 500M --daily-limit 20M \
  --monthly-cost-limit 30.00 --daily-cost-limit 4.00 \
  --monthly-enforcement block --daily-enforcement alert

# Set a group policy
ccwb quota set-group engineering --monthly-limit 400M --monthly-cost-limit 150.00

# Set the default policy for all users
ccwb quota set-default --monthly-limit 225M --daily-limit 8M \
  --monthly-cost-limit 150.00 --daily-cost-limit 30.00 \
  --monthly-enforcement block --daily-enforcement alert

# List all policies (shows token and cost limits, enabled status)
ccwb quota list
ccwb quota list --type group

# Enable or disable a policy without deleting it
ccwb quota disable user john.doe@company.com   # policy skipped; falls to group/default
ccwb quota enable user john.doe@company.com    # re-activate

# Show effective policy for a user (resolves user > group > default)
ccwb quota show john.doe@company.com

# View current usage against limits (tokens and cost)
ccwb quota usage john.doe@company.com

# Delete a policy
ccwb quota delete group engineering

# Temporarily unblock a user who exceeded quota
ccwb quota unblock john.doe@company.com --duration 24h
```

> **Note**: All email addresses are normalised to lowercase before storing in DynamoDB. Mixed-case emails from the identity provider (e.g. `John.Doe@company.com`) are automatically lowercased when looking up and storing policies.

### Token Value Shortcuts

The CLI supports human-readable token values:

- `225M` = 225,000,000 (225 million) - default limit
- `500K` = 500,000 (500 thousand)
- `1B` = 1,000,000,000 (1 billion)

### Group Membership from JWT Claims

The system automatically extracts group membership from JWT token claims:

- `groups`: Standard groups claim
- `cognito:groups`: Amazon Cognito groups
- `custom:department`: Custom department claim (treated as a group)

Configure your identity provider to include group claims in the JWT tokens issued to users.

## Alert Management

After deployment, subscribe to the SNS topic for notifications:

```bash
# Get topic ARN from stack outputs
aws cloudformation describe-stacks --stack-name <quota-stack-name> \
  --query 'Stacks[0].Outputs[?OutputKey==`QuotaAlertTopicArn`].OutputValue' \
  --output text

# Subscribe (email, SMS, HTTPS webhook, etc.)
aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint admin@company.com
```

### Alert Types

The system sends alerts for two limit types, each with three threshold levels:

#### Monthly Token Alert

Sent when monthly token usage exceeds 80%, 90%, or 100% of the monthly limit.

#### Daily Token Alert

Sent when daily token usage exceeds 80%, 90%, or 100% of the daily limit. Daily alerts can be sent each day (they include the date in the deduplication key).

### Sample Alert Content

**Token quota alert:**
```
Subject: Claude Code CRITICAL - Monthly Token Quota - 92%

Claude Code Usage Alert - Monthly Token Quota

User: john.doe@company.com
Alert Level: CRITICAL
Month: November 2025
Policy: group:engineering

Current Usage: 207,000,000 tokens
Monthly Limit: 225,000,000 tokens
Percentage Used: 92.0%

Days Remaining in Month: 8
Daily Average: 9,409,091 tokens
Projected Monthly Total: 282,272,727 tokens

---
This alert is sent once per threshold level per month.
```

**Cost quota alert:**
```
Subject: Claude Code WARNING - Daily Cost Quota - 82%

Claude Code Usage Alert - Daily Cost Quota

User: john.doe@company.com
Alert Level: WARNING
Date: 2025-11-15
Policy: user:john.doe@company.com

Current Usage: $3.2800 / $4.00 (82.0%)

Projected Daily Total: $4.79

---
This alert is sent once per threshold level per day.
```

Alerts are deduplicated — each threshold triggers only once per user per period, with history stored in DynamoDB (60-day TTL). Daily alerts (both token and cost) include the date in the deduplication key, so the same threshold can trigger once per day.

## User Notifications

When users approach or exceed their quota limits, they receive visual notifications in both the terminal and browser.

### Browser Notification

The credential provider opens a browser page showing quota status when:

| Condition | Browser Opens? | Access Granted? |
|-----------|----------------|-----------------|
| Within quota (<80%) | No | Yes |
| Warning (80-99%) | Yes (yellow) | Yes |
| Blocked (100%+) | Yes (red) | No |

The browser page displays:
- **Status header**: Warning (⚠️) or Blocked (🚫) with colour-coded background
- **Triggered by**: Which metric caused the alert or block (e.g. "Approaching daily cost")
- **Monthly Token Usage**: Progress bar with token count and percentage
- **Daily Token Usage**: Progress bar with token count and percentage
- **Monthly Cost**: Progress bar with USD amount (shown when `monthly_cost_limit` is set)
- **Daily Cost**: Progress bar with USD amount (shown when `daily_cost_limit` is set)
- **Message**: Explanation and guidance

### Terminal Output

In addition to browser notifications, the terminal shows:

**Warning (80%+ usage):**
```
============================================================
QUOTA WARNING
============================================================
  Triggered by: monthly cost limit

  Monthly Token Usage:  180,000,000 / 225,000,000 tokens (80.0%)
  Daily Token Usage:     6,600,000 /   8,250,000 tokens (80.0%)
  Monthly Cost:              $24.1200 / $30.00 (80.4%)
  Daily Cost:                 $3.2800 / $4.00 (82.0%)
============================================================
```

**Blocked (100%+ usage):**
```
============================================================
ACCESS BLOCKED - QUOTA EXCEEDED
============================================================

Daily cost limit exceeded: $7.2100 / $4.00 (180.2%).
Contact your administrator for assistance.

Current Usage:
  Monthly Token Usage:  185,000,000 / 225,000,000 tokens (82.2%)
  Daily Token Usage:     8,100,000 /   8,250,000 tokens (98.2%)
  Monthly Cost:              $24.8700 / $30.00 (82.9%)
  Daily Cost:                 $7.2100 / $4.00 (180.2%)

Policy: user:john.doe@company.com

To request an unblock, contact your administrator.
============================================================
```

### Periodic Quota Re-Check

By default, quota is re-checked every 30 minutes even when credentials are cached. This closes the enforcement gap where users could continue working for up to 12 hours after being blocked (the credential cache duration).

Configure during `ccwb init`:

| Interval | Check Frequency | Max Enforcement Delay | UX Impact |
|----------|----------------|----------------------|-----------|
| 0 | Every request | Immediate | ~200ms per request |
| 15 | Every 15 min | 15 minutes | Minimal |
| 30 (default) | Every 30 min | 30 minutes | Imperceptible |
| 60 | Every hour | 1 hour | None |

**How it works:**

1. User requests credentials (cached or fresh)
2. If last quota check was more than `interval` minutes ago:
   - Call quota API (~200ms)
   - Update timestamp
3. If blocked: Show browser notification, deny credentials
4. If warning (80%+): Show browser notification, issue credentials
5. If OK: Issue credentials silently

**Trade-offs:**

- **Interval = 0** (strictest): Every request checks quota. Adds ~200ms latency to each credential request. Use for strict cost control where immediate enforcement is critical.
- **Interval = 30** (recommended): Balance between enforcement tightness and user experience. Users are blocked within 30 minutes of exceeding quota.
- **Interval = 60+** (relaxed): Minimal impact but users may work up to an hour after being blocked.

The check happens in the background when returning cached credentials - users only see a browser notification if their quota status changes.

## Bulk Policy Management

For organizations with many users, the CLI provides import/export commands to manage policies in bulk.

### Export Policies

Export existing policies to JSON or CSV for backup, audit, or migration:

```bash
# Export all policies to JSON
ccwb quota export policies.json

# Export to CSV for spreadsheet editing
ccwb quota export policies.csv

# Export only user policies
ccwb quota export users.json --type user
```

### Import Policies

Import policies from a file:

```bash
# Import from CSV, creating new and updating existing
ccwb quota import users.csv --update

# Preview changes without applying
ccwb quota import users.csv --dry-run

# Auto-calculate daily limits (monthly / 30 + burst buffer)
ccwb quota import users.csv --auto-daily --burst 15
```

### CSV Template

Create a CSV file with these columns:

```csv
type,identifier,monthly_token_limit,daily_token_limit,enforcement_mode,enabled
user,alice@example.com,300M,15M,alert,true
user,bob@example.com,200M,,block,true
group,engineering,500M,25M,alert,true
default,default,225M,8M,alert,true
```

**Required columns:** `type`, `identifier`, `monthly_token_limit`

**Token format:** Supports `K` (thousands), `M` (millions), `B` (billions), e.g., `300M` = 300,000,000 tokens

### Typical Workflow

1. **Initial setup from HR system:**
   ```bash
   # Export user list from HR, create CSV
   ccwb quota import users.csv --auto-daily --update
   ```

2. **Backup before changes:**
   ```bash
   ccwb quota export backup-$(date +%Y%m%d).json
   ```

3. **Cross-environment sync:**
   ```bash
   # Export from staging
   ccwb quota export policies.json --profile staging

   # Import to production
   ccwb quota import policies.json --profile production --update
   ```

See [CLI Reference](CLI_REFERENCE.md#quota-export---export-policies) for full documentation.

## Troubleshooting

### Quick Checks

```bash
# View Lambda logs
aws logs tail /aws/lambda/claude-code-quota-monitor --follow

# Query user quotas
aws dynamodb scan --table-name UserQuotaMetrics \
  --projection-expression "email, total_tokens, daily_tokens"

# List quota policies
aws dynamodb scan --table-name QuotaPolicies \
  --filter-expression "sk = :current" \
  --expression-attribute-values '{":current": {"S": "CURRENT"}}'
```

### Common Issues

- **No alerts**: Verify SNS subscriptions are confirmed and EventBridge rule is enabled
- **Missing users**: Check JWT tokens include email claim
- **Wrong policy applied**: Verify group claims are present in JWT tokens
- **Groups not detected**: Check that `ENABLE_FINEGRAINED_QUOTAS` is set to `true`
- **`ccwb quota usage` crashing**: Ensure you are on v3.1.0+. Earlier versions stored a single `enforcement_mode` field on `QuotaPolicy`; this has been split into `monthly_enforcement_mode` and `daily_enforcement_mode`. Re-deploy the quota stack and re-set any affected policies with `ccwb quota set-user`/`set-group`/`set-default`.
- **Daily cost not resetting**: If `daily_cost` in DynamoDB appears stale after midnight, verify the quota_monitor Lambda executed successfully at or after UTC midnight — it is responsible for zeroing the daily cost counter.

For detailed monitoring setup, see the [Monitoring Guide](MONITORING.md).

## Cost Considerations

**Estimated monthly costs for <1000 users: $2-10**
- Lambda: ~2,880 invocations x $0.0000002 = $0.58
- DynamoDB: Pay-per-request for user count x 2,880 operations
- SNS: $0.50 per million notifications
- CloudWatch Logs: Standard retention pricing
- QuotaPolicies table: Minimal cost (policies rarely change)

## Data Schema

### UserQuotaMetrics Table

**User Totals**: `PK: USER#{email}`, `SK: MONTH#{YYYY-MM}`
- Attributes: `total_tokens`, `daily_tokens`, `daily_date`, `total_cost`, `daily_cost`, `input_tokens`, `output_tokens`, `cache_tokens`, `cache_write_tokens`, `groups`, `last_updated`, `email`
- `total_tokens`: sum of all token types including cache writes; used for enforcement
- `input_tokens`, `output_tokens`, `cache_tokens` (cache read), `cache_write_tokens`: per-type breakdowns for auditability
- `total_cost`: accumulated USD cost for the current month (Decimal, 6dp)
- `daily_cost`: accumulated USD cost for `daily_date` (reset to 0 when date changes)
- `daily_date`: the date (YYYY-MM-DD UTC) that `daily_tokens` and `daily_cost` apply to
- TTL: End of following month

**Alert History**: `PK: ALERTS`, `SK: {YYYY-MM}#ALERT#{email}#{type}#{level}[#{date}]`
- Attributes: `sent_at`, `alert_type`, `alert_level`, `usage_at_alert`, `policy_info`
- `alert_type` values: `monthly`, `daily`, `monthly_cost`, `daily_cost`
- Daily and daily_cost alert keys include `#{date}` so the same threshold can fire once per day
- TTL: 60 days

### QuotaPolicies Table

**Policy Records**: `PK: POLICY#{type}#{identifier}`, `SK: CURRENT`
- Attributes: `policy_type`, `identifier`, `monthly_token_limit`, `daily_token_limit`, `monthly_cost_limit`, `daily_cost_limit`, `warning_threshold_80`, `warning_threshold_90`, `monthly_enforcement_mode`, `daily_enforcement_mode`, `enabled`, `created_at`, `updated_at`, `created_by`
- `monthly_enforcement_mode` / `daily_enforcement_mode`: `"alert"` or `"block"` — configured independently for monthly and daily limits
- `monthly_cost_limit` / `daily_cost_limit`: optional USD cost limits (Decimal, stored as string for DynamoDB precision)
- `enabled`: when `false`, this policy is skipped during quota resolution (user falls through to group/default policy)
- User policy identifiers are stored in lowercase (e.g. `POLICY#user#john.doe@company.com`)

**GSI: PolicyTypeIndex**
- PK: `policy_type` (user, group, default)
- SK: `identifier`
- Enables efficient queries like "list all group policies"

## Migration from Basic Quotas

If you're upgrading from the basic quota system (single global limit):

1. Deploy the updated CloudFormation stack (adds QuotaPolicies table)
2. Existing UserQuotaMetrics data continues working (new fields are nullable)
3. Set `EnableFinegrainedQuotas: true` in stack parameters
4. Optionally create a default policy to maintain previous behavior:
   ```bash
   ccwb quota set-default --monthly-limit 225M
   ```
5. Gradually add group/user policies as needed

**No breaking changes** - this is an enhancement that's opt-in through policy creation.

## Access Blocking (Phase 2)

When `enforcement_mode` is set to `"block"` for a policy, the system will deny credential issuance when a user exceeds their quota limits.

### How Blocking Works

1. **Quota Check API**: A real-time API endpoint checks user quota before credential issuance
2. **Enforcement Point**: The credential provider calls the quota check API after OIDC authentication
3. **Block Triggers**: Access is blocked when any of the following are exceeded (cost is checked first):
   - Monthly cost ≥ monthly_cost_limit (if configured)
   - Daily cost ≥ daily_cost_limit (if configured)
   - Monthly token usage ≥ monthly_token_limit
   - Daily token usage ≥ daily_token_limit (if configured)
4. **User notification**: The browser popup and terminal output both show which limit was exceeded and current usage vs limit for all configured metrics

### Configuring Blocking

Enable blocking for a policy:

```bash
# Set user policy with monthly blocking and daily alerting
ccwb quota set-user john.doe@company.com --monthly-limit 10M \
  --monthly-enforcement block --daily-enforcement alert

# Set group policy with blocking
ccwb quota set-group engineering --monthly-limit 50M --monthly-enforcement block

# Set default with monthly block, daily alert
ccwb quota set-default --monthly-limit 225M \
  --monthly-enforcement block --daily-enforcement alert
```

### Admin Override (Unblock)

Administrators can temporarily unblock users who have exceeded their quota:

```bash
# Unblock for 24 hours (default)
ccwb quota unblock john.doe@company.com

# Unblock for 7 days
ccwb quota unblock john.doe@company.com --duration 7d

# Unblock until end of month (quota reset)
ccwb quota unblock john.doe@company.com --duration until-reset

# With reason
ccwb quota unblock john.doe@company.com --duration 24h --reason "Urgent project deadline"
```

The unblock record expires automatically and is cleaned up by DynamoDB TTL.

### Error Handling: Fail-Open vs Fail-Closed

By default, the system uses **fail-open** behavior - if the quota check API is unavailable, access is allowed. This prevents service disruptions due to network issues.

Configure fail mode in your profile config:

```json
{
  "quota_fail_mode": "open"   // Allow on error (default)
  // OR
  "quota_fail_mode": "closed" // Deny on error (stricter)
}
```

The 15-minute Lambda monitoring job continues to run regardless, so alerts will still be sent even if real-time checks fail.

### Quota Check API

The Quota Check API is a secured HTTP endpoint that validates user quotas before credential issuance.

#### API Security

The API requires JWT authentication using your OIDC provider's tokens:

- **Authentication**: JWT token in `Authorization: Bearer <token>` header
- **Validation**: API Gateway JWT Authorizer validates the token against your OIDC provider
- **User Identity**: Email and group membership extracted from validated JWT claims (no query parameters)

This ensures:
- Only authenticated users can check quotas
- User identity cannot be spoofed (claims come from validated JWT)
- No additional credentials needed (uses same OIDC token from auth flow)

#### Deployment Configuration

When using `ccwb deploy quota`, the OIDC configuration is **automatically passed** from your profile settings (configured during `ccwb init`). No manual parameter configuration is required.

For manual CloudFormation deployments, provide your OIDC configuration:

```bash
aws cloudformation deploy \
  --stack-name claude-code-quota \
  --template-file quota-monitoring.yaml \
  --parameter-overrides \
    OidcIssuerUrl="https://company.okta.com" \
    OidcClientId="your-client-id" \
    # ... other parameters
```

The OIDC parameters must match your credential provider configuration:
- `OidcIssuerUrl`: Your identity provider's issuer URL (e.g., `https://company.okta.com` for Okta)
- `OidcClientId`: The client ID configured in your identity provider

After deploying, get the API endpoint from stack outputs:

```bash
# Get quota check API endpoint
aws cloudformation describe-stacks --stack-name <quota-stack-name> \
  --query 'Stacks[0].Outputs[?OutputKey==`QuotaCheckApiEndpoint`].OutputValue' \
  --output text
```

Configure the endpoint in your credential provider config.json:

```json
{
  "profiles": {
    "ClaudeCode": {
      "quota_api_endpoint": "https://xxx.execute-api.us-east-1.amazonaws.com"
    }
  }
}
```

#### API Responses

| Scenario | HTTP Status | Response |
|----------|-------------|----------|
| No/invalid JWT | 401 | Unauthorized (API Gateway rejects) |
| Valid JWT, quota OK | 200 | `{"allowed": true, ...}` |
| Valid JWT, quota exceeded | 200 | `{"allowed": false, "reason": "monthly_exceeded", ...}` |
| Valid JWT, missing email claim | 200 | `{"allowed": true, "reason": "missing_email_claim"}` (fail-open) |

### Enforcement Timing

**Important**: Quota enforcement only occurs at credential issuance time, not during an active session.

If a user exceeds their quota mid-session, they can continue using Claude Code until their credentials expire and they need to re-authenticate. At that point, the quota check will block access.

#### Example Timeline (12-hour session)

```
09:00 - User authenticates, quota check passes (at 50% of limit)
09:00 - AWS credentials issued, valid for 12 hours
15:00 - User exceeds 100% of monthly quota
15:01 - User CONTINUES working (credentials still valid)
21:00 - Credentials expire, user must re-authenticate
21:00 - Quota check BLOCKS access (enforcement finally applied)
```

In this scenario, there's a 6-hour gap between exceeding the quota (15:00) and enforcement (21:00).

#### Recommendation for Tight Enforcement

Reduce `max_session_duration` when blocking is enabled:

| Session Duration | Enforcement Gap | Use Case |
|------------------|-----------------|----------|
| 12h (default) | Up to 12 hours | Alert-only mode |
| 4h | Up to 4 hours | Moderate enforcement |
| 1h (recommended) | Up to 1 hour | Strict cost control |

Configure in your profile:

```json
{
  "profiles": {
    "ClaudeCode": {
      "max_session_duration": 3600,
      "quota_api_endpoint": "https://xxx.execute-api.us-east-1.amazonaws.com"
    }
  }
}
```

**Trade-off**: Shorter sessions mean more frequent re-authentication prompts for users, but provide tighter quota enforcement.

## Current Limitations

- Quotas reset on calendar month/day (UTC timezone)
- Requires email claim in JWT tokens for per-user attribution
- Group membership requires JWT group claims from identity provider
- Enforcement only at credential issuance (see [Enforcement Timing](#enforcement-timing) for mitigation)
- Cost pricing is hardcoded in Lambda code — update `quota_monitor/index.py` if model prices change
- Bedrock model invocation logging must be enabled in the AWS account (account-level setting)

## Future Enhancements

- **Self-service usage command**: `ccwb usage` for individual developers to check their own consumption without needing admin access
- **IAM deny enforcement**: Server-side deny policy applied to the federated role when a user exceeds quota, as a belt-and-suspenders complement to credential-time blocking

## Integration Points

- **Dashboard**: Shares DynamoDB metrics table and OTEL pipeline
- **Analytics**: Quota data available in Athena queries (see [Analytics Guide](ANALYTICS.md))
- **External Systems**: SNS topic supports webhooks, Lambda triggers, and third-party integrations
- **Identity Provider**: Group membership extracted from JWT claims

For complete monitoring setup and general telemetry information, see the [Monitoring Guide](MONITORING.md).
