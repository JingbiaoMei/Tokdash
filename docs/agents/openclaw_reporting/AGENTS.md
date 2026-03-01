# Agent prompt: OpenClaw scheduled usage reports (via Tokdash API)

Goal: set up an automated daily/weekly/monthly report that summarizes usage from Tokdash.

Tokdash exposes a local HTTP API (default: `http://127.0.0.1:55423`) that OpenClaw can query on a schedule.

## Questions to ask first
- Where is Tokdash running (host/port)? Is it already in the background?
- Should remote access use `tailscale serve` (recommended) instead of LAN exposure?
- Report period: `today`, `week`, `month`, or `N` days?
- Delivery schedule: what time + timezone?
- Delivery channel:
  - `--announce` with `--channel` (WhatsApp, Telegram, Slack, Discord, etc.)
  - `--webhook` URL for custom integrations
  - `--announce` with no channel (posts to main session only)
- Language: English vs Chinese vs both?

## Tokdash API quick reference
- Combined usage: `GET /api/usage?period=<period>`
  - fields commonly used in reports:
    - `total_cost`, `total_tokens`
    - `openclaw_models` (list)
    - `coding_apps` (dict) / `apps` (dict)
    - `coding_models` (list)
    - `combined_models` (list; aggregated across sources)
- Coding tools only: `GET /api/tools?period=<period>`
- OpenClaw only: `GET /api/openclaw?period=<period>`

Example:
```bash
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

## Scheduling (OpenClaw native cron)

Use OpenClaw's built-in cron scheduler. The isolated agent will query Tokdash and deliver the report.

### Daily report (announce to channel)

```bash
openclaw cron add \
  --name "Tokdash daily report" \
  --cron "0 8 * * *" \
  --tz "America/Los_Angeles" \
  --session isolated \
  --message "Query the Tokdash API at http://127.0.0.1:55423/api/usage?period=today and generate a usage report. Format: show total cost, total tokens, top models by cost, and breakdown by tool (OpenClaw vs coding tools). Respond in both English and Chinese." \
  --announce \
  --channel whatsapp \
  --to "+15551234567"
```

### Daily report (webhook delivery)

```bash
openclaw cron add \
  --name "Tokdash daily webhook" \
  --cron "0 8 * * *" \
  --tz "America/Los_Angeles" \
  --session isolated \
  --message "Query the Tokdash API at http://127.0.0.1:55423/api/usage?period=today and generate a usage report in JSON format with: total_cost, total_tokens, top_models (by cost), and tool_breakdown." \
  --webhook "https://your-webhook.example.com/tokdash"
```

### Weekly report (Monday 08:00)

```bash
openclaw cron add \
  --name "Tokdash weekly report" \
  --cron "0 8 * * 1" \
  --tz "America/Los_Angeles" \
  --session isolated \
  --message "Query the Tokdash API at http://127.0.0.1:55423/api/usage?period=week and generate a weekly usage summary. Include: total cost, token breakdown, top 5 models by cost, and day-by-day activity highlights." \
  --announce \
  --channel slack \
  --to "channel:C1234567890"
```

### Monthly report (1st of each month)

```bash
openclaw cron add \
  --name "Tokdash monthly report" \
  --cron "0 8 1 * *" \
  --tz "America/Los_Angeles" \
  --session isolated \
  --message "Query the Tokdash API at http://127.0.0.1:55423/api/usage?period=month and generate a monthly usage summary. Include: total cost, token breakdown, top models, cost trend vs previous month if available." \
  --announce \
  --channel telegram \
  --to "-1001234567890"
```

## Cron job management

List existing jobs:
```bash
openclaw cron list
```

Run a job immediately (test):
```bash
openclaw cron run <job-id>
```

View run history:
```bash
openclaw cron runs --id <job-id>
```

Edit an existing job:
```bash
openclaw cron edit <job-id> --message "Updated prompt..." --tz "UTC"
```

Remove a job:
```bash
openclaw cron remove <job-id>
```

## Prerequisites
- Ensure Tokdash is already running at the specified base URL (use the systemd/launchd prompt if needed: `https://github.com/JingbiaoMei/Tokdash/blob/main/docs/agents/systemd/AGENTS.md`).
- For remote Tokdash access, use `tailscale serve --bg 55423` and use the resulting HTTPS URL.

## Delivery notes
- `--announce` delivers to the specified channel (WhatsApp, Telegram, Slack, Discord, etc.)
- `--webhook` POSTs the result to a URL (useful for custom integrations)
- If delivery channel is omitted, the report posts to the main session only
- The isolated session has no prior context; include all necessary instructions in `--message`
