
import os
import re
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://souramitra.com/"
STATE_FILE = "state.json"

KEYWORDS = re.compile(
    r"kusum|component[\s-]*b|farmer|registration|register|apply|solar.?pump",
    re.I
)

COMPONENT_B = re.compile(
    r"component[\s-]*b|component b",
    re.I
)

FARMER_REG = re.compile(
    r"farmer|registration|register|apply",
    re.I
)


def telegram_alert(url):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    message = (
        "🚨 PM-KUSUM KARNATAKA – COMPONENT B\n\n"
        "A potentially active farmer registration form has been detected.\n\n"
        f"Registration page: {url}\n"
        f"Detected: {datetime.now(timezone.utc).isoformat()}\n\n"
        "Please open the official page and verify availability."
    )

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=20
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram notification failed")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "unknown", "url": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_active_registration(page, link_context=""):
    text = page.locator("body").inner_text(timeout=10000)

    component_b = bool(
        COMPONENT_B.search(text) or COMPONENT_B.search(link_context)
    )
    farmer_registration = bool(FARMER_REG.search(text))

    if not (component_b and farmer_registration):
        return False

    forms = page.locator("form")

    for i in range(forms.count()):
        form = forms.nth(i)

        if not form.is_visible():
            continue

        controls = form.locator(
            "input:not([type=hidden]), select, textarea"
        )

        usable_controls = 0
        for j in range(controls.count()):
            control = controls.nth(j)
            if control.is_visible() and control.is_enabled():
                usable_controls += 1

        submit = form.locator(
            'button[type="submit"], input[type="submit"]'
        )

        enabled_submit = False
        for j in range(submit.count()):
            item = submit.nth(j)
            if item.is_visible() and item.is_enabled():
                enabled_submit = True

        if usable_controls >= 2 and enabled_submit:
            return True

    return False


def check_portal():
    candidates = [(BASE_URL, "Karnataka PM-KUSUM portal")]
    visited = set()
    loaded = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while candidates and len(visited) < 20:
            url, context = candidates.pop(0)

            if url in visited:
                continue

            visited.add(url)

            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45000
                )

                if response is None or response.status >= 400:
                    continue

                page.wait_for_timeout(2500)
                loaded += 1

                if is_active_registration(page, context):
                    browser.close()
                    return url

                # Discover relevant links on the official site.
                links = page.locator("a[href]")
                for i in range(min(links.count(), 150)):
                    link = links.nth(i)

                    try:
                        href = link.get_attribute("href")
                        label = link.inner_text(timeout=1000)

                        if not href:
                            continue

                        absolute = urljoin(url, href)
                        parsed = urlparse(absolute)

                        if parsed.scheme not in ("http", "https"):
                            continue

                        # Do not follow unrelated external websites.
                        if parsed.netloc != "souramitra.com":
                            continue

                        context_text = f"{label} {context}"

                        if KEYWORDS.search(
                            context_text + " " + absolute
                        ):
                            if absolute not in visited:
                                candidates.append(
                                    (absolute, context_text)
                                )
                    except Exception:
                        continue

            except Exception as error:
                print(f"Could not inspect {url}: {error}")

        browser.close()

    if loaded == 0:
        raise RuntimeError(
            "Could not access the portal. Keeping previous status."
        )

    return None


def main():
    previous = load_state()
    registration_url = check_portal()

    if registration_url:
        print("Potential registration form detected:", registration_url)

        if previous.get("status") != "open":
            telegram_alert(registration_url)
            print("Telegram alert sent.")
        else:
            print("Already alerted. No duplicate notification.")

        save_state({
            "status": "open",
            "url": registration_url,
            "checked_at": datetime.now(timezone.utc).isoformat()
        })

    else:
        print("No active Component-B registration form detected.")

        save_state({
            "status": "closed",
            "url": None,
            "checked_at": datetime.now(timezone.utc).isoformat()
        })


if __name__ == "__main__":
    main()
