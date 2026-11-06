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
import html
import json
import os
import re
import shutil
import signal
import subprocess
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
REPEAT_SECONDS = env_int("REPEAT_SECONDS", 600)           # re-alert while registration is open...
MAX_REPEATS = env_int("MAX_REPEATS", 12)                  # ...until /ack, at most this many alerts

# Optional personalisation of the "registration open" alert (never required).
MENTION_NAME = os.environ.get("MENTION_NAME", "").strip()          # name shown in the mention
MENTION_USERNAME = os.environ.get("MENTION_USERNAME", "").strip().lstrip("@")  # for group chats

IST = timezone(timedelta(hours=5, minutes=30))            # India has no daylight saving

# Screenshot settings. Same width as Playwright's default, so the page layout (and the
# selectors in check_souramitra) behave exactly as before; scale 1.5 keeps text crisp.
VIEWPORT = {"width": 1280, "height": 960}
DEVICE_SCALE = 1.5
MAX_PHOTO_BYTES = 9_000_000      # Telegram photo limit is 10 MB
CAPTION_LIMIT = 1000             # Telegram caption limit is 1024 characters
BLANK_PNG_BYTES = 15_000         # a 1280x960 page with real content is far larger than this

DEFAULT_STATE = {
    "status": "unknown",
    "last_successful_check": None,
    "last_check_result": None,
    "last_error": None,
    "consecutive_failures": 0,
    "telegram_update_offset": 0,
    # "registration open" alert bookkeeping (times only, nothing sensitive)
    "alert_acked": False,
    "alert_count": 0,
    "last_alert_at": None,
    "pinned_message_id": None,
}

# In-memory info shown by /status. The detected page URL lives here only: state.json is
# published on the public "state" branch, so it must not contain the site address.
RUNTIME = {"started_at": None, "ends_at": None, "next_check": None, "registration_url": None,
           "screenshot": None, "error_screenshot": None, "user_name": None}   # PNG bytes, memory only


class Stop(BaseException):
    """Raised by the signal handler. BaseException so `except Exception` can't swallow it."""


# --------------------------------------------------------------------------- helpers

def now_dt():
    return datetime.now(timezone.utc)


def now():
    return now_dt().isoformat(timespec="seconds")


def fmt_ist(value):
    """Human-readable IST time, e.g. '6 Nov 2026, 6:42 PM IST'. Accepts datetime or ISO text.
    State files stay in UTC; only what the user reads is converted."""
    if not value:
        return "n/a"
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(IST)
    return f"{dt.day} {dt:%b %Y}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'} IST"


def esc(text):
    """Escape page/error text before putting it in an HTML-formatted Telegram message."""
    return html.escape(str(text), quote=False)


def mention():
    """Highlighted name for the open-registration alert. In a private chat the chat id is
    the user id, so tg://user?id=... renders a tappable mention (and notifies in groups)."""
    if re.fullmatch(r"\w{4,32}", MENTION_USERNAME):
        return f"@{MENTION_USERNAME}"
    name = esc(MENTION_NAME or RUNTIME.get("user_name") or "Farmer")
    if CHAT_ID.isdigit():          # positive id = a private chat with the user
        return f'<a href="tg://user?id={CHAT_ID}">{name}</a>'
    return f"<b>{name}</b>"


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


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_message(text, silent=False):
    """Send an HTML-formatted message (callers escape dynamic text with esc()).
    If Telegram still rejects the markup, resend as plain text so an alert is never lost.
    Returns the message_id."""
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    try:
        return (tg("sendMessage", payload) or {}).get("message_id")
    except RuntimeError as e:
        if "parse entities" not in str(e):
            raise
        payload.pop("parse_mode")
        payload["text"] = strip_tags(text)
        return (tg("sendMessage", payload) or {}).get("message_id")


def send_photo(image, caption, silent=False):
    """Send a PNG with an HTML caption. Falls back to a text message if the upload fails
    (too big, network, bad markup), so the alert text always arrives. Returns message_id."""
    caption = caption if len(caption) <= CAPTION_LIMIT else caption[:CAPTION_LIMIT - 1] + "…"
    if not image or len(image) > MAX_PHOTO_BYTES:
        return send_message(caption, silent)
    try:
        r = requests.post(
            f"{API}/sendPhoto",
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
                  "disable_notification": str(bool(silent)).lower()},
            files={"photo": ("page.png", image, "image/png")},
            timeout=60,
        )
        data = r.json() if r.content else {}
        if r.ok and data.get("ok"):
            return (data.get("result") or {}).get("message_id")
        log("sendPhoto failed: HTTP", r.status_code, data.get("description", ""))
    except requests.RequestException as e:
        log("sendPhoto network error:", type(e).__name__)
    return send_message(caption, silent)


def pin_message(message_id, silent=True):
    """Pin the alert at the top of the chat (best effort; needs no extra rights in a private chat)."""
    if not message_id:
        return
    try:
        tg("pinChatMessage", {"chat_id": CHAT_ID, "message_id": message_id,
                              "disable_notification": silent}, retries=1)
    except Exception as e:  # noqa: BLE001
        log("pin failed:", e)


def unpin_message(message_id):
    if not message_id:
        return
    try:
        tg("unpinChatMessage", {"chat_id": CHAT_ID, "message_id": message_id}, retries=1)
    except Exception as e:  # noqa: BLE001
        log("unpin failed:", e)


# --------------------------------------------------------------------------- commands

REGISTRATION_TEXT = {
    "open": "Registration entry point detected",
    "closed": "Registration entry point not detected",
    "unknown": "Not established yet",
}

HELP_TEXT = (
    "🤖 <b>kAgent - SOURA MITRA MONITOR</b>\n\n"
    "/status - Monitor health and last result\n"
    "/check - Check the website right now\n"
    "/screenshot - Send a picture of the website's home page\n"
    "/ack - Stop repeat alerts after registration opens\n"
    "/help - Show this list\n\n"
    f"The monitor checks every {CHECK_INTERVAL // 60} min and answers commands within seconds. "
    "All times are in IST."
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
    icon = {"open": "✅", "closed": "❌"}.get(status, "❔")
    return (
        "📊 <b>SOURA MITRA MONITOR STATUS</b>\n\n"
        f"Monitor health: {health}\n"
        f"Website: {esc(BASE_URL)}\n"
        f"Frequency: every {CHECK_INTERVAL // 60} min\n\n"
        f"Last successful check: {fmt_ist(last) if last else 'Not checked yet'}\n"
        f"Next check: {fmt_ist(nxt) if nxt else 'Unknown'}\n"
        f"Registration: {icon} {esc(REGISTRATION_TEXT.get(status, status))}\n"
        f"Consecutive failures: {state.get('consecutive_failures', 0)}\n"
        f"This run ends (then auto-restarts): {fmt_ist(ends) if ends else 'n/a'}"
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
    RUNTIME["user_name"] = (message.get("from") or {}).get("first_name") or RUNTIME.get("user_name")
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    command = text.split()[0].split("@")[0].lower()
    if command == "/status":
        send_message(build_status(state))
    elif command == "/check":
        send_message("🔎 Checking Soura Mitra now...")
        return True
    elif command == "/screenshot":
        handle_screenshot()
    elif command == "/ack":
        handle_ack(state)
    elif command in ("/help", "/start"):
        send_message(HELP_TEXT)
    return False


def handle_screenshot():
    """/screenshot: live picture of the home page. If it fails, say why in text."""
    send_message("📸 Taking a screenshot of the website...", silent=True)
    try:
        png = take_screenshot()
    except Exception as e:  # noqa: BLE001
        log("screenshot command failed:", e)
        send_message(f"⚠️ Could not take a screenshot: {esc(redact(e))[:300]}")
        return
    send_photo(png, f"📸 <b>Soura Mitra home page</b>\n🕒 {fmt_ist(now_dt())}")


def handle_ack(state):
    if state.get("status") == "open" and not state.get("alert_acked"):
        state["alert_acked"] = True
        unpin_message(state.get("pinned_message_id"))
        state["pinned_message_id"] = None
        save_state(state)
        send_message("👍 Acknowledged. Repeat alerts stopped; monitoring continues.")
    else:
        send_message("Nothing to acknowledge right now.")


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


FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def ensure_fonts():
    """Make Kannada (and other Indic) text render in screenshots.

    macOS ships Kannada fonts. A fresh Linux runner does not, so Chromium would draw empty
    boxes. We bundle Noto Sans Kannada (SIL OFL) in fonts/ and register it for fontconfig
    under ~/.local/share/fonts. Idempotent, no apt/network, never raises."""
    if sys.platform == "darwin":
        return True
    try:
        dest = os.path.expanduser("~/.local/share/fonts/kagent")
        os.makedirs(dest, exist_ok=True)
        copied = False
        for name in os.listdir(FONT_DIR) if os.path.isdir(FONT_DIR) else []:
            if name.endswith(".ttf") and not os.path.exists(os.path.join(dest, name)):
                shutil.copy(os.path.join(FONT_DIR, name), dest)
                copied = True
        if copied and shutil.which("fc-cache"):
            subprocess.run(["fc-cache", "-f", dest], timeout=60, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if shutil.which("fc-list"):
            out = subprocess.run(["fc-list", ":lang=kn", "family"], capture_output=True,
                                 text=True, timeout=30).stdout
            if not out.strip():
                log("WARNING: no Kannada font found; screenshots may show boxes for Kannada text.")
                return False
        return True
    except Exception as e:  # noqa: BLE001
        log("font setup failed:", e)
        return False


def snap(page):
    """PNG of the current viewport, or None. Never raises: a screenshot problem must not
    break the check. Waits for web fonts first, and retries once if the page looks blank
    (a blank page compresses to a tiny PNG)."""
    for attempt in range(2):
        try:
            page.evaluate("document.fonts ? document.fonts.ready.then(() => true) : true")
            page.wait_for_timeout(500)
            png = page.screenshot(type="png", full_page=False, timeout=20000)
            if len(png) >= BLANK_PNG_BYTES or attempt == 1:
                if len(png) < BLANK_PNG_BYTES:
                    log("screenshot looks blank; sending it anyway")
                return png
            page.wait_for_timeout(2500)   # looked blank: give the page more time
        except Exception as e:  # noqa: BLE001
            log("screenshot failed:", e)
            return None
    return None


def new_page(browser):
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=DEVICE_SCALE,
                                  locale="en-IN", timezone_id="Asia/Kolkata")
    return context.new_page()


def open_home(page):
    """Load the home page and click the optional 'Continue' gate. Raises on HTTP errors."""
    from playwright.sync_api import TimeoutError as PWTimeout

    response = page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    if response is None or response.status >= 400:
        raise RuntimeError("Soura Mitra website returned an error")
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    cont = first_visible(page.get_by_role("button", name=re.compile(r"^\s*Continue\s*$", re.I)))
    if cont:
        cont.click()
        page.wait_for_timeout(2500)


def take_screenshot():
    """Fresh browser, home page, PNG bytes (used by /screenshot). Raises if it can't load."""
    from playwright.sync_api import sync_playwright

    ensure_fonts()   # before launch: Chromium reads the font list at startup
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = new_page(browser)
            open_home(page)
            png = snap(page)
            if not png:
                raise RuntimeError("could not capture a screenshot")
            return png
        finally:
            browser.close()


def check_souramitra():
    """Return the registration page URL if registration looks open, None if the entry
    point is not shown. Raises if the site/layout cannot be verified.
    Side effect: RUNTIME['screenshot'] holds a PNG of the page that decided the result
    (registration page when open, home page when closed); RUNTIME['error_screenshot']
    holds the page as it looked when a check failed. The page stays open until captured."""
    from playwright.sync_api import sync_playwright

    RUNTIME["screenshot"] = RUNTIME["error_screenshot"] = None
    ensure_fonts()   # before launch: Chromium reads the font list at startup
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = new_page(browser)
        try:
            open_home(page)

            body = page.locator("body").inner_text(timeout=15000).lower()
            if not any(k in body for k in ("track application", "important documents", "public information")):
                raise RuntimeError("Could not verify the expected Soura Mitra homepage.")

            item = first_visible(
                page.get_by_text(re.compile(r"^\s*FARMER\s+REGISTRATION\s*$", re.I))
            )
            if not item:
                RUNTIME["screenshot"] = snap(page)   # closed: show the home page we saw
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
                RUNTIME["screenshot"] = snap(page)   # captured BEFORE the browser closes
                return page.url
            raise RuntimeError(
                "Registration entry point exists, but the registration interface could not be verified."
            )
        except Exception:
            # Keep what the page looked like when the check failed (layout change vs outage).
            RUNTIME["error_screenshot"] = snap(page)
            raise
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

def send_open_alert(state, url, repeat=False):
    """The loud alert: bold headline, a mention of the user, the page screenshot, a pin.
    Repeats (until /ack) are marked as reminders and are not re-pinned."""
    count = state.get("alert_count", 0) + 1 if repeat else 1   # a fresh opening restarts the count
    headline = "🚨🚨 <b>REGISTRATION OPEN - SOURA MITRA</b> 🚨🚨"
    if repeat:
        headline = f"🔁 <b>REMINDER {count}/{MAX_REPEATS}: REGISTRATION STILL OPEN</b>"
    caption = (
        f"{headline}\n\n{mention()}, Farmer Registration is now accessible.\n"
        "The registration form was detected. Please verify that new applications "
        "are being accepted.\n\n"
        f"🕒 Detected: {fmt_ist(now_dt())}\n"
        f"🔗 Page: {esc(url)}\n"
        "Reply /ack to stop reminders."
    )
    message_id = send_photo(RUNTIME.get("screenshot"), caption)
    state["alert_count"] = count
    state["last_alert_at"] = now()
    if not repeat:
        state["alert_acked"] = False
        state["pinned_message_id"] = message_id
        pin_message(message_id)


def apply_check(state, check=None):
    """Run one website check and update state / send alerts. Never raises."""
    check = check or run_check_with_retry
    previous = state.get("status", "unknown")
    checked_at = now()
    RUNTIME["screenshot"] = RUNTIME["error_screenshot"] = None
    try:
        url = check()
    except Exception as e:  # noqa: BLE001
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_check_result"] = "failure"
        state["last_error"] = redact(e)[:300]
        log("check failed:", e)
        if state["consecutive_failures"] == FAILURE_ALERT_AFTER:
            try:
                send_photo(
                    RUNTIME.get("error_screenshot"),
                    "🔴 <b>SOURA MITRA MONITOR: website check keeps failing</b> "
                    f"({FAILURE_ALERT_AFTER} times in a row).\n"
                    f"🕒 {fmt_ist(now_dt())}\nLast error: {esc(state['last_error'])}",
                )
            except Exception as ne:  # noqa: BLE001
                log("alert failed:", ne)
        save_state(state)
        return False

    recovered = state.get("consecutive_failures", 0) >= FAILURE_ALERT_AFTER
    try:
        if url and previous != "open":
            send_open_alert(state, url)
        elif url and not state.get("alert_acked"):
            due = state.get("last_alert_at")
            waited = (now_dt() - datetime.fromisoformat(due)).total_seconds() if due else 1e9
            if waited >= REPEAT_SECONDS and state.get("alert_count", 0) < MAX_REPEATS:
                send_open_alert(state, url, repeat=True)
        elif not url and previous == "open":
            unpin_message(state.get("pinned_message_id"))
            state["pinned_message_id"] = None
            send_photo(
                RUNTIME.get("screenshot"),
                "ℹ️ <b>SOURA MITRA WEBSITE UPDATE</b>\n\n"
                "Farmer Registration entry point is no longer visible.\n"
                f"🕒 {fmt_ist(now_dt())}\nMonitoring continues.",
            )
        if recovered:
            send_message(f"🟢 <b>SOURA MITRA MONITOR:</b> website checks are working again.\n🕒 {fmt_ist(now_dt())}")
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
                    f"⚠️ <b>SOURA MITRA MONITOR:</b> no check for {int(gap // 60)} min "
                    "(GitHub did not restart the monitor in time). "
                    f"Back online now.\n🕒 {fmt_ist(started)}"
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
