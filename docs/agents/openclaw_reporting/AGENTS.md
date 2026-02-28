# Agent prompt: OpenClaw scheduled usage reports (via Tokdash API)

Goal: set up an automated daily/weekly/monthly report that summarizes usage from Tokdash.

Tokdash exposes a local HTTP API (default: `http://127.0.0.1:55423`) that OpenClaw can query on a schedule.

## Questions to ask first
- Where is Tokdash running (host/port)? Is it already in the background?
- Report period: `today`, `week`, `month`, or `N` days?
- Delivery schedule: what time + timezone? (cron uses machine local timezone)
- Delivery channel:
  - stdout/log file only
  - email
  - Slack/Discord webhook
  - OpenClaw agent-based delivery (needs details)
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

## Starting script (recommended)
Use: `docs/agents/openclaw_reporting/openclaw_cron_job.py` from this repo, but **place it wherever the user wants** (typically under the OpenClaw workspace).

Suggested install location (example):
- `~/.openclaw/workspace/monitor/openclaw_cron_job.py`

Install options:

1) Copy from an existing tokdash checkout:
```bash
mkdir -p ~/.openclaw/workspace/monitor
cp /PATH/TO/tokdash/docs/agents/openclaw_reporting/openclaw_cron_job.py ~/.openclaw/workspace/monitor/openclaw_cron_job.py
```

2) Or download the single file (if the user has access to the repo):
```bash
mkdir -p ~/.openclaw/workspace/monitor
curl -L '<RAW_URL_TO>/docs/agents/openclaw_reporting/openclaw_cron_job.py' -o ~/.openclaw/workspace/monitor/openclaw_cron_job.py
```

It calls `GET /api/usage` and prints a human-readable report.

Run it once manually before scheduling:
```bash
python3 ~/.openclaw/workspace/monitor/openclaw_cron_job.py --base-url http://127.0.0.1:55423 --period today --lang both
```

## Scheduling (cron)
1. Pick a log directory (example: `~/tokdash_reports/`) and ensure it exists.
2. Add a crontab entry (`crontab -e`). Example: every day at 08:00:
```cron
0 8 * * * /usr/bin/python3 /ABS/PATH/to/openclaw_cron_job.py --base-url http://127.0.0.1:55423 --period today --lang both >> /ABS/PATH/to/tokdash_reports/daily.log 2>&1
```

Notes:
- cron requires **absolute paths** (it won’t expand `~`).
- Ensure Tokdash is already running at that `--base-url` (use the systemd/launchd prompt if needed).

## Delivery notes
- Default to “write report to stdout / a log file”.
- If the user wants Slack/email/webhook delivery, ask for:
  - which workspace/channel
  - preferred message format (plain text vs Markdown)
  - credentials handling (do **not** commit secrets; store locally, ideally under a gitignored `.api_keys/`)
