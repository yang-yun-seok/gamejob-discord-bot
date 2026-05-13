# Gamejob Discord Bot

This repository runs a GitHub Actions bot that checks a Gamejob listing URL and sends only new postings to a Discord webhook.

## How it works

1. GitHub Actions runs on a schedule.
2. `main.py` loads the Gamejob search results page from `GAMEJOB_TARGET_URL`.
3. Each posting is identified by its `GI_No` value from the posting URL.
4. Only unseen IDs are sent to Discord.
5. Sent IDs are stored in `sent_jobs.txt`.
6. The workflow commits and pushes `sent_jobs.txt` so the next run keeps the same state.

## Required configuration

### GitHub secret

- `DISCORD_WEBHOOK_URL`

### GitHub repository variable

- `GAMEJOB_TARGET_URL`
  - Set this to the full filtered Gamejob results URL you want to monitor.
  - Example: `https://www.gamejob.co.kr/Recruit/joblist?menucode=job`

## Optional configuration

- `SEED_ONLY_ON_FIRST_RUN`
  - Default: `true`
  - If `true`, the first run stores the current postings without sending them.
- `STATE_LIMIT`
  - Default: `500`
- `REQUEST_TIMEOUT_SECONDS`
  - Default: `20`
- `DISCORD_MAX_RETRIES`
  - Default: `3`

## GitHub Actions permissions

Enable write access for workflows:

1. `Settings`
2. `Actions` -> `General`
3. `Workflow permissions`
4. Select `Read and write permissions`

If the branch is protected, direct pushes from GitHub Actions may still be blocked.

## Local run

```bash
pip install -r requirements.txt
set DISCORD_WEBHOOK_URL=...
set GAMEJOB_TARGET_URL=https://www.gamejob.co.kr/Recruit/joblist?menucode=job
python main.py
```

PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
$env:GAMEJOB_TARGET_URL = "https://www.gamejob.co.kr/Recruit/joblist?menucode=job"
python main.py
```

## Files

- `main.py`: scraper, dedupe logic, Discord delivery
- `sent_jobs.txt`: state file for delivered posting IDs
- `.github/workflows/gamejob-discord-bot.yml`: scheduled workflow and state commit step
