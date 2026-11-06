#!/usr/bin/env python3
"""kAgent - Soura Mitra farmer-registration monitor with Telegram commands.

Two modes:
  python monitor.py          Long-running loop (default). Checks the website every
                             CHECK_INTERVAL_SECONDS and answers Telegram commands
                             instantly via long polling, until RUN_SECONDS elapses
                             (or SIGINT/SIGTERM arrives), then saves state and exits 0.
  python monitor.py --once   One poll + one check, then exit (handy for local tests).

Secrets come only from the environment (GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SITE_URL (the website to watch, e.g. https://example.com/).
Nothing sensitive is hard-coded, and none of the three values is ever logged or saved to state.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from urllib.parse import urlparse

import requests

BASE_URL = os.environ.get("SITE_URL", "").strip()   # secret: never hard-code or log it
SITE_HOST = urlparse(BASE_URL).hostname or ""
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
API = f"https://api.telegram.org/bot{TOKEN}"


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


CHECK_INTERVAL = env_int("CHECK_INTERVAL_SECONDS", 300)   # website check cadence
RUN_SECONDS = env_int("RUN_SECONDS", 19800)               # 5.5 h, then clean exit
POLL_TIMEOUT = env_int("POLL_TIMEOUT_SECONDS", 25)        # Telegram long-poll length
GAP_ALERT_SECONDS = env_int("GAP_ALERT_SECONDS", 1200)    # "monitor was down" notice
FAILURE_ALERT_AFTER = 3                                   # consecutive failed checks
MAX_POLL_ERRORS = 30                                      # then exit non-zero

DEFAULT_STATE = {
    "status": "unknown",
    "last_successful_check": None,
    "last_check_result": None,
    "last_error": None,
    "consecutive_failures": 0,
    "telegram_update_offset": 0,
}

# In-memory info shown by /status. The detected page URL lives here only: state.json is
# published on the public "state" branch, so it must not contain the site address.
RUNTIME = {"started_at": None, "ends_at": None, "next_check": None, "registration_url": None}


class Stop(BaseException):
    """Raised by the signal handler. BaseException so `except Exception` can't swallow it."""


# --------------------------------------------------------------------------- helpers

def now_dt():
    return datetime.now(timezone.utc)


def now():
    return now_dt().isoformat(timespec="seconds")


def redact(text):
    """Never let the bot token reach logs or Telegram (this repo's logs are public)."""
    text = str(text)
    for secret in (TOKEN, CHAT_ID, BASE_URL.rstrip("/"), SITE_HOST):
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot***", text)


def log(*parts):
    print(now(), *(redact(p) for p in parts), flush=True)


# --------------------------------------------------------------------------- state

def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            loaded.pop("last_registration_url", None)  # legacy key: never persist the URL
            state.update(loaded)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return state


def save_state(state):
    """Atomic write so a kill mid-save can never leave a corrupt state file."""
    folder = os.path.dirname(os.path.abspath(STATE_FILE))
    os.makedirs(folder, exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- telegram

def tg(method, payload=None, timeout=20, retries=3):
    """Call a Telegram Bot API method. Errors are re-raised WITHOUT the URL/token."""
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.ok and data.get("ok"):
                return data.get("result")
            if r.status_code == 429:
                time.sleep(min(int(data.get("parameters", {}).get("retry_after", 3)), 30))
            last = RuntimeError(
                f"Telegram {method} failed: HTTP {r.status_code} {data.get('description', '')}"
            )
        except requests.RequestException as e:
            last = RuntimeError(f"Telegram {method} network error: {type(e).__name__}")
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise last


def send_message(text):
    tg("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})


# --------------------------------------------------------------------------- commands

REGISTRATION_TEXT = {
    "open": "Registration entry point detected",
    "closed": "Registration entry point not detected",
    "unknown": "Not established yet",
}

HELP_TEXT = (
    "🤖 kAgent - SOURA MITRA MONITOR\n\n"
    "/status - Monitor health and last result\n"
    "/check - Check the website right now\n"
    "/help - Show this list\n\n"
    f"The monitor checks every {CHECK_INTERVAL // 60} min and answers commands within seconds."
)


def build_status(state):
    last = state.get("last_successful_check")
    status = state.get("status", "unknown")
    if last:
        age = (now_dt() - datetime.fromisoformat(last)).total_seconds()
        if age < 3 * CHECK_INTERVAL:
            health = "🟢 Healthy"
        else:
            health = f"🟠 STALE - last good check {int(age // 60)} min ago"
    else:
        health = "⚪ No successful check yet"
    nxt = RUNTIME.get("next_check")
    ends = RUNTIME.get("ends_at")
    return (
        "📊 SOURA MITRA MONITOR STATUS\n\n"
        f"Monitor health: {health}\n"
        f"Website: {BASE_URL}\n"
        f"Frequency: every {CHECK_INTERVAL // 60} min\n\n"
        f"Last successful check: {last or 'Not checked yet'}\n"
        f"Next check: {nxt.isoformat(timespec='seconds') if nxt else 'Unknown'}\n"
        f"Registration: {REGISTRATION_TEXT.get(status, status)}\n"
        f"Consecutive failures: {state.get('consecutive_failures', 0)}\n"
        f"This run ends (then auto-restarts): {ends.isoformat(timespec='seconds') if ends else 'n/a'}"
    )


def get_updates(state, timeout):
    return tg(
        "getUpdates",
        {
            "offset": state.get("telegram_update_offset", 0),
            "timeout": timeout,
            "allowed_updates": ["message"],
        },
        timeout=timeout + 15,
        retries=1,
    ) or []


def handle_update(update, state):
    """Return True if the user asked for an immediate check."""
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != CHAT_ID:
        return False  # ignore anyone else
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split()[0].split("@")[0].lower()
    if command == "/status":
        send_message(build_status(state))
    elif command == "/check":
        send_message("🔎 Checking Soura Mitra now...")
        return True
    elif command in ("/help", "/start"):
        send_message(HELP_TEXT)
    return False


def process_updates(state, timeout):
    """Fetch and handle updates. The offset is saved after EACH update, and one bad
    command can never block the queue or replay old messages."""
    force = False
    for update in get_updates(state, timeout):
        state["telegram_update_offset"] = update["update_id"] + 1
        save_state(state)
        try:
            force = handle_update(update, state) or force
        except Exception as e:  # noqa: BLE001
            log("command failed:", e)
    return force


# --------------------------------------------------------------------------- website check

def first_visible(locator):
    for i in range(locator.count()):
        element = locator.nth(i)
        try:
            if element.is_visible():
                return element
        except Exception:  # noqa: BLE001
            pass
    return None


def check_souramitra():
    """Return the registration page URL if registration looks open, None if the entry
    point is not shown. Raises if the site/layout cannot be verified."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            response = page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            if response is None or response.status >= 400:
                raise RuntimeError("Soura Mitra website returned an error")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
            page.wait_for_timeout(2000)

            cont = first_visible(
                page.get_by_role("button", name=re.compile(r"^\s*Continue\s*$", re.I))
            )
            if cont:
                cont.click()
                page.wait_for_timeout(2500)

            body = page.locator("body").inner_text(timeout=15000).lower()
            if not any(k in body for k in ("track application", "important documents", "public information")):
                raise RuntimeError("Could not verify the expected Soura Mitra homepage.")

            item = first_visible(
                page.get_by_text(re.compile(r"^\s*FARMER\s+REGISTRATION\s*$", re.I))
            )
            if not item:
                return None

            log("Farmer Registration entry point found.")
            item.click()
            page.wait_for_timeout(2500)

            mobile = first_visible(
                page.locator('input[placeholder*="Mobile" i], input[type="tel"]')
            )
            otp = first_visible(page.get_by_role("button", name=re.compile(r"Send OTP", re.I)))
            submit = first_visible(
                page.get_by_role("button", name=re.compile(r"Submit Application", re.I))
            )
            if (mobile and otp) or submit:
                return page.url
            raise RuntimeError(
                "Registration entry point exists, but the registration interface could not be verified."
            )
        finally:
            browser.close()


def run_check_with_retry(attempts=2):
    last = None
    for i in range(attempts):
        try:
            return check_souramitra()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"check attempt {i + 1}/{attempts} failed:", e)
            time.sleep(10)
    raise last


# --------------------------------------------------------------------------- one check cycle

def apply_check(state, check=None):
    """Run one website check and update state / send alerts. Never raises."""
    check = check or run_check_with_retry
    previous = state.get("status", "unknown")
    checked_at = now()
    try:
        url = check()
    except Exception as e:  # noqa: BLE001
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_check_result"] = "failure"
        state["last_error"] = redact(e)[:300]
        log("check failed:", e)
        if state["consecutive_failures"] == FAILURE_ALERT_AFTER:
            try:
                send_message(
                    "🔴 SOURA MITRA MONITOR: website check keeps failing "
                    f"({FAILURE_ALERT_AFTER} times in a row).\nLast error: {state['last_error']}"
                )
            except Exception as ne:  # noqa: BLE001
                log("alert failed:", ne)
        save_state(state)
        return False

    recovered = state.get("consecutive_failures", 0) >= FAILURE_ALERT_AFTER
    try:
        if url and previous != "open":
            send_message(
                "🚨 SOURA MITRA - REGISTRATION DETECTED\n\n"
                "Farmer Registration entry point is accessible.\n"
                "The registration interface was detected, but fresh application "
                "acceptance must be verified.\n\n"
                f"Website: {BASE_URL}\nDetected page: {url}\nTime: {checked_at}"
            )
        elif not url and previous == "open":
            send_message(
                "ℹ️ SOURA MITRA WEBSITE UPDATE\n\n"
                "Farmer Registration entry point is no longer visible.\nMonitoring continues."
            )
        if recovered:
            send_message("🟢 SOURA MITRA MONITOR: website checks are working again.")
    except Exception as e:  # noqa: BLE001
        log("alert failed:", e)

    state["status"] = "open" if url else "closed"
    RUNTIME["registration_url"] = url
    state["last_successful_check"] = checked_at
    state["last_check_result"] = "success"
    state["last_error"] = None
    state["consecutive_failures"] = 0
    save_state(state)
    log("check ok. registration:", state["status"])
    return True


# --------------------------------------------------------------------------- main loop

def install_signal_handlers():
    def handler(signum, _frame):
        raise Stop(signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="one poll + one check, then exit")
    args = parser.parse_args(argv)

    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("SITE_URL", BASE_URL)) if not v]
    if missing:
        print("Missing required secret(s): " + ", ".join(missing)
              + ". Add them under Settings > Secrets and variables > Actions.", file=sys.stderr)
        return 2
    if urlparse(BASE_URL).scheme not in ("http", "https") or not SITE_HOST:
        print("SITE_URL must be a full http(s) URL, e.g. https://example.com/", file=sys.stderr)
        return 2

    install_signal_handlers()
    state = load_state()
    started = now_dt()
    RUNTIME["started_at"] = started
    RUNTIME["ends_at"] = started + timedelta(seconds=RUN_SECONDS)
    log("kAgent monitor starting. previous status:", state.get("status"))

    # Tell the user if monitoring was interrupted for a long time (dropped cron, etc.)
    try:
        last = state.get("last_successful_check")
        if last:
            gap = (started - datetime.fromisoformat(last)).total_seconds()
            if gap > GAP_ALERT_SECONDS and not args.once:
                send_message(
                    f"⚠️ SOURA MITRA MONITOR: no check for {int(gap // 60)} min "
                    "(GitHub did not restart the monitor in time). Back online now."
                )
    except Exception as e:  # noqa: BLE001
        log("gap notice failed:", e)

    next_check = time.monotonic()  # check immediately on start
    deadline = time.monotonic() + RUN_SECONDS
    poll_errors = 0
    exit_code = 0

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("run window finished; exiting cleanly for hand-over.")
                break

            wait = max(next_check - time.monotonic(), 0)
            RUNTIME["next_check"] = now_dt() + timedelta(seconds=wait)
            timeout = 0 if args.once else int(max(1, min(POLL_TIMEOUT, wait, remaining)))
            force = False
            try:
                force = process_updates(state, timeout)
                poll_errors = 0
            except Exception as e:  # noqa: BLE001
                poll_errors += 1
                log(f"telegram poll error {poll_errors}/{MAX_POLL_ERRORS}:", e)
                if poll_errors >= MAX_POLL_ERRORS:
                    exit_code = 1
                    break
                time.sleep(min(5 * poll_errors, 30))

            if force or time.monotonic() >= next_check:
                apply_check(state)
                next_check = time.monotonic() + CHECK_INTERVAL
                if force:
                    try:
                        send_message("✅ Check finished.\n\n" + build_status(state))
                    except Exception as e:  # noqa: BLE001
                        log("reply failed:", e)
                if args.once:
                    break
            elif args.once:
                break
    except Stop:
        log("stop signal received; saving state and exiting.")
    finally:
        save_state(state)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
