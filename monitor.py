
import os
import re
import json
import requests

from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://souramitra.com/"
STATE_FILE = "state.json"

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

API = f"https://api.telegram.org/bot{TOKEN}"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "last_successful_check": None,
            "last_registration_url": None,
            "telegram_update_offset": 0
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_message(message):
    response = requests.post(
        f"{API}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def get_updates(state):
    offset = state.get("telegram_update_offset", 0)

    response = requests.get(
        f"{API}/getUpdates",
        params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message"])
        },
        timeout=20
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram polling error: {result}")

    return result.get("result", [])


def process_commands(state):
    updates = get_updates(state)
    force_check = False

    for update in updates:
        update_id = update["update_id"]
        state["telegram_update_offset"] = update_id + 1

        message = update.get("message", {})
        chat = message.get("chat", {})

        # Only accept commands from your configured Telegram chat.
        if str(chat.get("id")) != CHAT_ID:
            continue

        text = message.get("text", "").strip()

        if not text.startswith("/"):
            continue

        command = text.split()[0].split("@")[0].lower()

        if command == "/status":
            last_check = state.get("last_successful_check")
            status = state.get("status", "unknown")

            if last_check:
                last_time = datetime.fromisoformat(last_check)
                age = datetime.now(timezone.utc) - last_time

                if age.total_seconds() < 2 * 60 * 60:
                    health = "🟢 Recent successful monitoring run"
                else:
                    health = "🟠 STALE - Check GitHub Actions"

                next_check = last_time + timedelta(hours=1)
            else:
                health = "⚪ Monitoring has not completed a check yet"
                next_check = None

            registration_text = {
                "open": "Registration entry point detected",
                "closed": "Registration entry point not detected",
                "unknown": "Not established yet"
            }.get(registration, registration)

            response = (
                "📊 SOURA MITRA MONITOR STATUS\n\n"
                f"Monitor health: {health}\n"
                "Website: souramitra.com\n"
                "Frequency: Every hour\n\n"
                f"Last successful check: {last_check or 'Not checked yet'}\n"
                f"Next expected check: {next_check.isoformat() if next_check else 'Unknown'}\n"
                f"Registration: {registration_text}\n\n"
                "Note: This is based on the last saved successful run."
            )

            send_message(response)

        elif command == "/check":
            force_check = True

            send_message(
                "🔎 CHECK REQUEST RECEIVED\n\n"
                "Soura Mitra will be checked in this workflow run.\n"
                "If this command arrives between scheduled runs, "
                "it will be processed at the next hourly run."
            )

        elif command == "/help":
            send_message(
                "🤖 SOURA MITRA MONITOR COMMANDS\n\n"
                "/status - View last monitoring status\n"
                "/check - Request a website check\n"
                "/help - Show available commands\n\n"
                "Monitoring runs hourly through GitHub Actions."
            )

    return force_check


def first_visible(locator):
    for i in range(locator.count()):
        element = locator.nth(i)

        try:
            if element.is_visible():
                return element
        except Exception:
            pass

    return None


def check_souramitra():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            response = page.goto(
                BASE_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            if response is None or response.status >= 400:
                raise RuntimeError("Soura Mitra website returned an error")

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=10000
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(2000)

            continue_button = first_visible(
                page.get_by_role(
                    "button",
                    name=re.compile(r"^\s*Continue\s*$", re.I)
                )
            )

            if continue_button:
                continue_button.click()
                page.wait_for_timeout(2500)

            body_text = page.locator("body").inner_text(timeout=15000)

            expected_page = any(
                phrase in body_text.lower()
                for phrase in [
                    "track application",
                    "important documents",
                    "public information"
                ]
            )

            if not expected_page:
                raise RuntimeError(
                    "Could not verify the expected Soura Mitra homepage."
                )

            registration_item = first_visible(
                page.get_by_text(
                    re.compile(
                        r"^\s*FARMER\s+REGISTRATION\s*$",
                        re.I
                    )
                )
            )

            if not registration_item:
                return None

            print("Farmer Registration entry point found.")

            registration_item.click()
            page.wait_for_timeout(2500)

            mobile_input = first_visible(
                page.locator(
                    'input[placeholder*="Mobile" i], input[type="tel"]'
                )
            )

            otp_button = first_visible(
                page.get_by_role(
                    "button",
                    name=re.compile(r"Send OTP", re.I)
                )
            )

            submit_button = first_visible(
                page.get_by_role(
                    "button",
                    name=re.compile(r"Submit Application", re.I)
                )
            )

            if (
                mobile_input and otp_button
            ) or submit_button:
                return page.url

            raise RuntimeError(
                "Registration entry point exists, but the expected "
                "registration interface could not be verified."
            )

        finally:
            browser.close()


def main():
    state = load_state()

    print("Starting Soura Mitra monitoring.")
    print("Previous status:", state.get("status"))

    # Process Telegram commands before checking the website.
    process_commands(state)

    # Every workflow run performs a website check.
    registration_url = check_souramitra()

    previous_status = state.get("status", "unknown")
    checked_at = now()

    if registration_url:

        if previous_status != "open":
            send_message(
                "🚨 SOURA MITRA – REGISTRATION DETECTED\n\n"
                "Farmer Registration entry point is accessible.\n\n"
                "The registration interface was detected, "
                "but fresh application acceptance must be verified.\n\n"
                f"Website: {BASE_URL}\n"
                f"Detected page: {registration_url}\n"
                f"Time: {checked_at}"
            )

        state["status"] = "open"
        state["last_registration_url"] = registration_url

    else:

        if previous_status == "open":
            send_message(
                "ℹ️ SOURA MITRA WEBSITE UPDATE\n\n"
                "Farmer Registration entry point is no longer visible.\n"
                "Monitoring continues."
            )

        state["status"] = "closed"
        state["last_registration_url"] = None

    state["last_successful_check"] = checked_at
    state["last_check_result"] = "success"

    save_state(state)

    print("Monitoring completed successfully.")
    print("Registration status:", state["status"])
    print("Last successful check:", checked_at)


if __name__ == "__main__":
    main()
