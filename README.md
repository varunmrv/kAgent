# kAgent - Soura Mitra registration monitor

Watches https://souramitra.com/ and sends a Telegram alert when the **Farmer Registration**
entry point appears or disappears. `/status`, `/check` and `/help` are answered within seconds.

## How it runs on GitHub Actions only

```
monitor.yml  (one long job, ~5.5 h)                    watchdog.yml (cron every 10 min, ~10 s)
  restore state  ->  loop: long-poll Telegram            "is a monitor run active or queued?"
                          + website check / 5 min          yes -> do nothing
  at 5.5 h: save state, exit 0                             no  -> start monitor.yml
  start the NEXT run (queues behind this one)
```

- **Hand-over:** the next run is queued while the current one is still running, and the
  `concurrency` group (`kagent-monitor`, no cancel) starts it as soon as the current one ends.
  Gaps are seconds, and two monitors never poll at once.
- **Safety net:** GitHub may delay or drop cron runs. The watchdog is tiny, so a dropped run costs
  nothing. If the chain ever breaks (crash, manual cancel), it is restarted within about 10-20 minutes.
  A "no check for N min" message tells you when that happened.
- **State** (`state.json`: status, Telegram offset, failure counters) is stored on an orphan
  `state` branch, saved every 20 minutes and at exit, so `main` has no bot commits.
- **Alerts:** registration detected / gone, 3 failed checks in a row, recovery, monitor gap,
  and a failed workflow run.

## Setup

1. Repo **Settings > Secrets and variables > Actions**: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. **Settings > Actions > General**: *Workflow permissions* = read and write; for forks set
   *Require approval for all outside collaborators*.
3. Start it once: **Actions > kAgent Monitor > Run workflow** (or `gh workflow run monitor.yml`).
   After that the chain and the watchdog keep it going.

To stop it: disable the **kAgent Watchdog** workflow, then cancel the running **kAgent Monitor** run.

## Run locally

```bash
pip install -r requirements.txt && python -m playwright install chromium
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python monitor.py --once      # one poll + one check
python monitor.py             # loop until RUN_SECONDS (default 19800)
```

Settings (env vars): `CHECK_INTERVAL_SECONDS` (300), `RUN_SECONDS` (19800),
`POLL_TIMEOUT_SECONDS` (25), `GAP_ALERT_SECONDS` (1200).

## Security notes (public repo)

- The token and chat id exist only as GitHub Secrets. No workflow uses `pull_request` triggers,
  so fork PRs cannot reach them.
- `monitor.py` redacts the bot token from every log line and error message.
- `state.json`, `.env` and `__pycache__` are git-ignored. `state.json` holds no secrets
  (status, timestamps, Telegram update offset).
- Only the configured chat id can send commands; other chats are ignored.
- **If the token was ever pasted anywhere public, revoke it in @BotFather (`/revoke`) and update the secret.**
